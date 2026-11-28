#include "websocket_protocol.h"
#include "application.h"
#include "board.h"
#include "settings.h"
#include "system_info.h"

#include <esp_log.h>
#include <arpa/inet.h>
#include <cJSON.h>
#include <cstring>
#include "assets/lang_config.h"

#define TAG "WS"

WebsocketProtocol::WebsocketProtocol() { event_group_handle_ = xEventGroupCreate(); }

WebsocketProtocol::~WebsocketProtocol() {
    std::shared_ptr<WebSocket> websocket;
    {
        std::lock_guard<std::mutex> lock(channel_mutex_);
        // Destruction is intentional retirement, just like an explicit close.
        AdvanceChannelToken();
        health_channel_token_ = 0;
        media_started_ = false;
        awaiting_hello_ = false;
        hello_valid_ = false;
        websocket = std::move(websocket_);
    }
    websocket.reset();
    vEventGroupDelete(event_group_handle_);
}

bool WebsocketProtocol::Start() {
    // Only connect to server when audio channel is needed
    return true;
}

bool WebsocketProtocol::SendAudio(std::unique_ptr<AudioStreamPacket> packet) {
    std::shared_ptr<WebSocket> websocket;
    ChannelToken source_token;
    int version;
    {
        std::lock_guard<std::mutex> lock(channel_mutex_);
        websocket = websocket_;
        source_token = health_channel_token_;
        version = version_;
    }
    if (websocket == nullptr || !websocket->IsConnected()) {
        return false;
    }

    const auto send_binary = [&](const void* data, size_t size) {
        const bool sent = websocket->Send(data, size, true);
        if (!sent) {
            // Never attach a blocking old send's failure to the current channel.
            EmitHealthEvent(
                {HealthStage::Media, HealthOutcome::Failed, source_token, source_token});
        }
        return sent;
    };
    if (version == 2) {
        std::string serialized;
        serialized.resize(sizeof(BinaryProtocol2) + packet->payload.size());
        auto bp2 = (BinaryProtocol2*)serialized.data();
        bp2->version = htons(version);
        bp2->type = 0;
        bp2->reserved = 0;
        bp2->timestamp = htonl(packet->timestamp);
        bp2->payload_size = htonl(packet->payload.size());
        memcpy(bp2->payload, packet->payload.data(), packet->payload.size());

        return send_binary(serialized.data(), serialized.size());
    } else if (version == 3) {
        std::string serialized;
        serialized.resize(sizeof(BinaryProtocol3) + packet->payload.size());
        auto bp3 = (BinaryProtocol3*)serialized.data();
        bp3->type = 0;
        bp3->reserved = 0;
        bp3->payload_size = htons(packet->payload.size());
        memcpy(bp3->payload, packet->payload.data(), packet->payload.size());

        return send_binary(serialized.data(), serialized.size());
    } else {
        return send_binary(packet->payload.data(), packet->payload.size());
    }
}

bool WebsocketProtocol::SendText(const std::string& text) {
    std::shared_ptr<WebSocket> websocket;
    ChannelToken source_token;
    bool media_started;
    {
        std::lock_guard<std::mutex> lock(channel_mutex_);
        websocket = websocket_;
        source_token = health_channel_token_;
        media_started = media_started_;
    }
    if (websocket == nullptr || !websocket->IsConnected()) {
        return false;
    }

    if (!websocket->Send(text)) {
        ESP_LOGE(TAG, "Failed to send text: %s", text.c_str());
        EmitHealthEvent({media_started ? HealthStage::Media : HealthStage::Handshake,
                         HealthOutcome::Failed, source_token, source_token});
        bool current_source;
        {
            std::lock_guard<std::mutex> lock(channel_mutex_);
            current_source = websocket_ == websocket;
        }
        if (current_source) {
            SetError(Lang::Strings::SERVER_ERROR);
        }
        return false;
    }

    return true;
}

bool WebsocketProtocol::IsAudioChannelOpened() const {
    std::lock_guard<std::mutex> lock(channel_mutex_);
    return websocket_ != nullptr && websocket_->IsConnected() && !error_occurred_ && !IsTimeout();
}

void WebsocketProtocol::CloseAudioChannel(bool send_goodbye) {
    (void)send_goodbye;  // Websocket doesn't need to send goodbye message
    ChannelToken token;
    ChannelToken retired_token;
    bool retired_media;
    std::shared_ptr<WebSocket> websocket;
    {
        std::lock_guard<std::mutex> lock(channel_mutex_);
        token = AdvanceChannelToken();
        retired_token = health_channel_token_;
        retired_media = media_started_;
        health_channel_token_ = 0;
        media_started_ = false;
        awaiting_hello_ = false;
        hello_valid_ = false;
        websocket = std::move(websocket_);
    }
    websocket.reset();
    EmitHealthEvent({HealthStage::Handshake, HealthOutcome::Retired, retired_token, retired_token});
    if (retired_media) {
        EmitHealthEvent({HealthStage::Media, HealthOutcome::Retired, retired_token, retired_token});
    }
    if (on_audio_channel_closed_ != nullptr) {
        on_audio_channel_closed_(token);
    }
}

bool WebsocketProtocol::OpenAudioChannel() {
    ChannelToken token;
    ChannelToken retired_token;
    bool retired_media;
    std::shared_ptr<WebSocket> previous_websocket;
    {
        std::lock_guard<std::mutex> lock(channel_mutex_);
        token = AdvanceChannelToken();
        retired_token = health_channel_token_;
        retired_media = media_started_;
        health_channel_token_ = token;
        media_started_ = false;
        awaiting_hello_ = true;
        hello_valid_ = false;
        SetSessionId("");
        previous_websocket = std::move(websocket_);
        xEventGroupClearBits(event_group_handle_, WEBSOCKET_PROTOCOL_SERVER_HELLO_EVENT);
    }
    previous_websocket.reset();
    EmitHealthEvent({HealthStage::Handshake, HealthOutcome::Retired, retired_token, retired_token});
    if (retired_media) {
        EmitHealthEvent({HealthStage::Media, HealthOutcome::Retired, retired_token, retired_token});
    }
    EmitHealthEvent({HealthStage::Handshake, HealthOutcome::Started, token, token});
    Settings settings("websocket", false);
    std::string url = settings.GetString("url");
    std::string auth_token = settings.GetString("token");
    int version = settings.GetInt("version");
    if (version != 0) {
        version_ = version;
    }

    error_occurred_ = false;

    auto network = Board::GetInstance().GetNetwork();
    std::shared_ptr<WebSocket> websocket = network->CreateWebSocket(1);
    if (websocket == nullptr) {
        ESP_LOGE(TAG, "Failed to create websocket");
        EmitHealthEvent({HealthStage::Handshake, HealthOutcome::Failed, token, token});
        return false;
    }
    {
        std::lock_guard<std::mutex> lock(channel_mutex_);
        if (token != channel_token()) {
            return false;
        }
        websocket_ = websocket;
    }

    if (!auth_token.empty()) {
        // If token not has a space, add "Bearer " prefix
        if (auth_token.find(" ") == std::string::npos) {
            auth_token = "Bearer " + auth_token;
        }
        websocket->SetHeader("Authorization", auth_token.c_str());
    }
    websocket->SetHeader("Protocol-Version", std::to_string(version_).c_str());
    websocket->SetHeader("Device-Id", SystemInfo::GetMacAddress().c_str());
    websocket->SetHeader("Client-Id", Board::GetInstance().GetUuid().c_str());

    websocket->OnData([this, token, version = version_](const char* data, size_t len, bool binary) {
        if (token != channel_token()) {
            return;
        }
        if (binary) {
            if (on_incoming_audio_ != nullptr) {
                auto packet = std::make_unique<AudioStreamPacket>();
                {
                    std::lock_guard<std::mutex> lock(channel_mutex_);
                    if (token != channel_token()) {
                        return;
                    }
                    packet->sample_rate = server_sample_rate_;
                    packet->frame_duration = server_frame_duration_;
                }
                const auto* payload = reinterpret_cast<const uint8_t*>(data);
                size_t payload_size = len;
                if (version == 2) {
                    if (len < sizeof(BinaryProtocol2)) {
                        return;
                    }
                    BinaryProtocol2 header;
                    memcpy(&header, data, sizeof(header));
                    packet->timestamp = ntohl(header.timestamp);
                    payload_size = ntohl(header.payload_size);
                    payload += sizeof(header);
                    if (payload_size > len - sizeof(header)) {
                        return;
                    }
                } else if (version == 3) {
                    if (len < sizeof(BinaryProtocol3)) {
                        return;
                    }
                    BinaryProtocol3 header;
                    memcpy(&header, data, sizeof(header));
                    payload_size = ntohs(header.payload_size);
                    payload += sizeof(header);
                    if (payload_size > len - sizeof(header)) {
                        return;
                    }
                }
                packet->payload.assign(payload, payload + payload_size);
                on_incoming_audio_(std::move(packet), token);
            }
        } else {
            // Parse JSON data
            auto root = cJSON_ParseWithLength(data, len);
            auto type = cJSON_GetObjectItem(root, "type");
            if (cJSON_IsString(type)) {
                if (strcmp(type->valuestring, "hello") == 0) {
                    ParseServerHello(root, token);
                } else {
                    if (on_incoming_json_ != nullptr) {
                        on_incoming_json_(root, token);
                    }
                }
            } else {
                ESP_LOGE(TAG, "Missing message type, data: %s", std::string(data, len).c_str());
            }
            cJSON_Delete(root);
        }
        {
            std::lock_guard<std::mutex> lock(channel_mutex_);
            if (token == channel_token()) {
                last_incoming_time_ = std::chrono::steady_clock::now();
            }
        }
    });

    // Callbacks run on the transport's receive task, which on 4G modems is the
    // AT event task and cannot send, so the Pong is sent from the main task.
    std::weak_ptr<WebSocket> weak_websocket = websocket;
    if (!recovery_probe_) {
        websocket->OnPing([weak_websocket](const char* data, size_t len) {
            Application::GetInstance().Schedule(
                [weak_websocket, payload = std::string(data, len)]() {
                    if (auto websocket = weak_websocket.lock()) {
                        websocket->SendPong(payload.data(), payload.size());
                    }
                });
        });
    }

    websocket->OnDisconnected([this, token]() {
        ChannelToken closed_token;
        bool media_started;
        {
            std::lock_guard<std::mutex> lock(channel_mutex_);
            if (token != channel_token()) {
                return;
            }
            media_started = media_started_;
            health_channel_token_ = 0;
            media_started_ = false;
            awaiting_hello_ = false;
            hello_valid_ = false;
            closed_token = AdvanceChannelToken();
        }
        EmitHealthEvent({media_started ? HealthStage::Media : HealthStage::Handshake,
                         HealthOutcome::Failed, token, token});
        ESP_LOGI(TAG, "Websocket disconnected");
        if (on_audio_channel_closed_ != nullptr) {
            on_audio_channel_closed_(closed_token);
        }
    });

    ESP_LOGI(TAG, "Connecting to websocket server: %s with version: %d", url.c_str(), version_);
    if (auto connected = websocket->Connect(url.c_str()); !connected) {
        ESP_LOGE(TAG, "Failed to connect to websocket server: %s",
                 connected.error().ToString().c_str());
        EmitHealthEvent({HealthStage::Handshake, HealthOutcome::Failed, token, token});
        bool current_source;
        {
            std::lock_guard<std::mutex> lock(channel_mutex_);
            current_source = websocket_ == websocket;
            if (current_source) {
                awaiting_hello_ = false;
            }
        }
        if (current_source) {
            SetError(Lang::Strings::SERVER_NOT_CONNECTED, url);
        }
        return false;
    }

    // Send hello message to describe the client
    auto message = GetHelloMessage();
    if (!SendText(message)) {
        {
            std::lock_guard<std::mutex> lock(channel_mutex_);
            if (token == channel_token()) {
                awaiting_hello_ = false;
            }
        }
        EmitHealthEvent({HealthStage::Handshake, HealthOutcome::Failed, token, token});
        return false;
    }

    // Wait for server hello
    EventBits_t bits =
        xEventGroupWaitBits(event_group_handle_, WEBSOCKET_PROTOCOL_SERVER_HELLO_EVENT, pdTRUE,
                            pdFALSE, pdMS_TO_TICKS(10000));
    bool valid;
    {
        std::lock_guard<std::mutex> lock(channel_mutex_);
        if (websocket_ != websocket) {
            return false;
        }
        awaiting_hello_ = false;
        valid = hello_valid_;
        if (!(bits & WEBSOCKET_PROTOCOL_SERVER_HELLO_EVENT)) {
            hello_valid_ = false;
        }
    }
    if (!(bits & WEBSOCKET_PROTOCOL_SERVER_HELLO_EVENT) || !valid) {
        ESP_LOGE(TAG, "Failed to receive server hello");
        EmitHealthEvent({HealthStage::Handshake, HealthOutcome::Failed, token, token});
        SetError(Lang::Strings::SERVER_TIMEOUT);
        return false;
    }

    if (token != channel_token()) {
        return false;
    }
    if (on_audio_channel_opened_ != nullptr) {
        on_audio_channel_opened_(token);
    }

    return true;
}

std::string WebsocketProtocol::GetHelloMessage() {
    // keys: message type, version, audio_params (format, sample_rate, channels)
    cJSON* root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "hello");
    cJSON_AddNumberToObject(root, "version", version_);
    AddClientCapabilities(root);
    cJSON_AddStringToObject(root, "transport", "websocket");
    cJSON* audio_params = cJSON_CreateObject();
    cJSON_AddStringToObject(audio_params, "format", "opus");
    cJSON_AddNumberToObject(audio_params, "sample_rate", 16000);
    cJSON_AddNumberToObject(audio_params, "channels", 1);
    cJSON_AddNumberToObject(audio_params, "frame_duration", OPUS_FRAME_DURATION_MS);
    cJSON_AddItemToObject(root, "audio_params", audio_params);
    auto json_str = cJSON_PrintUnformatted(root);
    std::string message(json_str);
    cJSON_free(json_str);
    cJSON_Delete(root);
    return message;
}

void WebsocketProtocol::ParseServerHello(const cJSON* root, ChannelToken token) {
    std::unique_lock<std::mutex> lock(channel_mutex_);
    if (token != channel_token() || !awaiting_hello_ || hello_valid_) {
        return;
    }
    auto transport = cJSON_GetObjectItem(root, "transport");
    if (!cJSON_IsString(transport)) {
        ESP_LOGE(TAG, "Missing or non-string transport in server hello");
        return;
    }
    if (strcmp(transport->valuestring, "websocket") != 0) {
        ESP_LOGE(TAG, "Unsupported transport: %s", transport->valuestring);
        return;
    }

    auto session_id = cJSON_GetObjectItem(root, "session_id");
    if (cJSON_IsString(session_id)) {
        SetSessionId(session_id->valuestring);
    }

    auto audio_params = cJSON_GetObjectItem(root, "audio_params");
    if (cJSON_IsObject(audio_params)) {
        auto sample_rate = cJSON_GetObjectItem(audio_params, "sample_rate");
        if (cJSON_IsNumber(sample_rate)) {
            server_sample_rate_ = sample_rate->valueint;
        }
        auto frame_duration = cJSON_GetObjectItem(audio_params, "frame_duration");
        if (cJSON_IsNumber(frame_duration)) {
            server_frame_duration_ = frame_duration->valueint;
        }
    }
    hello_valid_ = true;
    media_started_ = true;
    lock.unlock();
    EmitHealthEvent({HealthStage::Handshake, HealthOutcome::Succeeded, token, token});
    EmitHealthEvent({HealthStage::Media, HealthOutcome::Started, token, token});
    lock.lock();
    // Serialize completion with Open's ClearBits and with timeout/cancellation.
    // The callback above may have outlived its original waiter.
    if (token == channel_token() && awaiting_hello_ && hello_valid_) {
        awaiting_hello_ = false;
        xEventGroupSetBits(event_group_handle_, WEBSOCKET_PROTOCOL_SERVER_HELLO_EVENT);
    }
}
