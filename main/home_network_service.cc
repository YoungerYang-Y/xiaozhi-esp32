#include "home_network_service.h"

#include "board.h"
#include "ota.h"
#include "protocols/mqtt_protocol.h"
#include "protocols/websocket_protocol.h"

#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include <memory>
#include <utility>

namespace {
// The normal hello waits remain inside the transports. This additional deadline
// bounds MQTT control evidence and an optional PONG after OpenAudioChannel returns.
constexpr int64_t kProbeBudgetUs = 6000000;

struct ProbeEvidence {
    std::mutex mutex;
    uint64_t control_attempt = 0;
    Protocol::ChannelToken channel = 0;
    bool control = false;
    bool handshake = false;
    bool media_started = false;
    bool udp_probe = false;
    bool pong = false;
    bool failed = false;
    bool retiring = false;

    void Observe(const Protocol::HealthEvent& event) {
        std::lock_guard<std::mutex> lock(mutex);
        using Stage = Protocol::HealthStage;
        using Outcome = Protocol::HealthOutcome;
        if (event.attempt_id == 0) {
            return;
        }
        if (event.stage == Stage::Control) {
            if (event.outcome == Outcome::Started) {
                if (control_attempt != event.attempt_id) {
                    control = false;
                    control_attempt = event.attempt_id;
                }
            } else if (event.attempt_id == control_attempt) {
                if (event.outcome == Outcome::Succeeded) {
                    control = true;
                } else if (event.outcome == Outcome::Failed ||
                           (event.outcome == Outcome::Retired && !retiring)) {
                    failed = true;
                }
            }
            return;
        }
        if (event.stage == Stage::Handshake && event.outcome == Outcome::Started) {
            channel = event.source_channel_token;
            handshake = media_started = udp_probe = pong = false;
        }
        if (channel == 0 || event.source_channel_token != channel) {
            return;
        }
        if (event.outcome == Outcome::Failed || (event.outcome == Outcome::Retired && !retiring)) {
            failed = true;
        } else if (event.stage == Stage::Handshake && event.outcome == Outcome::Succeeded) {
            handshake = true;
        } else if (event.stage == Stage::Media) {
            if (event.outcome == Outcome::Started) {
                media_started = true;
                udp_probe = event.udp_probe;
            } else if (event.outcome == Outcome::Succeeded && event.udp_probe) {
                pong = true;
            }
        }
    }

    bool Ready(bool mqtt) {
        std::lock_guard<std::mutex> lock(mutex);
        return !failed && (!mqtt || control) && handshake && media_started && (!udp_probe || pong);
    }

    void BeginClose() {
        std::lock_guard<std::mutex> lock(mutex);
        retiring = true;
    }

    bool Failed() {
        std::lock_guard<std::mutex> lock(mutex);
        return failed;
    }
};
}  // namespace

HomeNetworkService::~HomeNetworkService() {
    stopping_.store(true);
    generation_.store(0);
    // Do not delete a task while its HTTP/socket call owns resources or callbacks.
    while (Busy()) {
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

bool HomeNetworkService::StartRecovery(uint64_t generation) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (generation == 0 || stopping_.load() || running_ || result_) {
        return false;
    }
    generation_.store(generation);
    job_generation_ = generation;
    running_ = true;
    if (xTaskCreate(
            [](void* context) {
                static_cast<HomeNetworkService*>(context)->Run();
                vTaskDelete(nullptr);
            },
            "home_recovery", 8192, this, 2, nullptr) != pdPASS) {
        running_ = false;
        return false;
    }
    return true;
}

bool HomeNetworkService::TakeResult(Result& result) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!result_) {
        return false;
    }
    result = std::move(*result_);
    result_.reset();
    return true;
}

bool HomeNetworkService::Busy() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return running_;
}

void HomeNetworkService::Invalidate(uint64_t generation) {
    std::lock_guard<std::mutex> lock(mutex_);
    generation_.store(generation);
    if (result_ && result_->generation != generation) {
        result_->ready = false;
        result_->error = "recovery_cancelled";
    }
}

bool HomeNetworkService::Cancelled(uint64_t generation) const {
    return stopping_.load() || generation_.load() != generation;
}

void HomeNetworkService::Run() {
    uint64_t generation;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        generation = job_generation_;
    }
    auto result = Recover(generation);
    std::lock_guard<std::mutex> lock(mutex_);
    if (Cancelled(generation)) {
        result.ready = false;
        result.error = "recovery_cancelled";
    }
    result_ = std::move(result);
    running_ = false;
}

HomeNetworkService::Result HomeNetworkService::Recover(uint64_t generation) {
    Result result;
    result.generation = generation;
    const auto cancelled = [this, generation] { return Cancelled(generation); };
    if (cancelled()) {
        return result;
    }
    auto& board = Board::GetInstance();
    if (!board.IsNetworkConnected()) {
        result.error = "wifi_disconnected";
        return result;
    }

    Ota ota;
    ota.SetCancellationCheck(cancelled);
    ota.MarkCurrentVersionValid();
    const auto check = ota.CheckVersion();
    if (cancelled()) {
        return result;
    }
    if (!check) {
        result.error = check.error().ToString();
        return result;
    }
    result.has_server_time = ota.HasServerTime();
    result.use_mqtt = ota.HasMqttConfig();
    if (!board.IsNetworkConnected()) {
        result.error = "wifi_disconnected";
        return result;
    }
    if (ota.HasActivationCode() || ota.HasActivationChallenge()) {
        result.activation_message = ota.GetActivationMessage();
        if (ota.HasActivationCode()) {
            if (!result.activation_message.empty()) {
                result.activation_message += " ";
            }
            result.activation_message += ota.GetActivationCode();
        }
        if (ota.HasActivationChallenge()) {
            const auto activation = ota.Activate();
            if (activation != ESP_OK) {
                result.error = "activation_pending";
            }
        }
        // A successful activation requires a fresh config on the next recovery;
        // this job never loops or treats the old activation response as ready.
        if (result.error.empty()) {
            result.error = "activation_pending";
        }
        return result;
    }
    if (!result.use_mqtt && !ota.HasWebsocketConfig()) {
        result.error = "service_configuration_missing";
        return result;
    }
    if (cancelled() || !board.IsNetworkConnected()) {
        result.error = "wifi_disconnected";
        return result;
    }

    auto evidence = std::make_shared<ProbeEvidence>();
    std::unique_ptr<Protocol> protocol;
    if (result.use_mqtt) {
        protocol = std::make_unique<MqttProtocol>(true);
    } else {
        protocol = std::make_unique<WebsocketProtocol>();
    }
    protocol->SetRecoveryProbe();
    protocol->OnHealthEvent([evidence](const auto& event) { evidence->Observe(event); });
    // No business/audio/open/close callbacks: even unsolicited messages cannot
    // reach the application, local recognition, device actions or playback.
    if (!protocol->Start() || cancelled() || !board.IsNetworkConnected()) {
        result.error = "service_control_failed";
    } else if (!protocol->OpenAudioChannel()) {
        result.error = "service_handshake_failed";
    } else {
        const auto deadline = esp_timer_get_time() + kProbeBudgetUs;
        while (!cancelled() && board.IsNetworkConnected() && !evidence->Failed()) {
            protocol->ProcessPending();
            if (evidence->Ready(result.use_mqtt)) {
                result.ready = true;
                break;
            }
            if (esp_timer_get_time() >= deadline) {
                break;
            }
            vTaskDelay(pdMS_TO_TICKS(20));
        }
        if (!result.ready) {
            result.error = "service_probe_failed";
        }
    }
    // Ignore intentional retirement while retaining any new transport failures,
    // including a failed goodbye or a disconnect racing with the success snapshot.
    evidence->BeginClose();
    protocol->CloseAudioChannel();
    protocol.reset();
    if (evidence->Failed() || cancelled() || !board.IsNetworkConnected()) {
        result.ready = false;
        result.error = "service_probe_failed";
    }
    return result;
}
