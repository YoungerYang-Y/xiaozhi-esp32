"""Exercise production listening-pause policy with host platform adapters."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import unittest

from test_startup_wake_word_hint import definition


ROOT = Path(__file__).resolve().parents[2]
SIGNATURES = (
    "void Application::PrimaryButtonPressed()",
    "void Application::ToggleListeningPause()",
    "bool Application::IsListeningPaused() const",
    "uint32_t Application::ListeningPauseSequence() const",
    "uint32_t Application::ConversationIntentSequence() const",
    "bool Application::AcceptConversationChannel()",
    "bool Application::ApplyPendingListeningPause()",
    "void Application::HandlePrimaryButtonEvent()",
    "bool Application::IsConversationEventCurrent(",
    "void Application::HandleToggleChatEvent()",
    "void Application::ContinueOpenAudioChannel(",
    "void Application::ContinueWakeWordInvoke(",
    "void Application::SetListeningMode(",
    "ListeningMode Application::GetDefaultListeningMode() const",
    "void Application::AbortSpeaking(",
    "void Application::StopNotification()",
    "void Application::Alert(",
    "void Application::DismissAlert()",
)


PLATFORM = r'''
    #include <algorithm>
    #include <atomic>
    #include <cassert>
    #include <cstdint>
    #include <deque>
    #include <functional>
    #include <memory>
    #include <mutex>
    #include <string>
    #include <string_view>
    #include <utility>
    #include <vector>
    #include "main/device_state.h"
    #define TAG "HostApplication"
    #define ESP_LOGI(...) ((void)0)
    #define ESP_LOGW(...) ((void)0)
    #define ESP_LOGE(...) ((void)0)
    constexpr uint32_t MAIN_EVENT_WAKE_WORD_DETECTED = 1 << 2;
    constexpr uint32_t MAIN_EVENT_VAD_CHANGE = 1 << 3;
    constexpr uint32_t MAIN_EVENT_TOGGLE_CHAT = 1 << 9;
    constexpr uint32_t MAIN_EVENT_START_LISTENING = 1 << 10;
    constexpr uint32_t MAIN_EVENT_STATE_CHANGED = 1 << 12;
    constexpr uint32_t MAIN_EVENT_PRIMARY_BUTTON = 1 << 16;
    constexpr uint32_t MAIN_EVENT_LISTENING_PAUSE = 1 << 17;
    constexpr uint32_t STARTUP_EVENTS = MAIN_EVENT_WAKE_WORD_DETECTED |
        MAIN_EVENT_VAD_CHANGE | MAIN_EVENT_TOGGLE_CHAT |
        MAIN_EVENT_START_LISTENING | MAIN_EVENT_PRIMARY_BUTTON;
    void xEventGroupSetBits(uint32_t* events, uint32_t bits) { *events |= bits; }
    void xEventGroupClearBits(uint32_t* events, uint32_t bits) { *events &= ~bits; }
    uint32_t xEventGroupGetBits(const uint32_t* events) { return *events; }
    enum AecMode { kAecOff, kAecOnDeviceSide, kAecOnServerSide };
    enum ListeningMode { kListeningModeAutoStop, kListeningModeManualStop, kListeningModeRealtime };
    enum AbortReason { kAbortReasonNone, kAbortReasonWakeWordDetected };
    enum class PowerSaveLevel { LOW_POWER, PERFORMANCE };
    namespace Lang::Strings {
        constexpr const char* LISTENING_PAUSED = "Listening paused";
        constexpr const char* STANDBY = "Standby";
    }
    std::vector<std::string> operations;
    struct Display {
        std::string status;
        void SetChatMessage(const char*, const char*) {}
        void SetStatus(const char* value) { status = value; }
        void SetEmotion(const char*) {}
    };
    struct Board {
        Display display;
        static Board& GetInstance() { static Board board; return board; }
        Display* GetDisplay() { return &display; }
        void SetPowerSaveLevel(PowerSaveLevel) {}
    };
    struct AudioStreamPacket { int identity = 0; };
    struct AudioService {
        bool input_paused = false, voice = true, wake = true;
        int decoder_resets = 0, input_changes = 0;
        std::deque<std::unique_ptr<AudioStreamPacket>> wake_packets;
        std::function<void()> during_pop, after_input_pause;
        std::unique_ptr<AudioStreamPacket> PopWakeWordPacket() {
            std::unique_ptr<AudioStreamPacket> packet;
            if (!wake_packets.empty()) {
                packet = std::move(wake_packets.front());
                wake_packets.pop_front();
            }
            if (during_pop) during_pop();
            return packet;
        }
        void PlaySound(const std::string_view&) {}
        void SetInputPaused(bool paused) {
            input_paused = paused;
            // This adapter models the audio service's input-pause boundary;
            // the audio service itself has separate production-code tests.
            if (paused) voice = wake = false;
            ++input_changes;
            operations.push_back(paused ? "input.pause" : "input.resume");
            if (paused && after_input_pause) after_input_pause();
        }
        void EnableVoiceProcessing(bool enable) {
            voice = enable;
            operations.push_back(enable ? "voice.on" : "voice.off");
        }
        void EnableWakeWordDetection(bool enable) {
            wake = enable;
            operations.push_back(enable ? "wake.on" : "wake.off");
        }
        void EnableAudioTesting(bool) { assert(false && "unexpected audio test"); }
        void ResetDecoder() {
            ++decoder_resets;
            operations.push_back("decoder.reset");
        }
    };
    struct NotifyPlayer {
        int stops = 0;
        void Stop() { ++stops; operations.push_back("notification.stop"); }
    };
    struct Protocol {
        using ChannelToken = uint64_t;
        ChannelToken token = 7;
        bool opened = true;
        int opens = 0, closes = 0, aborts = 0;
        int sent_packets = 0, sent_wake_words = 0;
        std::function<void()> during_open, during_send;
        std::function<void(ChannelToken)> closed_callback;
        void OnAudioChannelClosed(std::function<void(ChannelToken)> callback) {
            closed_callback = std::move(callback);
        }
        bool SendAudio(std::unique_ptr<AudioStreamPacket> packet) {
            assert(packet);
            ++sent_packets;
            if (during_send) during_send();
            return true;
        }
        void SendWakeWordDetected(const std::string&) { ++sent_wake_words; }
        ChannelToken channel_token() const { return token; }
        bool IsAudioChannelOpened() const { return opened; }
        bool OpenAudioChannel() {
            ++opens;
            ++token;
            operations.push_back("channel.open");
            if (during_open) during_open();
            opened = true;
            return true;
        }
        void CloseAudioChannel(bool = true) {
            ++closes;
            ++token;
            opened = false;
            operations.push_back("channel.close");
        }
        void SendAbortSpeaking(AbortReason) {
            ++aborts;
            operations.push_back("channel.abort");
        }
    };
    struct Application {
        uint32_t events = 0;
        uint32_t* event_group_ = &events;
        DeviceState state = kDeviceStateIdle;
        AudioService audio_service_;
        NotifyPlayer notify_player_;
        std::unique_ptr<Protocol> protocol_ = std::make_unique<Protocol>();
        std::deque<std::function<void()>> main_tasks_;
        AecMode aec_mode_ = kAecOff;
        ListeningMode listening_mode_ = kListeningModeAutoStop;
        bool aborted_ = false, pending_listening_start_ = false;
        bool play_popup_on_listening_ = false;
        bool pending_startup_wake_word_hint_ = false;
        std::string last_error_message_;
        uint32_t notification_playback_id_ = 0;
    #if CONFIG_WAVESHARE_1_85C_HOME
        std::atomic<uint32_t> pause_requests_{0};
        std::atomic<uint32_t> primary_button_sequence_{0};
        std::atomic<uint32_t> conversation_intent_{0};
        std::atomic<bool> listening_paused_{false};
        std::atomic<bool> conversation_events_enabled_{false};
        std::atomic<Protocol::ChannelToken> accepted_channel_token_{0};
        std::atomic<uint32_t> accepted_channel_intent_{0};
        std::atomic<uint32_t> protocol_callback_epoch_{0};
        std::atomic<uint32_t> processed_pause_requests_{0};
        std::mutex audio_receive_mutex_;
    #endif
        DeviceState GetDeviceState() const { return state; }
        bool SetDeviceState(DeviceState next) {
            state = next;
            operations.push_back(next == kDeviceStateIdle ? "state.idle" : "state.change");
            return true;
        }
        void Schedule(std::function<void()>&& work) { main_tasks_.push_back(std::move(work)); }
        void DrainMainTasks() {
            while (!main_tasks_.empty()) {
                auto work = std::move(main_tasks_.front());
                main_tasks_.pop_front();
                work();
            }
        }
        void HideStartupWakeWordHint() { pending_startup_wake_word_hint_ = false; }
        void PrimaryButtonPressed();
        void ToggleListeningPause();
        bool IsListeningPaused() const;
        uint32_t ListeningPauseSequence() const;
        uint32_t ConversationIntentSequence() const;
        bool AcceptConversationChannel();
        bool ApplyPendingListeningPause();
        void HandlePrimaryButtonEvent();
        bool IsConversationEventCurrent(Protocol::ChannelToken, uint32_t) const;
        void HandleToggleChatEvent();
        void ContinueOpenAudioChannel(ListeningMode);
        void ContinueWakeWordInvoke(const std::string&);
        void SetListeningMode(ListeningMode);
        ListeningMode GetDefaultListeningMode() const;
        void AbortSpeaking(AbortReason);
        void StopNotification();
        void Alert(const char*, const char*, const char*, const std::string_view&);
        void DismissAlert();
        void InstallClosedCallback(uint32_t callback_epoch = 0);
    };
'''


CHECKS = r'''
    DeviceState parse_state(const std::string& value) {
        #define STATE(name) if (value == #name) return name;
        STATE(kDeviceStateUnknown)
        STATE(kDeviceStateStarting)
        STATE(kDeviceStateWifiConfiguring)
        STATE(kDeviceStateIdle)
        STATE(kDeviceStateConnecting)
        STATE(kDeviceStateListening)
        STATE(kDeviceStateSpeaking)
        STATE(kDeviceStateNotifying)
        STATE(kDeviceStateUpgrading)
        STATE(kDeviceStateActivating)
        STATE(kDeviceStateAudioTesting)
        STATE(kDeviceStateFatalError)
        #undef STATE
        assert(false && "unknown device state");
        return kDeviceStateUnknown;
    }

    #if CONFIG_WAVESHARE_1_85C_HOME
    void pause_and_resume(DeviceState initial) {
        Application app;
        app.state = initial;
        const auto old_token = app.protocol_->channel_token();
        app.accepted_channel_token_.store(old_token);
        app.conversation_events_enabled_.store(true);
        assert(app.IsConversationEventCurrent(old_token, 0));
        app.ToggleListeningPause();
        assert(app.ListeningPauseSequence() == 1);
        assert(app.events & MAIN_EVENT_LISTENING_PAUSE);
        // A button callback only posts intent; peripheral work runs in the main task.
        assert(app.audio_service_.input_changes == 0 && app.protocol_->closes == 0);
        assert(!app.IsConversationEventCurrent(old_token, 0));
        assert(app.ApplyPendingListeningPause());
        assert(app.IsListeningPaused());
        assert(app.GetDeviceState() == kDeviceStateIdle);
        assert(app.audio_service_.input_paused);
        assert(!app.audio_service_.voice && !app.audio_service_.wake);
        assert(app.audio_service_.decoder_resets > 0);
        assert(!app.protocol_->opened && app.protocol_->closes > 0);
        assert(!app.conversation_events_enabled_.load());
        const int closes = app.protocol_->closes;
        assert(!app.ApplyPendingListeningPause());
        assert(app.protocol_->closes == closes);

        app.ToggleListeningPause();
        assert(app.ListeningPauseSequence() == 2);
        assert(app.ApplyPendingListeningPause());
        assert(!app.IsListeningPaused());
        assert(!app.audio_service_.input_paused && app.audio_service_.wake);
        assert(!app.audio_service_.voice);
        assert(app.GetDeviceState() == kDeviceStateIdle);
        assert(app.protocol_->opens == 0 && !app.protocol_->opened);
        assert(app.main_tasks_.empty());
        assert(!app.IsConversationEventCurrent(old_token, 0));
    }

    void queued_pause_resume() {
        Application app;
        app.state = kDeviceStateConnecting;
        app.protocol_->opened = false;
        app.ToggleListeningPause();
        app.ToggleListeningPause();
        assert(app.ListeningPauseSequence() == 2);
        assert(app.IsListeningPaused());
        // Two toggles must still cancel the abandoned handshake.
        assert(app.ApplyPendingListeningPause());
        assert(app.protocol_->closes > 0);
        assert(app.GetDeviceState() == kDeviceStateIdle);
        assert(!app.IsListeningPaused() && !app.audio_service_.input_paused);
        assert(app.audio_service_.wake && !app.audio_service_.voice);
        assert(app.audio_service_.decoder_resets > 0);
        assert(!app.conversation_events_enabled_.load());
    }

    void pause_during_handshake(bool resume) {
        Application app;
        app.protocol_->opened = false;
        app.protocol_->during_open = [&]() {
            app.ToggleListeningPause();
            if (resume) app.ToggleListeningPause();
            // Even an even request count keeps all producers closed until
            // the main task has cancelled the old handshake.
            assert(app.IsListeningPaused());
            app.PrimaryButtonPressed();
            assert(!(app.events & MAIN_EVENT_PRIMARY_BUTTON));
            app.events |= STARTUP_EVENTS;
        };
        // Model a last producer posting stale work as the synchronous input
        // fence returns. Cleanup must clear these bits after that fence.
        app.audio_service_.after_input_pause = [&]() { app.events |= STARTUP_EVENTS; };
        app.PrimaryButtonPressed();
        app.HandlePrimaryButtonEvent();
        app.events = 0;
        app.DrainMainTasks();
        assert(app.protocol_->opens == 1 && app.protocol_->closes > 0);
        assert(!app.protocol_->opened && app.GetDeviceState() == kDeviceStateIdle);
        assert(app.ListeningPauseSequence() == (resume ? 2U : 1U));
        assert(app.IsListeningPaused() == !resume);
        assert(app.audio_service_.input_paused == !resume);
        assert(app.audio_service_.wake == resume && !app.audio_service_.voice);
        assert(!app.conversation_events_enabled_.load());
        assert(!(app.events & STARTUP_EVENTS));
        assert(!app.IsConversationEventCurrent(app.protocol_->channel_token(),
                                               app.ListeningPauseSequence()));
        app.protocol_->during_open = {};
        app.audio_service_.after_input_pause = {};
        if (resume) {
            app.PrimaryButtonPressed();
            app.HandlePrimaryButtonEvent();
            app.DrainMainTasks();
            assert(app.protocol_->opens == 2);
            assert(app.GetDeviceState() == kDeviceStateListening);
        }
    }

    void ignored_state(DeviceState initial) {
        Application app;
        app.state = initial;
        app.ToggleListeningPause();
        assert(app.ListeningPauseSequence() == 0);
        assert(!(app.events & MAIN_EVENT_LISTENING_PAUSE));
        assert(!app.ApplyPendingListeningPause());
        assert(app.GetDeviceState() == initial);
        assert(app.audio_service_.input_changes == 0 && app.protocol_->closes == 0);
        app.PrimaryButtonPressed();
        app.HandlePrimaryButtonEvent();
        app.DrainMainTasks();
        assert(app.GetDeviceState() == initial);
        assert(app.audio_service_.input_changes == 0 && app.protocol_->opens == 0);
    }

    void paused_single_click() {
        Application app;
        app.ToggleListeningPause();
        assert(app.ApplyPendingListeningPause());
        const int changes = app.audio_service_.input_changes;
        app.PrimaryButtonPressed();
        app.HandlePrimaryButtonEvent();
        app.DrainMainTasks();
        assert(app.IsListeningPaused() && app.audio_service_.input_paused);
        assert(app.audio_service_.input_changes == changes);
        assert(app.protocol_->opens == 0 && app.GetDeviceState() == kDeviceStateIdle);
    }

    void stale_single_click() {
        Application app;
        app.protocol_->opened = false;
        app.PrimaryButtonPressed();
        assert(app.events & MAIN_EVENT_PRIMARY_BUTTON);
        app.ToggleListeningPause();
        app.ToggleListeningPause();
        assert(app.ApplyPendingListeningPause());
        app.HandlePrimaryButtonEvent();
        app.DrainMainTasks();
        assert(app.protocol_->opens == 0 && app.GetDeviceState() == kDeviceStateIdle);
        assert(!app.IsListeningPaused());
        // A fresh click at the current pause sequence may start a new conversation.
        app.PrimaryButtonPressed();
        app.HandlePrimaryButtonEvent();
        app.DrainMainTasks();
        assert(app.protocol_->opens == 1 && app.GetDeviceState() == kDeviceStateListening);
    }

    void primary_state(DeviceState initial) {
        Application app;
        app.state = initial;
        app.protocol_->opened = initial != kDeviceStateIdle;
        operations.clear();
        app.PrimaryButtonPressed();
        assert(app.events & MAIN_EVENT_PRIMARY_BUTTON);
        assert(app.protocol_->opens == 0 && app.protocol_->closes == 0);
        app.HandlePrimaryButtonEvent();
        app.DrainMainTasks();
        if (initial == kDeviceStateConnecting || initial == kDeviceStateListening) {
            assert(app.GetDeviceState() == initial);
            assert(app.protocol_->opens == 0 && app.protocol_->closes == 0);
            return;
        }
        assert(app.GetDeviceState() == kDeviceStateListening);
        assert(app.protocol_->opens == 1);
        assert(app.IsConversationEventCurrent(app.protocol_->channel_token(),
                                              app.ListeningPauseSequence()));
        if (initial == kDeviceStateSpeaking || initial == kDeviceStateNotifying) {
            assert(app.protocol_->closes > 0 && app.audio_service_.decoder_resets > 0);
            const auto reset = std::find(operations.begin(), operations.end(), "decoder.reset");
            const auto close = std::find(operations.begin(), operations.end(), "channel.close");
            const auto open = std::find(operations.begin(), operations.end(), "channel.open");
            assert(reset < open && close < open);
        }
        if (initial == kDeviceStateNotifying) assert(app.notify_player_.stops > 0);
    }

    void stale_conversation_events() {
        Application app;
        const auto token = app.protocol_->channel_token();
        app.conversation_events_enabled_.store(true);
        assert(!app.IsConversationEventCurrent(0, 0));
        assert(!app.IsConversationEventCurrent(token, 0));
        app.accepted_channel_token_.store(token);
        assert(app.IsConversationEventCurrent(token, 0));
        assert(!app.IsConversationEventCurrent(token + 1, 0));
        assert(!app.IsConversationEventCurrent(token, 1));
        app.conversation_events_enabled_.store(false);
        assert(!app.IsConversationEventCurrent(token, 0));
        // The close callback publishes its new token and disables admission.
        // The helper reads this atomic cache, not transport-owned lifetime/state.
        app.accepted_channel_token_.store(token + 1);
        assert(!app.IsConversationEventCurrent(token, 0));
        assert(!app.IsConversationEventCurrent(token + 1, 0));
        app.conversation_events_enabled_.store(true);
        assert(!app.IsConversationEventCurrent(token, 0));
        app.accepted_channel_token_.store(token);
        app.protocol_.reset();
        assert(app.IsConversationEventCurrent(token, 0));
        app.listening_paused_.store(true);
        assert(!app.IsConversationEventCurrent(token, 0));
        app.listening_paused_.store(false);
        app.ToggleListeningPause();
        assert(!app.IsConversationEventCurrent(token, app.ListeningPauseSequence()));
    }

    void accept_only_open_current_channels() {
        Application closed;
        closed.protocol_->opened = false;
        closed.state = kDeviceStateConnecting;
        assert(!closed.AcceptConversationChannel());
        assert(!closed.conversation_events_enabled_.load());
        closed.SetListeningMode(kListeningModeAutoStop);
        assert(closed.GetDeviceState() == kDeviceStateIdle);
        assert(!closed.conversation_events_enabled_.load());

        Application current;
        current.conversation_intent_.store(23);
        current.state = kDeviceStateConnecting;
        current.SetListeningMode(kListeningModeAutoStop);
        assert(current.GetDeviceState() == kDeviceStateListening);
        assert(current.accepted_channel_token_.load() == current.protocol_->channel_token());
        assert(current.accepted_channel_intent_.load() == 23);
        assert(current.conversation_events_enabled_.load());

        Application pending;
        pending.ToggleListeningPause();
        assert(!pending.AcceptConversationChannel());
        assert(!pending.conversation_events_enabled_.load());
        Application missing;
        missing.protocol_.reset();
        assert(!missing.AcceptConversationChannel());
        assert(!missing.conversation_events_enabled_.load());
    }

    void pause_during_wake_upload(const std::string& boundary) {
        Application app;
        app.state = kDeviceStateConnecting;
        const int packets = boundary == "empty-pop" ? 0 : 3;
        for (int i = 0; i < packets; ++i) {
            app.audio_service_.wake_packets.push_back(std::make_unique<AudioStreamPacket>());
        }
        const auto toggle_twice = [&]() {
            app.ToggleListeningPause();
            app.ToggleListeningPause();
            assert(app.IsListeningPaused());
        };
        int pops = 0;
        if (boundary == "pop" || boundary == "empty-pop") {
            app.audio_service_.during_pop = [&]() { if (++pops == 1) toggle_twice(); };
        } else if (boundary == "send") {
            // One packet already entering the transport cannot be recalled.
            app.protocol_->during_send = toggle_twice;
        } else {
            assert(boundary == "normal");
        }
        app.ContinueWakeWordInvoke("Wake me");
        if (boundary == "normal") {
            assert(app.protocol_->sent_packets == packets);
            assert(app.protocol_->sent_wake_words == 1);
            assert(app.GetDeviceState() == kDeviceStateListening);
            assert(app.IsConversationEventCurrent(app.protocol_->channel_token(), 0));
            return;
        }
        assert(app.protocol_->sent_packets == (boundary == "send" ? 1 : 0));
        assert(app.protocol_->sent_wake_words == 0);
        assert(app.ListeningPauseSequence() == 2 && !app.IsListeningPaused());
        assert(!app.protocol_->opened && app.protocol_->closes > 0);
        assert(app.GetDeviceState() == kDeviceStateIdle);
        assert(app.audio_service_.wake && !app.audio_service_.voice);
        assert(!app.audio_service_.input_paused);
        assert(!app.conversation_events_enabled_.load());
    }

    void old_channel_close_does_not_cancel_a_new_intent() {
        Application app;
        app.conversation_intent_.store(41);
        assert(app.AcceptConversationChannel());
        const auto old_token = app.accepted_channel_token_.load();
        app.InstallClosedCallback();
        // The old transport is closed, and a new logical conversation exists
        // before that old transport delivers its delayed close callback.
        app.protocol_->opened = false;
        app.protocol_->token = old_token + 1;
        app.conversation_intent_.store(42);
        app.state = kDeviceStateConnecting;
        app.protocol_->closed_callback(old_token + 1);
        assert(!app.conversation_events_enabled_.load());
        assert(app.GetDeviceState() == kDeviceStateConnecting);
        app.DrainMainTasks();
        assert(app.GetDeviceState() == kDeviceStateConnecting);
        app.ContinueOpenAudioChannel(kListeningModeAutoStop);
        assert(app.GetDeviceState() == kDeviceStateListening);
        assert(app.accepted_channel_intent_.load() == 42);
        assert(app.IsConversationEventCurrent(app.protocol_->channel_token(), 0));
    }

    void current_close_runs_in_main_task_and_stale_callbacks_are_ignored() {
        Application current;
        current.conversation_intent_.store(17);
        current.state = kDeviceStateListening;
        assert(current.AcceptConversationChannel());
        current.InstallClosedCallback();
        const auto token = current.protocol_->channel_token();
        current.protocol_->closed_callback(token + 1);
        assert(!current.conversation_events_enabled_.load());
        assert(current.GetDeviceState() == kDeviceStateListening);
        current.DrainMainTasks();
        assert(current.GetDeviceState() == kDeviceStateIdle);

        Application stale;
        stale.state = kDeviceStateListening;
        assert(stale.AcceptConversationChannel());
        stale.InstallClosedCallback();
        stale.protocol_->closed_callback(stale.accepted_channel_token_.load());
        assert(stale.main_tasks_.empty() && stale.conversation_events_enabled_.load());
        stale.protocol_callback_epoch_.fetch_add(1);
        stale.protocol_->closed_callback(stale.accepted_channel_token_.load() + 1);
        assert(stale.main_tasks_.empty() && stale.conversation_events_enabled_.load());
        assert(stale.GetDeviceState() == kDeviceStateListening);
    }

    void alerts_preserve_the_pause_status() {
        Application app;
        auto display = Board::GetInstance().GetDisplay();
        app.ToggleListeningPause();
        assert(app.ApplyPendingListeningPause());
        assert(display->status == Lang::Strings::LISTENING_PAUSED);
        app.Alert("Network error", "Disconnected", "sad", "");
        assert(display->status == Lang::Strings::LISTENING_PAUSED);
        app.last_error_message_ = "Disconnected";
        app.DismissAlert();
        assert(app.last_error_message_.empty());
        assert(display->status == Lang::Strings::LISTENING_PAUSED);
        app.ToggleListeningPause();
        assert(app.ApplyPendingListeningPause());
        app.Alert("Network error", "Disconnected", "sad", "");
        assert(display->status == "Network error");
        app.DismissAlert();
        assert(display->status == Lang::Strings::STANDBY);
    }
    #endif

    int main(int argc, char** argv) {
        assert(argc >= 2);
        const std::string scenario = argv[1];
    #if CONFIG_WAVESHARE_1_85C_HOME
        const auto state = argc > 2 ? parse_state(argv[2]) : kDeviceStateIdle;
        if (scenario == "pause-resume") pause_and_resume(state);
        else if (scenario == "queued") queued_pause_resume();
        else if (scenario == "handshake-pause") pause_during_handshake(false);
        else if (scenario == "handshake-resume") pause_during_handshake(true);
        else if (scenario == "ignored") ignored_state(state);
        else if (scenario == "paused-click") paused_single_click();
        else if (scenario == "stale-click") stale_single_click();
        else if (scenario == "primary") primary_state(state);
        else if (scenario == "stale-event") stale_conversation_events();
        else if (scenario == "accept") accept_only_open_current_channels();
        else if (scenario == "wake-pop") pause_during_wake_upload("pop");
        else if (scenario == "wake-empty-pop") pause_during_wake_upload("empty-pop");
        else if (scenario == "wake-send") pause_during_wake_upload("send");
        else if (scenario == "wake-normal") pause_during_wake_upload("normal");
        else if (scenario == "old-close") old_channel_close_does_not_cancel_a_new_intent();
        else if (scenario == "current-close") current_close_runs_in_main_task_and_stale_callbacks_are_ignored();
        else if (scenario == "alert") alerts_preserve_the_pause_status();
        else assert(false && "unknown scenario");
    #else
        assert(scenario == "legacy");
        Application app;
        app.PrimaryButtonPressed();
        app.ToggleListeningPause();
        assert(!app.IsListeningPaused() && app.ListeningPauseSequence() == 0);
        assert(app.IsConversationEventCurrent(99, 99));
        assert(!app.ApplyPendingListeningPause());
        app.HandlePrimaryButtonEvent();
        app.DrainMainTasks();
        assert(app.events == 0 && app.GetDeviceState() == kDeviceStateIdle);
        assert(app.audio_service_.input_changes == 0);
        assert(app.protocol_->opens == 0 && app.protocol_->closes == 0);
    #endif
    }
'''


class ListeningPauseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.binaries = None

    def production_methods(self):
        source = (ROOT / "main/application.cc").read_text(encoding="utf-8")
        found = tuple(signature for signature in SIGNATURES if signature in source)
        for signature in SIGNATURES:
            self.assertIn(signature, found, f"missing production method: {signature}")
        close_signature = "protocol_->OnAudioChannelClosed("
        self.assertIn(close_signature, (close_signature,) if close_signature in source else ())
        # Reuse the exact production callback registration. Only initialization
        # of its hardware/epoch captures belongs to this host-only wrapper.
        close_callback = (
            "\nvoid Application::InstallClosedCallback(uint32_t callback_epoch) {\n"
            "auto& board = Board::GetInstance();\n"
            + definition(source, close_signature) + ");\n}\n"
        )
        return "\n".join(definition(source, signature) for signature in SIGNATURES) + close_callback

    def test_application_implements_listening_pause_api(self):
        self.production_methods()

    def compile_binaries(self):
        if self.__class__.binaries is not None:
            return self.__class__.binaries
        methods = self.production_methods()
        compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not compiler or shutil.which(compiler[0]) is None:
            self.skipTest("requires a host C++17 compiler")
        build = Path(self.directory.name)
        source = build / "listening_pause.cc"
        source.write_text(textwrap.dedent(PLATFORM) + methods + textwrap.dedent(CHECKS),
                          encoding="utf-8")
        binaries = {}
        for policy, flags in (
            ("home", ["-DCONFIG_WAVESHARE_1_85C_HOME=1"]),
            ("disabled", ["-DCONFIG_WAVESHARE_1_85C_HOME=0"]),
            ("undefined", []),
        ):
            binary = build / policy
            result = subprocess.run(
                compiler + ["-std=c++17", "-pthread", "-DCONFIG_SEND_WAKE_WORD_DATA=1",
                            *flags, f"-I{ROOT}",
                            str(source), "-o", str(binary)],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            binaries[policy] = binary
        self.__class__.binaries = binaries
        return binaries

    def run_scenario(self, scenario, state=None, policy="home"):
        command = [str(self.compile_binaries()[policy]), scenario]
        if state is not None:
            command.append(str(state))
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_pause_resume_cancels_each_active_conversation_state(self):
        for state in ("Idle", "Connecting", "Listening", "Speaking", "Notifying"):
            with self.subTest(state=state):
                self.run_scenario("pause-resume", "kDeviceState" + state)

    def test_two_queued_toggles_still_cancel_a_connecting_channel(self):
        self.run_scenario("queued")

    def test_pause_or_pause_resume_during_handshake_never_accepts_the_old_channel(self):
        for scenario in ("handshake-pause", "handshake-resume"):
            with self.subTest(scenario=scenario):
                self.run_scenario(scenario)

    def test_configuration_activation_upgrade_and_nonconversation_states_ignore_pause(self):
        for state in ("Unknown", "Starting", "WifiConfiguring", "Upgrading",
                      "Activating", "AudioTesting", "FatalError"):
            with self.subTest(state=state):
                self.run_scenario("ignored", "kDeviceState" + state)

    def test_single_click_does_not_resume_paused_listening(self):
        self.run_scenario("paused-click")

    def test_single_click_from_an_old_pause_sequence_is_discarded(self):
        self.run_scenario("stale-click")

    def test_primary_click_starts_or_interrupts_only_the_allowed_states(self):
        for state in ("Idle", "Connecting", "Listening", "Speaking", "Notifying"):
            with self.subTest(state=state):
                self.run_scenario("primary", "kDeviceState" + state)

    def test_stale_paused_disabled_and_closed_conversation_events_are_rejected(self):
        self.run_scenario("stale-event")

    def test_only_open_unpaused_channels_are_accepted_with_their_current_intent(self):
        self.run_scenario("accept")

    def test_wake_upload_rechecks_pause_after_each_pop_and_before_the_final_message(self):
        for scenario in ("wake-normal", "wake-pop", "wake-empty-pop", "wake-send"):
            with self.subTest(scenario=scenario):
                self.run_scenario(scenario)

    def test_delayed_old_channel_close_does_not_cancel_a_new_unopened_conversation(self):
        self.run_scenario("old-close")

    def test_current_close_is_scheduled_and_old_token_or_epoch_callbacks_are_ignored(self):
        self.run_scenario("current-close")

    def test_alert_and_dismiss_keep_the_paused_status_visible(self):
        self.run_scenario("alert")

    def test_legacy_new_api_is_inert_with_policy_disabled_or_undefined(self):
        for policy in ("disabled", "undefined"):
            with self.subTest(policy=policy):
                self.run_scenario("legacy", policy=policy)


if __name__ == "__main__":
    unittest.main()
