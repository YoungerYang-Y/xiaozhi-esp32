"""Exercise production application alert scheduling and recovery evidence."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

from test_audio_capture_pause import without_includes
from test_startup_wake_word_hint import definition

ROOT = Path(__file__).resolve().parents[2]

PLATFORM = r'''
#include <atomic>
#include <cassert>
#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include "main/connection_health.h"
#include "main/low_battery_reminder.h"
#include "main/waiting_response.h"
#include "main/device_state_machine.h"
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
constexpr int ESP_OK = 0;
uint64_t now_ms = 100;
int64_t esp_timer_get_time() { return now_ms * 1000; }
constexpr uint32_t MAIN_EVENT_VAD_CHANGE = 1 << 3;
constexpr uint32_t MAIN_EVENT_WAKE_WORD_DETECTED = 1 << 2;
constexpr uint32_t MAIN_EVENT_START_LISTENING = 1 << 10;
constexpr uint32_t MAIN_EVENT_TOGGLE_CHAT = 1 << 9;
constexpr uint32_t MAIN_EVENT_PRIMARY_BUTTON = 1 << 16;
constexpr uint32_t MAIN_EVENT_LISTENING_PAUSE = 1 << 17;
void xEventGroupSetBits(uint32_t* bits, uint32_t value) { *bits |= value; }
void xEventGroupClearBits(uint32_t* bits, uint32_t value) { *bits &= ~value; }
enum ListeningMode { kListeningModeAutoStop, kListeningModeManualStop, kListeningModeRealtime };
enum AbortReason { kAbortReasonNone, kAbortReasonWakeWordDetected };
enum class PowerSaveLevel { LOW_POWER, PERFORMANCE };
namespace Lang::Strings {
constexpr const char* WIFI_UNAVAILABLE = "wifi disconnected";
constexpr const char* SERVICE_UNAVAILABLE = "service unavailable";
constexpr const char* LISTENING_PAUSED = "paused";
constexpr const char* STANDBY = "standby";
constexpr const char* ACTIVATION = "activation";
constexpr const char* CONNECTING = "connecting";
constexpr const char* LISTENING = "listening";
constexpr const char* SPEAKING = "speaking";
}
namespace Lang::Sounds {
constexpr std::string_view OGG_LOW_BATTERY = "low";
constexpr std::string_view OGG_EXCLAMATION = "network";
constexpr std::string_view OGG_POPUP = "popup";
}
struct Display {
    std::string status, message;
    void SetStatus(const char* text) { status = text; }
    void SetChatMessage(const char*, const char* text) { message = text; }
    void ClearChatMessages() { message.clear(); }
    bool SetEmotion(const char*) { return true; }
    void RemoveChatMessage(const char*, const char* text) { if (message == text) message.clear(); }
};
struct Codec {
    int volume = 30;
    struct Status { int volume; };
    Status GetOutputVolumeStatus() const { return {volume}; }
};
struct Led { void OnStateChanged() {} };
struct Board {
    Display display; Codec codec; Led led;
    BatteryStatus battery{true, BatteryState::Unknown, BatteryBand::Unknown};
    static Board& GetInstance() { static Board board; return board; }
    Display* GetDisplay() { return &display; }
    Codec* GetAudioCodec() { return &codec; }
    Led* GetLed() { return &led; }
    BatteryStatus GetBatteryStatus() { return battery; }
    void SetPowerSaveLevel(PowerSaveLevel) {}
};
struct AudioServiceCallbacks {
    std::function<void(uint32_t)> on_local_sound_started;
    std::function<void(uint32_t, bool)> on_local_sound_finished;
    std::function<void(uint64_t,uint64_t)> on_incoming_audio_decoded;
    std::function<void(bool)> on_vad_change;
};
struct AudioService {
    AudioServiceCallbacks callbacks;
    bool idle = true, busy = false, voice = false, wake = false, detected = false;
    bool admit = true, input_paused = false;
    int requests = 0, cancels = 0, resets = 0;
    uint32_t id = 0;
    void SetCallbacks(AudioServiceCallbacks& value) { callbacks = value; }
    bool TryPlayLocalSound(std::string_view, uint32_t value) {
        ++requests; if (!admit) return false; busy = true; id = value; return true;
    }
    void CancelLocalSound(uint32_t) { ++cancels; }
    bool IsLocalSoundBusy() const { return busy; }
    bool IsPlaybackIdle() const { return idle; }
    bool IsVoiceDetected() const { return detected; }
    bool IsAudioProcessorRunning() const { return voice; }
    bool IsAfeWakeWord() const { return false; }
    void ResetDecoder() { ++resets; }
    void ResetCapture() {}
    void EnableVoiceProcessing(bool enabled) { voice = enabled && !input_paused; }
    void EnableWakeWordDetection(bool enabled) { wake = enabled && !input_paused; }
    void SetInputPaused(bool paused) { input_paused = paused; if (paused) voice = wake = false; }
    void EnableAudioTesting(bool) {}
    void PlaySound(std::string_view) {}
    void Started() { if (callbacks.on_local_sound_started) callbacks.on_local_sound_started(id); }
    void Finished(bool completed) {
        busy = false; if (callbacks.on_local_sound_finished) callbacks.on_local_sound_finished(id, completed);
    }
    void Decoded(uint64_t token,uint64_t request) { if (callbacks.on_incoming_audio_decoded) callbacks.on_incoming_audio_decoded(token,request); }
};
struct NotifyPlayer {
    bool busy = false;
    bool IsBusy() const { return busy; }
    void OnPlaybackDrained() {}
};
struct TokenProbe {
    std::atomic<uint64_t> value{0};
    mutable std::function<void()> after_load;
    void store(uint64_t next) { value.store(next); }
    uint64_t load() const {
        const auto observed = value.load();
        if (after_load) { auto run = std::move(after_load); after_load = {}; run(); }
        return observed;
    }
};
'''

CHECKS = r'''
#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS
void Base(Application& app) {
    auto& board = Board::GetInstance();
    board.battery = {true, BatteryState::Unknown, BatteryBand::Unknown};
    board.codec.volume = 30;
    app.SetDeviceState(kDeviceStateStarting);
    app.SetDeviceState(kDeviceStateActivating);
    app.SetDeviceState(kDeviceStateIdle);
    app.wifi_link_connected_.store(true);
    app.HandleConnectionWifiEvent(true);
    app.InstallAudioCallbacks();
}
void Channel(Application& app, uint64_t token, bool probe) {
    using S = Protocol::HealthStage;
    using O = Protocol::HealthOutcome;
    app.protocol_->token = token;
    app.HandleProtocolHealthEvent({S::Handshake, O::Started, token, token});
    app.HandleProtocolHealthEvent({S::Handshake, O::Succeeded, token, token});
    app.HandleProtocolHealthEvent({S::Media, O::Started, token, token, probe});
}
void HealthyConfiguration(Application& app) {
    using S = ConnectionHealth::Stage;
    for (auto stage : {S::Configuration, S::Activation, S::Control}) {
        app.connection_health_.BeginAttempt(stage, 7);
        app.connection_health_.ReportSuccess(stage, 7);
    }
}
void Listen(Application& app, uint64_t token) {
    app.SetDeviceState(kDeviceStateConnecting);
    app.SetDeviceState(kDeviceStateListening);
    app.protocol_->token = token;
    assert(app.AcceptConversationChannel());
    app.BeginWaitingResponseTurn();
}
#endif
int main(int argc, char** argv) {
    assert(argc == 2);
    const std::string scenario = argv[1];
    Application app;
#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS
    Base(app);
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
    using S = Protocol::HealthStage;
    using O = Protocol::HealthOutcome;
    if (scenario == "phase-evidence") {
        HealthyConfiguration(app);
        Channel(app, 100, true);
        app.HandleProtocolHealthEvent({S::Media, O::Failed, 100, 100});
        assert(app.HasConnectionAlert());
        Channel(app, 101, true);
        app.HandleProtocolHealthEvent({S::Control, O::Succeeded, 7, 0});
        assert(std::string(app.GetServiceStateName()) == "unavailable");
        app.HandleProtocolHealthEvent({S::Media, O::Succeeded, 100, 100});
        assert(app.HasConnectionAlert());
        app.HandleProtocolHealthEvent({S::Media, O::Succeeded, 101, 101});
        assert(std::string(app.GetServiceStateName()) == "available");
        assert(!app.HasConnectionAlert());
        return 0;
    }
    if (scenario == "decoded-media") {
        HealthyConfiguration(app);
        Channel(app, 200, false);
        app.HandleProtocolHealthEvent({S::Media, O::Failed, 200, 200});
        Channel(app, 201, false);
        Listen(app, 201);
        app.InstallVadCallback();
        app.audio_service_.callbacks.on_vad_change(true);
        app.Drain();
        const auto speech = app.media_speech_sequence_.load();
        assert(speech != 0);
        app.RecordMediaRecognition(201, speech, true);
        app.audio_service_.Decoded(201, app.media_decode_request_.load());
        app.Drain();
        assert(std::string(app.GetServiceStateName()) == "available");
        return 0;
    }
    if (scenario == "wifi-retirement") {
        HealthyConfiguration(app);
        Channel(app, 300, true);
        Listen(app, 300);
        app.audio_service_.voice = true;
        app.wifi_link_connected_.store(false);
        app.HandleConnectionWifiEvent(false);
        assert(app.GetDeviceState() == kDeviceStateIdle && !app.audio_service_.voice);
        assert(app.protocol_->closes == 1);
        app.wifi_link_connected_.store(true);
        app.HandleConnectionWifiEvent(true);
        assert(std::string(app.GetServiceStateName()) == "unknown");
        assert(app.protocol_->listens == 0 && !app.audio_service_.voice);
        return 0;
    }
    if (scenario == "copied-health-event") {
        app.InstallHealthCallback(0);
        Protocol::HealthEvent event{S::Control, O::Started, 77, 0};
        app.protocol_->Emit(event);
        event.attempt_id = 88;
        app.Drain();
        assert(app.connection_health_.ReportSuccess(ConnectionHealth::Stage::Control, 77));
        app.protocol_callback_epoch_.store(1);
        app.protocol_->Emit({S::Control, O::Failed, 77, 0});
        app.Drain();
        assert(app.connection_health_.GetStageState(ConnectionHealth::Stage::Control) ==
               ConnectionHealth::StageState::Healthy);
        return 0;
    }
    if (scenario == "withdraw-before-start") {
        HealthyConfiguration(app);
        Channel(app, 500, true);
        app.HandleProtocolHealthEvent({S::Media, O::Failed, 500, 500});
        app.TickLocalPrompts();
        assert(app.audio_service_.requests == 1);
        app.HandleProtocolHealthEvent({S::Media, O::Succeeded, 500, 500});
        assert(app.local_prompt_.cancelled && app.audio_service_.cancels == 1);
        app.audio_service_.Finished(false);
        app.Drain();
        assert(app.local_prompt_.id == 0 && app.protocol_->listens == 0);
        return 0;
    }
    if (scenario == "raw-link-priority") {
        HealthyConfiguration(app);
        Channel(app, 600, true);
        app.HandleProtocolHealthEvent({S::Media, O::Failed, 600, 600});
        // The actual link edge precedes processing of the queued board event.
        app.wifi_link_connected_.store(false);
        app.TickLocalPrompts();
        assert(app.audio_service_.requests == 0);
        return 0;
    }
    if (scenario == "decoded-request-race") {
        HealthyConfiguration(app);
        Channel(app, 700, false);
        Listen(app, 700);
        app.InstallVadCallback();
        app.audio_service_.callbacks.on_vad_change(true);
        app.Drain();
        app.RecordMediaRecognition(700, app.media_speech_sequence_.load(), true);
        const auto old_request = app.media_decode_request_.load();
        app.accepted_channel_token_.after_load = [&]() {
            // The old decode observer passed its eligibility check; the main
            // task replaces the channel and arms a new recovery request.
            app.SetDeviceState(kDeviceStateIdle);
            Channel(app, 701, false);
            Listen(app, 701);
            app.audio_service_.callbacks.on_vad_change(true);
            app.Drain();
            app.RecordMediaRecognition(701, app.media_speech_sequence_.load(), true);
        };
        app.audio_service_.Decoded(700, old_request);
        app.audio_service_.Decoded(701, app.media_decode_request_.load());
        app.Drain();
        assert(std::string(app.GetServiceStateName()) == "available");
        return 0;
    }
    if (scenario == "same-token-old-request") {
        HealthyConfiguration(app);
        Channel(app, 900, false);
        app.HandleProtocolHealthEvent({S::Media, O::Failed, 900, 900});
        Channel(app, 901, false);
        Listen(app, 901);
        app.InstallVadCallback();
        app.audio_service_.callbacks.on_vad_change(true);
        app.Drain();
        app.RecordMediaRecognition(901, app.media_speech_sequence_.load(), true);
        const auto old_request = app.media_decode_request_.load();
        app.HandleStopListeningEvent();
        app.HandleStartListeningEvent(); // retains the same open channel
        app.HandleStateChangedEvent();
        app.audio_service_.Decoded(901, old_request);
        app.Drain();
        assert(std::string(app.GetServiceStateName()) == "unavailable");
        app.audio_service_.callbacks.on_vad_change(true);
        app.Drain();
        app.RecordMediaRecognition(901, app.media_speech_sequence_.load(), true);
        const auto current_request = app.media_decode_request_.load();
        assert(current_request != 0 && current_request != old_request);
        app.audio_service_.Decoded(901, old_request);
        app.Drain();
        assert(std::string(app.GetServiceStateName()) == "unavailable");
        app.audio_service_.Decoded(901, current_request);
        app.Drain();
        assert(std::string(app.GetServiceStateName()) == "available");
        return 0;
    }
#endif
#if CONFIG_WAVESHARE_1_85C_HOME_LOW_BATTERY_ALERTS
    if (scenario == "listen-gap" || scenario == "drained-gap") {
        auto& board = Board::GetInstance();
        board.battery = {true, BatteryState::Low, BatteryBand::OneBar};
        Channel(app, 400, false);
        Listen(app, 400);
        app.SetDeviceState(kDeviceStateSpeaking);
        app.TtsStopped();
        app.Drain();
        if (scenario == "drained-gap") {
            app.audio_service_.idle = false;
            app.HandleStateChangedEvent();
            assert(app.pending_listening_start_ && app.audio_service_.requests == 0);
            app.audio_service_.idle = true;
            app.PlaybackDrained();
        } else {
            app.HandleStateChangedEvent();
        }
        assert(app.audio_service_.requests == 1 && app.protocol_->listens == 0);
        app.audio_service_.Started();
        app.audio_service_.Finished(true);
        app.Drain();
        assert(app.protocol_->listens == 1 && app.audio_service_.voice);
        assert(!app.low_battery_reminder_.HasPending());
        return 0;
    }
    if (scenario == "paused-late-start") {
        Board::GetInstance().battery = {true, BatteryState::Low, BatteryBand::OneBar};
        app.TickLocalPrompts();
        assert(app.audio_service_.requests == 1);
        app.audio_service_.Started(); // receipt is queued, not yet consumed
        app.pause_requests_.store(1);
        app.ApplyPendingListeningPause();
        app.audio_service_.Finished(false);
        app.Drain();
        assert(app.IsListeningPaused() && !app.low_battery_reminder_.HasPending());
        app.TickLocalPrompts();
        assert(app.audio_service_.requests == 1 && app.protocol_->listens == 0);
        assert(!app.audio_service_.voice);
        return 0;
    }
    if (scenario == "mute-and-retry") {
        auto& board = Board::GetInstance();
        board.battery = {true, BatteryState::Low, BatteryBand::OneBar};
        board.codec.volume = 0;
        app.TickLocalPrompts();
        assert(app.audio_service_.requests == 0 && app.low_battery_reminder_.HasPending());
        board.codec.volume = 30;
        app.TickLocalPrompts();
        assert(app.audio_service_.requests == 1);
        return 0;
    }
    if (scenario == "active-user-first-listen") {
        auto& board = Board::GetInstance();
        board.battery = {true, BatteryState::Low, BatteryBand::OneBar};
        app.low_battery_reminder_.Observe(board.battery);
        Channel(app, 800, false);
        Listen(app, 800);
        app.SetListeningMode(kListeningModeAutoStop);
        app.HandleStateChangedEvent();
        assert(app.audio_service_.requests == 0 && app.protocol_->listens == 1);
        assert(app.audio_service_.voice && app.low_battery_reminder_.HasPending());
        return 0;
    }
#endif
    assert(false && "unknown scenario");
#else
    assert(!app.HasConnectionAlert());
    assert(std::string(app.GetServiceStateName()) == "unknown");
#endif
}
'''


class ApplicationAlertsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not compiler or not shutil.which(compiler[0]):
            raise unittest.SkipTest("requires a host C++ compiler")
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        directory = Path(cls.directory.name)
        (directory / "esp_log.h").write_text(
            '#pragma once\n#define ESP_LOGI(...) ((void)0)\n#define ESP_LOGW(...) ((void)0)\n')
        source = (ROOT / "main/application.cc").read_text()
        header = (ROOT / "main/application.h").read_text()
        protocol = (ROOT / "main/protocols/protocol.h").read_text()
        begin = protocol.index("    using ChannelToken")
        end = protocol.index("    virtual ~Protocol", begin)
        protocol_stub = 'struct Protocol {\n' + protocol[begin:end] + r'''
            uint64_t token = 1; int closes = 0, listens = 0;
            std::function<void(const HealthEvent&)> health;
            void OnHealthEvent(std::function<void(const HealthEvent&)> cb) { health = std::move(cb); }
            void Emit(const HealthEvent& event) { if (health) health(event); }
            uint64_t channel_token() const { return token; }
            bool IsAudioChannelOpened() const { return true; }
            bool OpenAudioChannel() { return true; }
            void CloseAudioChannel() { ++closes; }
            void SendStartListening(ListeningMode) { ++listens; }
            void SendStopListening() {}
            void SendAbortSpeaking(AbortReason) {}
        };
'''
        begin = header.index("    std::atomic<uint32_t> pause_requests_")
        end = header.index("\n#endif", begin)
        home_members = header[begin:end]
        home_members = home_members.replace(
            "std::atomic<Protocol::ChannelToken> accepted_channel_token_{0};",
            "TokenProbe accepted_channel_token_;")
        begin = header.index("    enum class LocalPromptKind")
        end = header.index("    void HandleConnectionWifiEvent", begin)
        local_members = header[begin:end]
        begin_methods = header.index("    void HandleConnectionWifiEvent", begin)
        end_methods = header.index("\n#endif", begin_methods)
        local_declarations = header[begin_methods:end_methods]
        public = definition(header, "bool HasConnectionAlert() const") + '\n'
        public += definition(header, "const char* GetServiceStateName() const")
        app = r'''
struct Application {
    DeviceStateMachine state_machine_;
    AudioService audio_service_; NotifyPlayer notify_player_;
    std::unique_ptr<Protocol> protocol_ = std::make_unique<Protocol>();
    std::deque<std::function<void()>> tasks;
    uint32_t events = 0; uint32_t* event_group_ = &events;
    ListeningMode listening_mode_ = kListeningModeAutoStop;
    bool pending_listening_start_ = false, play_popup_on_listening_ = false, aborted_ = false;
    int clock_ticks_ = 0;
    std::string last_error_message_;
    DeviceState GetDeviceState() const { return state_machine_.GetState(); }
    void Schedule(std::function<void()>&& work) { tasks.push_back(std::move(work)); }
    void Drain() { while (!tasks.empty()) { auto work = std::move(tasks.front()); tasks.pop_front(); work(); } }
    void StopNotification() { SetDeviceState(kDeviceStateIdle); }
    void InstallAudioCallbacks(); void InstallVadCallback();
    void InstallHealthCallback(uint32_t callback_epoch);
    void PlaybackDrained();
    void TtsStopped();
    void SetListeningMode(ListeningMode);
    bool SetDeviceState(DeviceState);
    bool IsListeningPaused() const;
    uint32_t ListeningPauseSequence() const;
    uint32_t ConversationIntentSequence() const;
    bool IsConversationEventCurrent(Protocol::ChannelToken, uint32_t) const;
    bool AcceptConversationChannel();
    bool ApplyPendingListeningPause();
    void BeginWaitingResponseTurn();
    void HandleWaitingSpeech(uint64_t, Protocol::ChannelToken, uint32_t);
    void StartListeningAudio();
    void ConfigureWakeWordForListening();
    void HandleStateChangedEvent();
    void HandleStartListeningEvent();
    void HandleStopListeningEvent();
    void AbortSpeaking(AbortReason);
    void ContinueOpenAudioChannel(ListeningMode);
'''
        app += public + '\n' + home_members
        app += '\n#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS\n' + local_members + local_declarations + '\n#endif\n};\n'
        methods = '\n'.join(definition(source, name) for name in [
            "bool Application::SetDeviceState(", "bool Application::IsListeningPaused() const",
            "uint32_t Application::ListeningPauseSequence() const",
            "uint32_t Application::ConversationIntentSequence() const",
            "bool Application::IsConversationEventCurrent(",
            "bool Application::AcceptConversationChannel()",
            "bool Application::ApplyPendingListeningPause()",
            "void Application::BeginWaitingResponseTurn()",
            "void Application::HandleWaitingSpeech(",
            "void Application::StartListeningAudio()",
            "void Application::ConfigureWakeWordForListening()",
            "void Application::HandleStateChangedEvent()",
            "void Application::SetListeningMode(",
            "void Application::HandleStartListeningEvent()",
            "void Application::HandleStopListeningEvent()",
            "void Application::AbortSpeaking(",
            "void Application::ContinueOpenAudioChannel(",
        ])
        install = '\n#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS\n'
        install += 'void Application::InstallAudioCallbacks() { AudioServiceCallbacks callbacks;\n'
        install += definition(source, "callbacks.on_local_sound_started =") + ';\n'
        install += definition(source, "callbacks.on_local_sound_finished =") + ';\n'
        install += '#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS\n'
        install += definition(source, "callbacks.on_incoming_audio_decoded =") + ';\n#endif\n'
        install += 'audio_service_.SetCallbacks(callbacks); }\n'
        install += 'void Application::InstallVadCallback() { auto& callbacks = audio_service_.callbacks;\n'
        install += definition(source, "callbacks.on_vad_change =") + '; }\n'
        install += '#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS\n'
        install += 'void Application::InstallHealthCallback(uint32_t callback_epoch) {\n'
        install += definition(source, "protocol_->OnHealthEvent([this, callback_epoch]") + '); }\n#endif\n'
        install += 'void Application::PlaybackDrained() {\n'
        drain = definition(source, "if (bits & MAIN_EVENT_PLAYBACK_DRAINED)")
        install += drain[drain.index('{') + 1:-1] + '\n}\n'
        install += 'void Application::TtsStopped() { const auto token = accepted_channel_token_.load();\n'
        install += 'const auto pause_sequence = ListeningPauseSequence();\n'
        install += definition(source, "Schedule([this, token, pause_sequence]()", occurrence=1) + '); }\n#endif\n'
        cpp = directory / "app.cc"
        cpp.write_text(PLATFORM + protocol_stub + app + methods +
                       without_includes((ROOT / "main/application_alerts.cc").read_text()) +
                       install + CHECKS, encoding="utf-8")
        cls.binaries = {}
        for network, low in ((1, 1), (1, 0), (0, 1), (0, 0)):
            local = int(network or low)
            binary = directory / f"app-{network}-{low}"
            result = subprocess.run(compiler + ["-std=c++23", "-pthread", "-I", str(ROOT),
                                    "-I", str(directory), "-DCONFIG_WAVESHARE_1_85C_HOME=1",
                                    f"-DCONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS={local}",
                                    f"-DCONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS={network}",
                                    f"-DCONFIG_WAVESHARE_1_85C_HOME_LOW_BATTERY_ALERTS={low}",
                                    str(cpp), str(ROOT / "main/device_state_machine.cc"),
                                    "-o", str(binary)], capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
            cls.binaries[(network, low)] = binary

    def scenario(self, name, configuration=(1, 1)):
        result = subprocess.run([str(self.binaries[configuration]), name],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_stage_specific_positive_evidence(self):
        self.scenario("phase-evidence")

    def test_stt_and_actual_decode_receipt_recover_unprobed_media(self):
        self.scenario("decoded-media")

    def test_wifi_recovery_does_not_resume_microphone(self):
        self.scenario("wifi-retirement")

    def test_health_events_are_copied_and_old_protocol_epoch_is_ignored(self):
        self.scenario("copied-health-event")

    def test_recovery_withdraws_an_unstarted_sound_before_the_next_tick(self):
        self.scenario("withdraw-before-start")

    def test_link_edge_prevents_a_stale_service_sound(self):
        self.scenario("raw-link-priority")

    def test_old_decode_observer_cannot_consume_the_new_recovery_request(self):
        self.scenario("decoded-request-race")

    def test_old_media_evidence_is_rejected_after_same_channel_restart(self):
        self.scenario("same-token-old-request")

    def test_reminder_precedes_listening_for_both_drain_orders(self):
        self.scenario("listen-gap")
        self.scenario("drained-gap")

    def test_pause_retains_delayed_started_receipt_without_reopening_microphone(self):
        self.scenario("paused-late-start")

    def test_muted_reminder_remains_pending(self):
        self.scenario("mute-and-retry")

    def test_active_user_start_is_not_delayed_by_an_unstarted_reminder(self):
        self.scenario("active-user-first-listen")

    def test_feature_switches_preserve_independent_paths(self):
        self.scenario("phase-evidence", (1, 0))
        self.scenario("mute-and-retry", (0, 1))
        self.scenario("disabled", (0, 0))
