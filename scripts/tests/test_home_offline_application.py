"""Execute HOME application methods with real checked controls and host I/O boundaries."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from test_backlight_settings import BACKLIGHT_PLATFORM, backlight_source
from test_checked_settings import NVS_PLATFORM, ROOT, compiler_command, settings_source
from test_home_volume import (
    AUDIO_PLATFORM, CODEC_PLATFORM, audio_source, box_source, vendor_volume_source,
)
from test_startup_wake_word_hint import definition


def production(path, *signatures):
    source = (ROOT / path).read_text(encoding="utf-8")
    return "\n".join(definition(source, signature) for signature in signatures)


def network_callback_source():
    source = (ROOT / "main/application.cc").read_text(encoding="utf-8")
    # Execute the registration statements from Initialize/InitializeProtocol.
    # Only their surrounding SDK startup is omitted; callback bodies remain exact.
    board_callback = definition(source, "board.SetNetworkEventCallback(")
    health_callback = definition(
        source,
        "#if CONFIG_WAVESHARE_1_85C_HOME_OFFLINE && !CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS",
    )
    return ("void Application::InstallNetworkCallbacks() {\n"
            "auto& board = Board::GetInstance();\n" + board_callback + ");\n}\n"
            "void Application::InstallHealthCallbacks() {\n"
            "const auto callback_epoch = protocol_callback_epoch_.load();\n" +
            health_callback + ");\n#endif\n}\n")


def mcp_callback_source():
    source = (ROOT / "main/application.cc").read_text(encoding="utf-8")
    source_guard = definition(
        source, "auto source_guard = [this, callback_epoch, source_intent, source_pause]()",
    )
    sender = definition(source, "[this, source_guard](const std::string& response)")
    return ("std::function<bool()> Application::MakeMcpSourceGuard() {\n"
            "const auto callback_epoch = protocol_callback_epoch_.load();\n"
            "const auto source_intent = ConversationIntentSequence();\n"
            "const auto source_pause = ListeningPauseSequence();\n" + source_guard + ";\n"
            "return source_guard;\n}\n"
            "std::function<void(const std::string&)> Application::MakeMcpSender() {\n"
            "const auto source_guard = MakeMcpSourceGuard();\nreturn " + sender + ";\n}\n")


def checked_control_source():
    # Reuse the established NVS/LEDC/codec-device adapters, retaining the actual
    # Settings, Backlight, BoxAudioCodec and vendor volume implementation.
    nvs = NVS_PLATFORM.replace(
        'assert(std::string(ns) == "audio");',
        'assert(std::string(ns) == "audio" || std::string(ns) == "display" || '
        'std::string(ns) == "home");',
    )
    return "\n".join((
        nvs, AUDIO_PLATFORM, BACKLIGHT_PLATFORM, vendor_volume_source(),
        CODEC_PLATFORM,
        production("managed_components/espressif__esp_codec_dev/esp_codec_dev.c",
                   "int esp_codec_dev_set_out_vol("),
        settings_source(), audio_source(), box_source(), backlight_source(),
    ))


ACTION_PLATFORM = r'''
#include <atomic>
#include <functional>
#include <memory>
#include <optional>
#include <utility>
#include "main/home_offline_policy.h"
#include "main/home_network_service.h"
#include "main/boards/common/battery_status.h"
#include "main/device_state.h"
namespace Lang {
namespace Strings {
const char* LISTENING_PAUSED = "监听已暂停";
const char* STANDBY = "待机";
const char* SCANNING_WIFI = "scanning";
const char* REGISTERING_NETWORK = "registering";
const char* CONNECT_TO = "connecting ";
const char* CONNECTED_TO = "connected ";
const char* DETECTING_MODULE = "detecting";
const char* ERROR = "error";
const char* PIN_ERROR = "pin";
const char* REG_ERROR = "registration";
const char* MODEM_INIT_ERROR = "modem";
}
namespace Sounds {
const char* OGG_POPUP = "popup";
const char* OGG_SUCCESS = "success";
const char* OGG_ERR_PIN = "pin";
const char* OGG_ERR_REG = "registration";
const char* OGG_EXCLAMATION = "error";
}
}
uint64_t now_ms = 100;
uint64_t HomeNow() { return now_ms; }
int64_t esp_timer_get_time() { return now_ms * 1000; }
constexpr uint32_t MAIN_EVENT_PRIMARY_BUTTON = 1, MAIN_EVENT_LISTENING_PAUSE = 2;
constexpr uint32_t MAIN_EVENT_NETWORK_CONNECTED = 4, MAIN_EVENT_NETWORK_DISCONNECTED = 8;
constexpr int kAbortReasonNone = 0;
void xEventGroupSetBits(uint32_t* group, uint32_t bits) { *group |= bits; }
std::vector<std::function<void()>> background;
bool task_create_success = true;
constexpr int pdPASS = 1;
int xTaskCreate(void (*run)(void*), const char*, int, void* arg, int, void*) {
    if (!task_create_success) return 0;
    background.push_back([=] { run(arg); });
    return pdPASS;
}
void vTaskDelete(void*) {}
void RunBackground() {
    auto pending = std::move(background);
    background.clear();
    for (auto& run : pending) run();
}
struct HomeWork { std::function<void()> run; };
struct HostNetwork {
    std::optional<HomeNetworkService::Result> result;
    bool busy = false;
    int starts = 0, invalidates = 0;
    uint64_t generation = 0;
} network;
HomeNetworkService::~HomeNetworkService() = default;
bool HomeNetworkService::StartRecovery(uint64_t generation) {
    ++network.starts; network.busy = true; network.generation = generation; return true;
}
bool HomeNetworkService::TakeResult(Result& result) {
    if (!network.result) return false;
    result = *network.result; network.result.reset(); network.busy = false; return true;
}
bool HomeNetworkService::Busy() const { return network.busy; }
void HomeNetworkService::Invalidate(uint64_t generation) {
    ++network.invalidates; network.generation = generation;
}
int timer_starts = 0, timer_stops = 0;
int esp_timer_start_once(esp_timer_handle_t timer, uint64_t delay) {
    assert(delay == 8000000);
    ++timer_starts;
    timer->active = true;
    return ESP_OK;
}
struct Display {
    std::string status, message, role, notification;
    std::vector<std::string> menu;
    int selected = -1, hit = -1;
    void SetStatus(const char* value) { status = value; }
    void SetChatMessage(const char* who, const char* value) { role = who; message = value; }
    void ShowNotification(const char* value, int = 0) { notification = value; }
    void UpdateStatusBar(bool) {}
    void ShowNetworkMenu(const std::vector<std::string>& items, int index) {
        menu = items; selected = index;
    }
    int NetworkMenuHitTest(int, int) { return hit; }
};
struct AudioService {
    bool paused = false, wake = false, voice = false, available = true;
    bool start_success = true, playback_idle = true;
    uint64_t generation = 0;
    int starts = 0, stops = 0, decoder_resets = 0;
    std::vector<std::string> sounds;
    bool HasLocalCommands() { return available; }
    bool StartLocalCommands(uint64_t value) { ++starts; generation = value; return start_success; }
    void StopLocalCommands() { ++stops; }
    void SetInputPaused(bool value) {
        paused = value;
        if (value) wake = voice = false; // ResetCapture at the AudioService boundary.
    }
    void EnableVoiceProcessing(bool value) { voice = value; }
    void EnableWakeWordDetection(bool value) { wake = value; }
    void ResetDecoder() { ++decoder_resets; }
    void PlaySound(const char* value) { sounds.push_back(value); }
    bool IsPlaybackIdle() { return playback_idle; }
};
enum class NetworkEvent {
    Scanning, Connecting, Connected, Disconnected, WifiConfigModeEnter, WifiConfigModeExit,
    ModemDetecting, ModemErrorNoSim, ModemErrorRegDenied, ModemErrorInitFailed, ModemErrorTimeout,
};
struct Board {
    Display display;
    AudioCodec* codec = nullptr;
    Backlight* backlight = nullptr;
    BatteryStatus battery;
    bool connected = false, enabled = false, configuration_success = true;
    int network_requests = 0, configuration_requests = 0;
    std::function<void(NetworkEvent, const std::string&)> network_callback;
    static Board& GetInstance() { static Board board; return board; }
    Display* GetDisplay() { return &display; }
    AudioCodec* GetAudioCodec() { return codec; }
    Backlight* GetBacklight() { return backlight; }
    BatteryStatus GetBatteryStatus() { return battery; }
    bool IsNetworkConnected() { return connected; }
    bool SetNetworkEnabled(bool value) { ++network_requests; enabled = value; return true; }
    bool RequestWifiConfiguration() { ++configuration_requests; return configuration_success; }
    void SetNetworkEventCallback(std::function<void(NetworkEvent, const std::string&)> callback) {
        network_callback = std::move(callback);
    }
    void EmitNetwork(NetworkEvent event) { assert(network_callback); network_callback(event, "test"); }
};
struct Protocol {
    using ChannelToken = uint64_t;
    enum class HealthStage { Control, Handshake, Media };
    enum class HealthOutcome { Started, Failed, Succeeded, Retired };
    struct HealthEvent {
        HealthStage stage;
        HealthOutcome outcome;
        uint64_t attempt_id;
        ChannelToken source_channel_token;
        bool udp_probe = false;
    };
    std::function<void(const HealthEvent&)> health_callback;
    bool opened = false, start_result = true, open_result = true;
    ChannelToken token = 23;
    int starts = 0, opens = 0, closes = 0, wakes = 0;
    std::vector<std::string> mcp_messages;
    bool Start() { ++starts; return start_result; }
    bool OpenAudioChannel() { ++opens; opened = open_result; return opened; }
    void CloseAudioChannel() { ++closes; opened = false; ++token; }
    bool IsAudioChannelOpened() const { return opened; }
    ChannelToken channel_token() const { return token; }
    void SendWakeWordDetected(const std::string&) { ++wakes; }
    void SendMcpMessage(const std::string& payload) { mcp_messages.push_back(payload); }
    void OnHealthEvent(std::function<void(const HealthEvent&)> callback) {
        health_callback = std::move(callback);
    }
    void EmitHealth(const HealthEvent& event) { assert(health_callback); health_callback(event); }
};
struct Application {
    HomeOfflinePolicy home_policy_;
    HomeNetworkService home_network_;
    AudioService audio_service_;
    std::atomic_bool home_assets_busy_{false}, home_configuring_{false};
    std::atomic_bool home_menu_open_{false}, home_async_busy_{false}, home_online_{false};
    std::atomic_bool home_disconnect_pending_{false};
    bool home_pending_conversation_ = false;
    bool home_use_mqtt_ = false, has_server_time_ = false;
    Protocol::ChannelToken home_health_channel_ = 0;
    bool pending_listening_start_ = false, play_popup_on_listening_ = false;
    std::atomic<uint32_t> home_network_epoch_{1};
    uint64_t home_front_deadline_ = 15100;
    std::string home_pending_wake_;
    std::atomic<uint32_t> pause_requests_{0}, processed_pause_requests_{0};
    std::atomic<uint32_t> primary_button_sequence_{0}, conversation_intent_{0};
    std::atomic<uint32_t> protocol_callback_epoch_{0}, accepted_channel_intent_{0};
    std::atomic<bool> listening_paused_{false}, conversation_events_enabled_{false};
    std::atomic<uint64_t> accepted_channel_token_{0};
    std::mutex audio_receive_mutex_;
    std::shared_ptr<Protocol> protocol_;
    std::function<void(const std::string&)> mcp_broadcast_callback_;
    uint32_t event_bits = 0;
    uint32_t* event_group_ = &event_bits;
    int listening_starts = 0, protocol_initializations = 0, asset_update_checks = 0;
    std::vector<std::function<void()>> scheduled;
    std::string home_reason_;
    DeviceState state = kDeviceStateOffline;
    esp_timer_handle_t home_command_timer_ = nullptr;
    void* activation_task_handle_ = nullptr;
    bool IsListeningPaused() const;
    uint32_t ListeningPauseSequence() const;
    uint32_t ConversationIntentSequence() const;
    bool ApplyPendingListeningPause();
    void ToggleListeningPause();
    void PrimaryButtonPressed();
    void HandlePrimaryButtonEvent();
    void InstallNetworkCallbacks();
    void InstallHealthCallbacks();
    std::function<bool()> MakeMcpSourceGuard();
    std::function<void(const std::string&)> MakeMcpSender();
    void SendMcpMessage(const std::string&, std::function<bool()> source_guard = nullptr);
    void HandleNetworkDisconnectedEvent();
    void HandleNetworkConnectedEvent();
    void HomeProtocolFailure(uint32_t, const Protocol::HealthEvent&);
    void ActivationTask() { assert(false && "HOME recovery must not run legacy activation"); }
    void Alert(const char*, const char*, const char*, const char*) {}
    void AbortSpeaking(int) { assert(false && "legacy branch must not handle HOME inputs"); }
    void HandleToggleChatEvent() { assert(false && "legacy branch must not handle HOME inputs"); }
    void Schedule(std::function<void()> run) { scheduled.push_back(std::move(run)); }
    void Drain() {
        auto pending = std::move(scheduled); scheduled.clear();
        for (auto& run : pending) run();
    }
    void EndWaitingResponseTurn() {}
    void StopNotification() {}
    void InitializeProtocol() { ++protocol_initializations; protocol_ = std::make_shared<Protocol>(); }
    bool AcceptConversationChannel();
    int GetDefaultListeningMode() { return 0; }
    void SetListeningMode(int) { ++listening_starts; SetDeviceState(kDeviceStateListening); }
    DeviceState GetDeviceState() const { return state; }
    void SetDeviceState(DeviceState value) { state = value; }
    void HomeRestoreStandby();
    void HomeShowMenu();
    void HomeHandleCommand(const std::string&, uint64_t);
    bool HomeAllowsConversation() const;
    void HomeCancelInteraction();
    void HomeRetireProtocol();
    void HomeSetManualOffline();
    bool HandleHomePrimaryButton();
    void NetworkMenuPressed();
    bool HandleHomeTouch(int, int);
    void HomeExecuteMenu();
    void HomeStartCommand();
    void HomeStartConversation(const std::string& wake = "");
    void TickHomeOffline();
    void HomeStartAssetUpdate() { ++asset_update_checks; }
};
struct Fixture {
    BoxAudioCodec codec;
    PwmBacklight backlight{1};
    Application app;
    Fixture(int volume = 37, int brightness = 47) {
        saved["output_volume"] = volume;
        codec.Start();
        codec.EnableOutput(true);
        assert(backlight.SetBrightnessChecked(brightness).error == ESP_OK);
        auto& board = Board::GetInstance();
        board.codec = &codec;
        board.backlight = &backlight;
        app.home_policy_.Start(true, now_ms);
        ClearObservations();
    }
    void ClearObservations() {
        sets = commits = 0;
        app.audio_service_.sounds.clear();
        Board::GetInstance().display.message.clear();
    }
    void Command(const std::string& action) {
        auto generation = app.home_policy_.BeginCommand(now_ms);
        app.HomeHandleCommand(action, generation);
    }
    void Completed() {
        assert(!app.home_policy_.CommandActive());
        assert(app.state == kDeviceStateOffline);
        assert(app.audio_service_.stops == 1);
        assert(!app.audio_service_.paused && app.audio_service_.wake);
    }
    void Failed() {
        Completed();
        assert(Board::GetInstance().display.message.find("失败") != std::string::npos);
        assert(app.audio_service_.sounds.empty());
    }
};
'''


ACTION_CHECKS = r'''
void MakeOnline(Application& app) {
    app.home_policy_.AllowNetwork(now_ms);
    app.home_policy_.RecoverySucceeded();
    assert(app.home_policy_.ApplyRecovery());
    app.home_online_ = true;
    app.state = kDeviceStateIdle;
}
void Pause(Application& app) {
    app.ToggleListeningPause();
    assert(app.ApplyPendingListeningPause());
    assert(app.IsListeningPaused());
}
void NetworkScenario(const std::string& scenario) {
    Fixture f;
    auto& app = f.app;
    auto& board = Board::GetInstance();
    board.connected = true;
    app.InstallNetworkCallbacks();
    if (scenario.rfind("disconnect-", 0) == 0 || scenario == "quick-reconnect") {
        MakeOnline(app);
        app.InitializeProtocol();
        auto old_protocol = app.protocol_;
        old_protocol->opened = true;
        assert(app.AcceptConversationChannel());
        app.state = scenario == "disconnect-speaking" ? kDeviceStateSpeaking :
                    scenario == "disconnect-connecting" ? kDeviceStateConnecting :
                    scenario == "disconnect-idle" ? kDeviceStateIdle : kDeviceStateListening;
        const auto previous_state = app.state;
        const auto previous_epoch = app.home_network_epoch_.load();
        const auto intent = app.ConversationIntentSequence();
        HomeNetworkService::Result stale;
        stale.ready = true;
        stale.generation = previous_epoch;
        network.result = stale;
        board.connected = false;
        board.EmitNetwork(NetworkEvent::Disconnected);
        // Invalidation belongs to the network callback, before any main-task dispatch.
        assert(app.home_disconnect_pending_ && network.invalidates == 1);
        assert(app.home_network_epoch_ == previous_epoch + 1);
        assert(network.generation == app.home_network_epoch_);
        assert(app.state == previous_state && app.protocol_ == old_protocol);
        if (scenario == "quick-reconnect") {
            board.connected = true;
            board.EmitNetwork(NetworkEvent::Connected);
            assert((app.event_bits & MAIN_EVENT_NETWORK_CONNECTED) != 0);
            assert((app.event_bits & MAIN_EVENT_NETWORK_DISCONNECTED) != 0);
            // Run() processes Connected before Disconnected when both bits are set.
            app.HandleNetworkConnectedEvent();
            assert(!app.home_disconnect_pending_);
            app.HandleNetworkDisconnectedEvent();
        } else {
            app.HandleNetworkDisconnectedEvent();
        }
        assert(!app.home_disconnect_pending_ && !app.home_online_);
        assert(app.state == kDeviceStateOffline && !app.protocol_);
        assert(app.home_policy_.mode() == HomeOfflinePolicy::Mode::AutomaticOffline);
        assert(app.ConversationIntentSequence() != intent && !app.conversation_events_enabled_);
        assert(app.accepted_channel_token_ == 0 && app.listening_starts == 0);
        assert(app.audio_service_.wake && !app.audio_service_.voice);
        RunBackground(); app.Drain();
        assert(old_protocol->closes == 1 && !old_protocol->opened);
        app.TickHomeOffline();
        assert(!app.home_online_); // The ready result predates the callback invalidation.
        if (scenario == "quick-reconnect") {
            assert(network.starts == 1 && network.generation == app.home_network_epoch_);
            assert(app.state == kDeviceStateOffline && app.listening_starts == 0);
        }
    } else if (scenario == "local-window-reconnect") {
        app.home_policy_.AllowNetwork(now_ms);
        app.HomeStartCommand();
        const auto generation = app.audio_service_.generation;
        const auto stops = app.audio_service_.stops;
        board.connected = false;
        board.EmitNetwork(NetworkEvent::Disconnected);
        app.HandleNetworkDisconnectedEvent();
        assert(app.state == kDeviceStateLocalCommand && app.home_policy_.CommandActive());
        assert(app.audio_service_.stops == stops && !app.audio_service_.paused);
        board.connected = true;
        board.EmitNetwork(NetworkEvent::Connected);
        app.HandleNetworkConnectedEvent();
        assert(app.state == kDeviceStateLocalCommand && app.home_policy_.CommandActive());
        assert(app.audio_service_.generation == generation && app.audio_service_.stops == stops);
        app.HomeHandleCommand("volume_up", generation);
        assert(f.codec.output_volume() == 47 && sets == 1);
        assert(app.state == kDeviceStateOffline && app.listening_starts == 0);
    } else if (scenario == "manual-network-event") {
        board.connected = false;
        board.EmitNetwork(NetworkEvent::Disconnected);
        app.HandleNetworkDisconnectedEvent();
        assert(!app.home_disconnect_pending_);
        assert(app.home_policy_.mode() == HomeOfflinePolicy::Mode::ManualOffline);
        board.connected = true;
        board.EmitNetwork(NetworkEvent::Connected);
        app.HandleNetworkConnectedEvent();
        assert(app.home_policy_.mode() == HomeOfflinePolicy::Mode::ManualOffline);
        assert(network.starts == 0 && board.network_requests == 0 && sets == 0);
    } else if (scenario == "scanning-is-not-disconnect") {
        MakeOnline(app);
        const auto epoch = app.home_network_epoch_.load();
        board.EmitNetwork(NetworkEvent::Scanning);
        assert(!app.home_disconnect_pending_ && network.invalidates == 0);
        assert(app.home_network_epoch_ == epoch && app.home_online_);
        assert((app.event_bits & MAIN_EVENT_NETWORK_DISCONNECTED) == 0);
    } else {
        MakeOnline(app);
        app.InitializeProtocol();
        auto protocol = app.protocol_;
        protocol->opened = true;
        app.state = kDeviceStateListening;
        assert(app.AcceptConversationChannel());
        app.InstallHealthCallbacks();
        const auto handshake_token = protocol->channel_token();
        protocol->EmitHealth({Protocol::HealthStage::Handshake, Protocol::HealthOutcome::Started,
                              1, handshake_token});
        assert(app.scheduled.size() == 1 && app.home_health_channel_ == 0);
        app.Drain();
        assert(app.home_health_channel_ == handshake_token && app.home_online_);
        Protocol::HealthEvent event{Protocol::HealthStage::Media, Protocol::HealthOutcome::Failed,
                                    1, protocol->channel_token()};
        const auto epoch = app.protocol_callback_epoch_.load();
        if (scenario == "health-handshake-failure" || scenario == "health-stale-handshake" ||
            scenario == "health-close-before-failure")
            event.stage = Protocol::HealthStage::Handshake;
        if (scenario == "health-control-failure") {
            event.stage = Protocol::HealthStage::Control;
            event.source_channel_token = 0; // MQTT control has independent provenance.
        }
        if (scenario == "health-stale-token" || scenario == "health-stale-handshake") {
            ++protocol->token;
            protocol->EmitHealth({Protocol::HealthStage::Handshake, Protocol::HealthOutcome::Started,
                                  2, protocol->channel_token()});
            app.Drain();
            assert(app.home_health_channel_ != event.source_channel_token);
        }
        if (scenario == "health-close-before-failure") {
            // WebSocket retires/advances its transport token before reporting
            // failure for the handshake source. That failure must still degrade.
            protocol->CloseAudioChannel();
            assert(protocol->channel_token() != event.source_channel_token);
            assert(app.home_health_channel_ == event.source_channel_token);
        }
        if (scenario == "health-non-failure") {
            for (auto outcome : {Protocol::HealthOutcome::Started, Protocol::HealthOutcome::Succeeded,
                                 Protocol::HealthOutcome::Retired}) {
                event.outcome = outcome;
                app.HomeProtocolFailure(epoch, event);
                assert(app.home_online_ && app.protocol_ == protocol);
                protocol->EmitHealth(event);
                assert(app.scheduled.size() == 1); // The callback forwards every outcome.
                app.Drain();
                assert(app.home_online_ && app.protocol_ == protocol);
            }
            return;
        }
        protocol->EmitHealth(event);
        assert(app.scheduled.size() == 1 && app.home_online_ && app.state == kDeviceStateListening);
        if (scenario == "health-stale-epoch") ++app.protocol_callback_epoch_;
        if (scenario == "health-manual-offline") {
            app.home_policy_.ManualOffline();
            app.home_online_ = false;
        }
        app.Drain();
        if (scenario == "health-stale-token" || scenario == "health-stale-handshake" ||
            scenario == "health-stale-epoch" || scenario == "health-manual-offline") {
            assert(app.protocol_ == protocol && app.state == kDeviceStateListening);
            assert(app.home_policy_.mode() == (scenario == "health-manual-offline" ?
                   HomeOfflinePolicy::Mode::ManualOffline : HomeOfflinePolicy::Mode::Online));
            assert(background.empty());
        } else {
            assert(scenario == "health-media-failure" || scenario == "health-handshake-failure" ||
                   scenario == "health-control-failure" || scenario == "health-close-before-failure");
            assert(!app.home_online_ && !app.protocol_ && app.state == kDeviceStateOffline);
            assert(app.home_policy_.mode() == HomeOfflinePolicy::Mode::AutomaticOffline);
            assert(app.home_reason_ == "语音服务不可用");
            assert(!app.home_policy_.CanRecover(now_ms + 9999));
            assert(app.home_policy_.CanRecover(now_ms + 10000));
            assert(!app.conversation_events_enabled_ && app.audio_service_.wake);
            assert(!app.audio_service_.voice && app.listening_starts == 0);
            RunBackground(); app.Drain();
            assert(protocol->closes == (scenario == "health-close-before-failure" ? 2 : 1));
            assert(!protocol->opened);
        }
    }
}
void McpScenario(const std::string& scenario) {
    Fixture f;
    auto& app = f.app;
    auto& board = Board::GetInstance();
    MakeOnline(app);
    app.InitializeProtocol();
    auto original = app.protocol_;
    original->opened = true;
    assert(app.AcceptConversationChannel());
    std::vector<std::string> broadcasts;
    app.mcp_broadcast_callback_ = [&](const std::string& response) { broadcasts.push_back(response); };
    const auto source_guard = app.MakeMcpSourceGuard();
    const auto sender = app.MakeMcpSender(); // The actual OnIncomingJson custom sender.
    assert(source_guard());
    sender("queued response");
    assert(app.scheduled.size() == 1 && original->mcp_messages.empty() && broadcasts.empty());
    if (scenario == "cancel-replace") {
        app.HomeCancelInteraction();
        app.InitializeProtocol();
        auto replacement = app.protocol_;
        replacement->opened = true;
        assert(app.AcceptConversationChannel());
        assert(replacement != original && !source_guard());
        RunBackground(); app.Drain();
        assert(original->mcp_messages.empty() && replacement->mcp_messages.empty());
        assert(broadcasts.empty());
        const auto fresh_sender = app.MakeMcpSender();
        fresh_sender("current response");
        app.Drain();
        assert(replacement->mcp_messages == std::vector<std::string>{"current response"});
        assert(broadcasts == std::vector<std::string>{"current response"});
    } else if (scenario == "disconnect-pending") {
        app.InstallNetworkCallbacks();
        board.connected = false;
        board.EmitNetwork(NetworkEvent::Disconnected);
        // The network callback blocks a queued response before the main-task
        // disconnect handler has retired its still-current protocol.
        assert(app.home_disconnect_pending_ && app.protocol_ == original);
        assert(!app.HomeAllowsConversation() && !source_guard());
        app.Drain();
        assert(original->mcp_messages.empty() && broadcasts.empty());
        app.HandleNetworkDisconnectedEvent();
        RunBackground(); app.Drain();
    } else if (scenario == "current") {
        app.Drain();
        assert(original->mcp_messages == std::vector<std::string>{"queued response"});
        assert(broadcasts == std::vector<std::string>{"queued response"});
        app.SendMcpMessage("default guard"); // Existing callers keep the optional-guard API.
        app.Drain();
        assert(original->mcp_messages.back() == "default guard" && broadcasts.back() == "default guard");
        assert(original->mcp_messages.size() == 2 && broadcasts.size() == 2);
    } else {
        assert(false && "unknown MCP scenario");
    }
}
void InteractionScenario(const std::string& scenario) {
    Fixture f;
    auto& app = f.app;
    auto& board = Board::GetInstance();
    if (scenario == "primary-window-cancel") {
        assert(app.HandleHomePrimaryButton());
        assert(app.home_policy_.CommandActive() && app.state == kDeviceStateLocalCommand);
        assert(app.audio_service_.starts == 1 && app.audio_service_.generation != 0);
        assert(!app.audio_service_.paused && !app.audio_service_.wake);
        const auto generation = app.audio_service_.generation;
        assert(app.HandleHomePrimaryButton());
        assert(!app.home_policy_.CommandActive() && app.state == kDeviceStateOffline);
        assert(app.audio_service_.wake && app.protocol_initializations == 0);
        app.HomeHandleCommand("volume_up", generation);
        assert(sets == 0 && f.codec.output_volume() == 37);
        app.HandleHomePrimaryButton();
        assert(app.audio_service_.generation != generation && app.audio_service_.starts == 2);
    } else if (scenario == "window-unavailable" || scenario == "window-start-failure") {
        if (scenario == "window-unavailable") app.audio_service_.available = false;
        else app.audio_service_.start_success = false;
        app.HomeStartCommand();
        assert(!app.home_policy_.CommandActive());
        assert(board.display.notification == "本地语音不可用");
        assert(board.display.status != "请说指令");
        assert(app.audio_service_.wake && !app.audio_service_.paused);
    } else if (scenario == "window-timeout") {
        HostTimer timer{nullptr, nullptr};
        app.home_command_timer_ = &timer;
        app.HomeStartCommand();
        const auto generation = app.audio_service_.generation;
        assert(timer_starts == 1 && timer.active);
        now_ms += 8000;
        app.TickHomeOffline();
        assert(!timer.active && !app.home_policy_.CommandActive());
        app.HomeHandleCommand("volume_up", generation);
        assert(sets == 0 && f.codec.output_volume() == 37 && app.audio_service_.wake);
        app.home_command_timer_ = nullptr;
    } else if (scenario == "window-one-result") {
        app.HomeStartCommand();
        const auto generation = app.audio_service_.generation;
        app.HomeHandleCommand("volume_up", generation);
        app.HomeHandleCommand("volume_down", generation);
        assert(sets == 1 && f.codec.output_volume() == 47);
    } else if (scenario == "pause-precedes-command") {
        app.HomeStartCommand();
        const auto generation = app.audio_service_.generation;
        app.ToggleListeningPause(); // Pending request already blocks a queued recognition.
        app.HomeHandleCommand("volume_up", generation);
        assert(sets == 0);
        assert(app.ApplyPendingListeningPause());
        app.HandleHomePrimaryButton();
        assert(!app.home_policy_.CommandActive() && app.audio_service_.starts == 1);
        assert(app.audio_service_.paused && !app.audio_service_.wake);
    } else if (scenario == "menu-paused-primary") {
        Pause(app);
        app.NetworkMenuPressed();
        assert(app.home_policy_.Selected() == app.home_policy_.MenuSize() - 1);
        app.event_bits = 0;
        app.PrimaryButtonPressed();
        assert((app.event_bits & MAIN_EVENT_PRIMARY_BUTTON) != 0);
        app.HandlePrimaryButtonEvent();
        assert(app.home_policy_.Selected() == 0);
        assert(app.audio_service_.paused && app.audio_service_.starts == 0);
    } else if (scenario == "primary-stale-after-pause") {
        app.PrimaryButtonPressed();
        assert((app.event_bits & MAIN_EVENT_PRIMARY_BUTTON) != 0);
        Pause(app);
        app.ToggleListeningPause();
        assert(app.ApplyPendingListeningPause() && !app.IsListeningPaused());
        // The pending click belongs to the interaction before both pause requests.
        app.HandlePrimaryButtonEvent();
        assert(app.audio_service_.starts == 0 && !app.home_policy_.CommandActive());
    } else if (scenario == "menu-pause-current-choice") {
        app.NetworkMenuPressed();
        assert(app.home_policy_.MenuOpen());
        Pause(app);
        assert(app.home_policy_.MenuOpen() && app.audio_service_.paused);
        app.ToggleListeningPause();
        assert(app.ApplyPendingListeningPause());
        assert(!app.IsListeningPaused() && app.audio_service_.paused && !app.audio_service_.wake);
        app.HomeExecuteMenu(); // Default Back resumes the current pause choice.
        assert(!app.home_policy_.MenuOpen() && app.audio_service_.wake);
        assert(!app.home_policy_.CommandActive() && app.audio_service_.starts == 0);
    } else if (scenario == "menu-cancels-window") {
        app.HomeStartCommand();
        const auto generation = app.audio_service_.generation;
        app.NetworkMenuPressed();
        assert(app.home_policy_.MenuOpen() && !app.home_policy_.CommandActive());
        app.HomeHandleCommand("volume_up", generation);
        assert(sets == 0 && app.audio_service_.paused);
        app.NetworkMenuPressed(); // A second long press confirms default Back.
        assert(!app.home_policy_.MenuOpen() && !app.home_policy_.CommandActive());
        assert(app.audio_service_.starts == 1 && app.audio_service_.wake);
    } else if (scenario == "menu-all-modes") {
        for (int mode = 0; mode < 3; ++mode) {
            if (mode == 0) MakeOnline(app);
            else if (mode == 1) { app.home_policy_.AllowNetwork(now_ms); app.home_online_ = false; }
            else { app.home_policy_.ManualOffline(); app.home_online_ = false; }
            const auto original = app.home_policy_.mode();
            app.NetworkMenuPressed();
            assert(app.home_policy_.MenuOpen());
            assert(board.display.menu.size() == (mode == 1 ? 4 : 3));
            assert(board.display.menu.back() == "返回");
            assert(board.display.selected == app.home_policy_.MenuSize() - 1);
            assert(app.audio_service_.paused && !app.audio_service_.wake);
            app.HandleHomePrimaryButton();
            assert(app.home_policy_.Selected() == 0 && app.audio_service_.starts == 0);
            app.home_policy_.Select(app.home_policy_.MenuSize() - 1);
            app.HomeExecuteMenu();
            assert(!app.home_policy_.MenuOpen() && board.display.menu.empty());
            assert(app.home_policy_.mode() == original && app.audio_service_.wake);
            assert(sets == 0 && !f.backlight.display_off());
        }
    } else if (scenario == "menu-manual-offline") {
        MakeOnline(app);
        Pause(app);
        app.NetworkMenuPressed();
        app.home_policy_.Select(0);
        app.HomeExecuteMenu();
        assert(app.home_policy_.mode() == HomeOfflinePolicy::Mode::ManualOffline);
        assert(saved.at("offline") == 1 && !board.enabled && board.network_requests == 1);
        assert(app.IsListeningPaused() && app.audio_service_.paused && !app.audio_service_.wake);
    } else if (scenario == "menu-restore" || scenario == "menu-save-failure") {
        Pause(app);
        app.NetworkMenuPressed();
        app.home_policy_.Select(0);
        if (scenario == "menu-save-failure") commit_results = {91};
        app.HomeExecuteMenu();
        if (scenario == "menu-save-failure") {
            assert(board.network_requests == 0 && network.invalidates == 0);
            assert(app.home_policy_.mode() == HomeOfflinePolicy::Mode::ManualOffline);
            assert(board.display.notification == "联网设置保存失败");
        } else {
            assert(saved.at("offline") == 0 && board.enabled && board.network_requests == 1);
            assert(app.home_policy_.mode() == HomeOfflinePolicy::Mode::AutomaticOffline);
            assert(network.invalidates == 1);
        }
        assert(app.IsListeningPaused() && app.audio_service_.paused && !app.audio_service_.wake);
    } else if (scenario == "menu-configure" || scenario == "menu-configure-failure") {
        board.configuration_success = scenario != "menu-configure-failure";
        app.NetworkMenuPressed();
        app.home_policy_.Select(app.home_policy_.MenuSize() - 2);
        app.HomeExecuteMenu();
        assert(saved.at("offline") == 0 && board.configuration_requests == 1);
        assert(!app.home_policy_.MenuOpen() && app.audio_service_.starts == 0);
        if (board.configuration_success) {
            assert(app.home_configuring_ && app.state == kDeviceStateWifiConfiguring);
            assert(app.audio_service_.paused && !app.audio_service_.wake);
        } else {
            assert(!app.home_configuring_ && app.state == kDeviceStateOffline);
            assert(app.home_reason_ == "配网不可用" && app.audio_service_.wake);
        }
    } else if (scenario == "menu-touch-once") {
        MakeOnline(app);
        app.NetworkMenuPressed();
        board.display.hit = 0;
        assert(app.HandleHomeTouch(100, 100));
        assert(!app.HandleHomeTouch(100, 100));
        assert(saved.at("offline") == 1 && sets == 1 && board.network_requests == 1);
    } else if (scenario == "conversation-async-cancel") {
        MakeOnline(app);
        app.HomeStartConversation();
        auto opened = app.protocol_;
        assert(app.home_async_busy_ && background.size() == 1);
        assert(app.state == kDeviceStateConnecting && opened->opens == 0);
        app.HandleHomePrimaryButton();
        assert(!app.protocol_ && app.state == kDeviceStateIdle && app.listening_starts == 0);
        app.HomeStartConversation("queued");
        assert(app.home_pending_conversation_ && app.state == kDeviceStateConnecting);
        app.HandleHomePrimaryButton();
        assert(!app.home_pending_conversation_ && app.state == kDeviceStateIdle);
        RunBackground();
        app.Drain();
        assert(!app.home_async_busy_ && app.listening_starts == 0);
        assert(opened->closes >= 1 && !opened->opened);
        app.HomeStartConversation("hello");
        auto fresh = app.protocol_;
        assert(fresh != opened && app.home_async_busy_);
        RunBackground();
        assert(app.listening_starts == 0);
        app.Drain();
        assert(app.listening_starts == 1 && app.state == kDeviceStateListening);
        assert(fresh->wakes == 1 && app.conversation_events_enabled_);
        app.HandleHomePrimaryButton();
        assert(app.state == kDeviceStateIdle && !app.conversation_events_enabled_);
        RunBackground(); app.Drain();
    } else if (scenario == "recovery-media-unknown" || scenario == "recovery-stale" ||
               scenario == "recovery-waits-window" || scenario == "recovery-waits-feedback" ||
               scenario == "recovery-waits-menu") {
        app.home_policy_.AllowNetwork(now_ms);
        board.connected = true;
        HomeNetworkService::Result result;
        result.generation = scenario == "recovery-stale" ? app.home_network_epoch_.load() - 1 : app.home_network_epoch_.load();
        result.ready = true;
        result.use_mqtt = true;
        network.result = result;
        if (scenario == "recovery-waits-window") app.HomeStartCommand();
        if (scenario == "recovery-waits-feedback") app.audio_service_.playback_idle = false;
        if (scenario == "recovery-waits-menu") app.NetworkMenuPressed();
        app.TickHomeOffline();
        if (scenario == "recovery-stale") {
            assert(!app.home_online_ && app.home_policy_.mode() == HomeOfflinePolicy::Mode::AutomaticOffline);
        } else {
            if (scenario != "recovery-media-unknown") {
                assert(!app.home_online_ && app.home_policy_.mode() == HomeOfflinePolicy::Mode::AutomaticOffline);
                if (scenario == "recovery-waits-window") app.HandleHomePrimaryButton();
                if (scenario == "recovery-waits-feedback") app.audio_service_.playback_idle = true;
                if (scenario == "recovery-waits-menu") app.HomeExecuteMenu();
                app.TickHomeOffline();
            }
            assert(app.home_online_ && app.home_policy_.mode() == HomeOfflinePolicy::Mode::Online);
            assert(app.state == kDeviceStateIdle && app.home_use_mqtt_);
            assert(app.protocol_initializations == 0 && app.listening_starts == 0);
            assert(!app.audio_service_.voice && background.empty());
        }
    } else {
        assert(false && "unknown interaction scenario");
    }
}
int main(int argc, char** argv) {
    assert(argc == 2);
    const std::string scenario = argv[1];
    if (scenario.rfind("interaction:", 0) == 0) {
        InteractionScenario(scenario.substr(12));
        return 0;
    }
    if (scenario.rfind("network:", 0) == 0) {
        NetworkScenario(scenario.substr(8));
        return 0;
    }
    if (scenario.rfind("mcp:", 0) == 0) {
        McpScenario(scenario.substr(4));
        return 0;
    }
    const bool volume_max = scenario == "volume-max";
    const bool volume_min = scenario == "volume-min";
    const bool brightness_max = scenario == "brightness-max";
    const bool brightness_min = scenario == "brightness-min";
    Fixture f(volume_max ? 100 : volume_min ? 0 : 37,
              brightness_max ? 100 : brightness_min ? 1 : 47);
    auto& board = Board::GetInstance();
    if (scenario == "screen_on") {
        assert(f.backlight.SetDisplayOff(true) == ESP_OK);
        f.Command(scenario);
        assert(!f.backlight.display_off() && f.backlight.brightness() == 47);
        assert(board.display.message == "已打开屏幕" && sets == 0);
    } else if (scenario == "screen_off") {
        f.Command(scenario);
        assert(f.backlight.display_off() && f.backlight.brightness() == 0);
        assert(f.backlight.GetStatus().brightness == 47 && sets == 0);
        assert(board.display.message == "已关闭屏幕");
    } else if (scenario == "volume_up" || scenario == "volume_down") {
        f.Command(scenario);
        const int expected = scenario == "volume_up" ? 47 : 27;
        assert(f.codec.output_volume() == expected && hardware_volume == expected);
        assert(saved.at("output_volume") == expected && sets == 1);
        assert(board.display.message.find(std::to_string(expected)) != std::string::npos);
    } else if (scenario == "brightness_up" || scenario == "brightness_down") {
        assert(f.backlight.SetDisplayOff(true) == ESP_OK);
        f.Command(scenario);
        const int expected = scenario == "brightness_up" ? 57 : 37;
        assert(saved.at("brightness") == expected && sets == 1);
        assert(f.backlight.GetStatus().brightness == expected);
        assert(f.backlight.display_off() && f.backlight.brightness() == 0);
        assert(board.display.message.find(std::to_string(expected)) != std::string::npos);
        assert(board.display.message.find("息屏") != std::string::npos);
    } else if (scenario == "device_status") {
        assert(f.backlight.SetDisplayOff(true) == ESP_OK);
        f.Command(scenario);
        assert(!f.backlight.display_off());
        assert(board.display.message.find("电池 未知") != std::string::npos);
        assert(board.display.message.find("音量 37") != std::string::npos);
        assert(board.display.message.find("亮度 47") != std::string::npos);
        assert(board.display.message.find("手动离线") != std::string::npos);
        assert(sets == 0);
    } else if (scenario == "cancel") {
        f.Command(scenario);
        assert(board.display.message == "已取消" && sets == 0);
        assert(f.app.audio_service_.sounds.empty());
    } else if (volume_max || volume_min || brightness_max || brightness_min) {
        f.Command(volume_max ? "volume_up" : volume_min ? "volume_down" :
                  brightness_max ? "brightness_up" : "brightness_down");
        assert(sets == 0 && commits == 0); // A clamped no-op must not rewrite NVS.
        assert(f.codec.output_volume() == (volume_max ? 100 : volume_min ? 0 : 37));
        assert(f.backlight.GetStatus().brightness == (brightness_max ? 100 : brightness_min ? 1 : 47));
        assert(!board.display.message.empty());
    } else if (scenario == "unknown") {
        f.Command("reboot");
        assert(sets == 0 && board.network_requests == 0);
        f.Failed();
        return 0;
    } else if (scenario == "missing-controls") {
        board.codec = nullptr;
        board.backlight = nullptr;
        f.Command("device_status");
        assert(board.display.message.find("音量 未知") != std::string::npos);
        assert(board.display.message.find("亮度 未知") != std::string::npos);
        assert(sets == 0);
    } else if (scenario == "unsupported-volume" || scenario == "unsupported-screen") {
        if (scenario == "unsupported-volume") board.codec = nullptr;
        else board.backlight = nullptr;
        f.Command(scenario == "unsupported-volume" ? "volume_up" : "screen_on");
        f.Failed();
        return 0;
    } else if (scenario == "volume-hardware-failure" || scenario == "volume-storage-failure") {
        if (scenario == "volume-hardware-failure") volume_results = {81};
        else commit_results = {82};
        f.Command("volume_up");
        assert(f.codec.output_volume() == 37 && saved.at("output_volume") == 37);
        f.Failed();
        return 0;
    } else if (scenario == "brightness-hardware-failure" || scenario == "brightness-storage-failure") {
        if (scenario == "brightness-hardware-failure") duty_results = {83};
        else commit_results = {84};
        f.Command("brightness_up");
        assert(f.backlight.GetStatus().brightness == 47 && saved.at("brightness") == 47);
        f.Failed();
        return 0;
    } else if (scenario == "screen-hardware-failure") {
        duty_results = {85};
        f.Command("screen_off");
        assert(!f.backlight.display_off());
        f.Failed();
        return 0;
    } else if (scenario == "volume-deferred") {
        f.codec.EnableOutput(false);
        f.Command("volume_up");
        assert(f.codec.output_volume() == 47 && saved.at("output_volume") == 47);
        assert(!f.codec.GetOutputVolumeStatus().applied);
        assert(board.display.message.find("已保存") != std::string::npos);
        assert(board.display.message.find("尚未应用") != std::string::npos);
    } else {
        assert(false && "unknown scenario");
    }
    f.Completed();
}
'''


class HomeOfflineApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        path = Path(cls.temp.name)
        source = path / "home_actions.cc"
        source.write_text(checked_control_source() + ACTION_PLATFORM + production(
            "main/application_home.cc", "bool RunHomeWork(",
            "bool Application::HomeAllowsConversation() const",
            "void Application::HomeRestoreStandby()", "void Application::HomeShowMenu()",
            "void Application::HomeHandleCommand(", "void Application::HomeCancelInteraction()",
            "void Application::HomeRetireProtocol()", "void Application::HomeSetManualOffline()",
            "bool Application::HandleHomePrimaryButton()", "void Application::NetworkMenuPressed()",
            "bool Application::HandleHomeTouch(", "void Application::HomeExecuteMenu()",
            "void Application::HomeStartCommand()", "void Application::HomeStartConversation(",
            "void Application::TickHomeOffline()",
            "void Application::HomeProtocolFailure(",
        ) + production(
            "main/application.cc", "void Application::PrimaryButtonPressed()",
            "void Application::HandlePrimaryButtonEvent()",
            "bool Application::IsListeningPaused() const", "void Application::ToggleListeningPause()",
            "uint32_t Application::ListeningPauseSequence() const",
            "uint32_t Application::ConversationIntentSequence() const",
            "bool Application::ApplyPendingListeningPause()",
            "bool Application::AcceptConversationChannel()",
            "void Application::HandleNetworkConnectedEvent()",
            "void Application::HandleNetworkDisconnectedEvent()",
            "void Application::SendMcpMessage(",
        ) + network_callback_source() + mcp_callback_source() + ACTION_CHECKS, encoding="utf-8")
        cls.binary = path / "home_actions"
        compiled = subprocess.run(
            compiler_command() + ["-std=c++20", "-pthread", "-fsanitize=undefined",
                                  "-fno-sanitize-recover=undefined",
                                  "-DCONFIG_WAVESHARE_1_85C_HOME=1",
                                  "-DCONFIG_WAVESHARE_1_85C_HOME_OFFLINE=1",
                                  "-DCONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS=0",
                                  "-I", str(ROOT), str(source), "-o", str(cls.binary)],
            capture_output=True, text=True, timeout=45,
        )
        if compiled.returncode:
            raise AssertionError(compiled.stdout + compiled.stderr)

    def run_scenario(self, scenario):
        executed = subprocess.run([str(self.binary), scenario], capture_output=True,
                                  text=True, timeout=10)
        self.assertEqual(executed.returncode, 0, executed.stdout + executed.stderr)

    def test_all_eight_actions_use_checked_controls_and_actual_values(self):
        for action in ("screen_on", "screen_off", "volume_up", "volume_down",
                       "brightness_up", "brightness_down", "device_status", "cancel"):
            with self.subTest(action=action):
                self.run_scenario(action)

    def test_clamped_boundaries_do_not_repeat_storage_writes(self):
        for scenario in ("volume-max", "volume-min", "brightness-max", "brightness-min"):
            with self.subTest(scenario=scenario):
                self.run_scenario(scenario)

    def test_failed_checked_actions_do_not_announce_success_for_rolled_back_values(self):
        for scenario in ("volume-hardware-failure", "volume-storage-failure",
                         "brightness-hardware-failure", "brightness-storage-failure",
                         "screen-hardware-failure"):
            with self.subTest(scenario=scenario):
                self.run_scenario(scenario)

    def test_unknown_and_unsupported_actions_cannot_report_success(self):
        for scenario in ("unknown", "unsupported-volume", "unsupported-screen"):
            with self.subTest(scenario=scenario):
                self.run_scenario(scenario)

    def test_status_retains_unknown_optional_capabilities(self):
        self.run_scenario("missing-controls")

    def test_saved_volume_is_distinguished_from_hardware_application(self):
        self.run_scenario("volume-deferred")

    def test_local_window_start_cancel_timeout_and_unavailable(self):
        for scenario in ("primary-window-cancel", "window-unavailable", "window-start-failure",
                         "window-timeout", "window-one-result", "pause-precedes-command"):
            with self.subTest(scenario=scenario):
                self.run_scenario("interaction:" + scenario)

    def test_paused_menu_primary_button_still_selects_an_item(self):
        self.run_scenario("interaction:menu-paused-primary")

    def test_pending_primary_input_does_not_survive_pause_and_resume(self):
        self.run_scenario("interaction:primary-stale-after-pause")

    def test_menu_preserves_current_pause_choice_and_retires_old_window(self):
        for scenario in ("menu-pause-current-choice", "menu-cancels-window", "menu-all-modes"):
            with self.subTest(scenario=scenario):
                self.run_scenario("interaction:" + scenario)

    def test_menu_actions_and_persistence_failures_execute_real_application_routes(self):
        for scenario in ("menu-manual-offline", "menu-restore", "menu-save-failure",
                         "menu-configure", "menu-configure-failure", "menu-touch-once"):
            with self.subTest(scenario=scenario):
                self.run_scenario("interaction:" + scenario)

    def test_cancel_during_async_open_cannot_restart_listening_and_next_request_works(self):
        self.run_scenario("interaction:conversation-async-cancel")

    def test_recovery_accepts_unknown_media_but_waits_for_local_interaction(self):
        for scenario in ("recovery-media-unknown", "recovery-stale", "recovery-waits-window",
                         "recovery-waits-feedback", "recovery-waits-menu"):
            with self.subTest(scenario=scenario):
                self.run_scenario("interaction:" + scenario)

    def test_network_callbacks_retire_online_turns_even_after_a_quick_reconnect(self):
        for scenario in ("disconnect-listening", "disconnect-speaking", "disconnect-connecting",
                         "disconnect-idle", "quick-reconnect"):
            with self.subTest(scenario=scenario):
                self.run_scenario("network:" + scenario)

    def test_network_callbacks_preserve_local_window_and_manual_offline_choice(self):
        for scenario in ("local-window-reconnect", "manual-network-event", "scanning-is-not-disconnect"):
            with self.subTest(scenario=scenario):
                self.run_scenario("network:" + scenario)

    def test_protocol_failure_degrades_with_network_alerts_disabled(self):
        for scenario in ("health-media-failure", "health-handshake-failure", "health-control-failure",
                         "health-close-before-failure"):
            with self.subTest(scenario=scenario):
                self.run_scenario("network:" + scenario)

    def test_queued_mcp_responses_keep_source_guard_until_send_and_broadcast(self):
        for scenario in ("current", "cancel-replace", "disconnect-pending"):
            with self.subTest(scenario=scenario):
                self.run_scenario("mcp:" + scenario)

    def test_protocol_failure_rejects_stale_provenance_and_non_failure_events(self):
        for scenario in ("health-stale-epoch", "health-stale-token", "health-stale-handshake",
                         "health-non-failure", "health-manual-offline"):
            with self.subTest(scenario=scenario):
                self.run_scenario("network:" + scenario)


if __name__ == "__main__":
    unittest.main()
