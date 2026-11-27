"""Exercise the production display-only waiting state across listening turns."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

from test_startup_wake_word_hint import definition

ROOT = Path(__file__).resolve().parents[2]


class WaitingResponseTests(unittest.TestCase):
    def run_driver(self, body):
        compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not compiler or not shutil.which(compiler[0]):
            self.skipTest("requires a host C++ compiler")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "waiting.cc"
            source.write_text(
                '#include <cassert>\n#include "main/waiting_response.h"\n'
                'int main() { WaitingResponse waiting;\n' + body + '\n}\n',
                encoding="utf-8",
            )
            binary = Path(directory) / "waiting"
            result = subprocess.run(
                compiler + ["-std=c++23", "-I", str(ROOT), str(source), "-o", str(binary)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_only_new_speech_and_nonempty_recognition_start_waiting(self):
        self.run_driver(r'''
            waiting.Begin(1);
            assert(!waiting.Recognized(1, 100, true)); // old STT before new speech
            waiting.Speech(1);
            assert(!waiting.Recognized(1, 110, false));
            assert(waiting.Recognized(1, 200, true));
            assert(waiting.waiting());
            assert(!waiting.Recognized(1, 300, true)); // partials cannot extend it
        ''')

    def test_expiry_cannot_be_extended_by_partials_and_requires_new_speech(self):
        self.run_driver(r'''
            waiting.Begin(7);
            waiting.Speech(7);
            assert(waiting.Recognized(7, 100, true));
            assert(!waiting.Recognized(7, 9000, true));
            assert(!waiting.Expire(10099));
            assert(waiting.Expire(10100));
            assert(!waiting.waiting());
            assert(!waiting.Recognized(7, 10101, true));
            waiting.Speech(7);
            assert(waiting.Recognized(7, 10200, true));
        ''')

    def test_cancellation_and_next_turn_reject_old_events(self):
        self.run_driver(r'''
            waiting.Begin(10);
            waiting.Speech(10);
            assert(waiting.Recognized(10, 100, true));
            waiting.Cancel(); // TTS, disconnect, pause or cancellation
            assert(!waiting.waiting());
            waiting.Speech(10);
            assert(!waiting.Recognized(10, 200, true));
            waiting.Begin(11); // TTS stop / listen start alone is insufficient
            assert(!waiting.Recognized(11, 300, true));
            waiting.Speech(10);
            assert(!waiting.Recognized(11, 400, true));
            waiting.Speech(11);
            assert(!waiting.Recognized(10, 500, true));
            assert(waiting.Recognized(11, 600, true));
        ''')

    def test_application_gates_queued_events_and_keeps_timeouts_display_only(self):
        compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not compiler or not shutil.which(compiler[0]):
            self.skipTest("requires a host C++ compiler")
        production = (ROOT / "main/application.cc").read_text()
        signatures = [
            "bool Application::SetDeviceState(",
            "void Application::BeginWaitingResponseTurn()",
            "void Application::EndWaitingResponseTurn()",
            "void Application::HandleWaitingSpeech(",
            "void Application::HandleWaitingRecognition(",
            "void Application::UpdateWaitingResponse()",
            "void Application::HandleWakeWordDetectedEvent()",
        ]
        platform = r'''
#include <atomic>
#include <cassert>
#include <deque>
#include <functional>
#include <memory>
#include <string>
#include "main/waiting_response.h"
#include "main/device_state.h"
#define CONFIG_WAVESHARE_1_85C_HOME 1
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
uint64_t now_ms = 0;
int64_t esp_timer_get_time() { return now_ms * 1000; }
constexpr int MAIN_EVENT_VAD_CHANGE = 1;
void xEventGroupSetBits(int, int) {}
namespace Lang::Strings { const char* LISTENING = "listening";
                         const char* WAITING_RESPONSE = "waiting"; }
namespace Lang::Sounds { const char* OGG_POPUP = "popup"; }
constexpr int kAbortReasonWakeWordDetected = 1;
struct Protocol { using ChannelToken = uint64_t; void SendStartListening(int) {} };
struct AudioService {
    std::string GetLastWakeWord() { return "wake"; }
    std::unique_ptr<int> PopPacketFromSendQueue() { return {}; }
    void ResetDecoder() {}
    void PlaySound(const char*) {}
    void EnableWakeWordDetection(bool) {}
};
struct Display { std::string status; void SetStatus(const char* s) { status = s; } };
struct Board {
    Display display;
    static Board& GetInstance() { static Board b; return b; }
    Display* GetDisplay() { return &display; }
};
struct StateMachine {
    DeviceState state = kDeviceStateIdle;
    bool reject = false;
    bool TransitionTo(DeviceState next) { if (reject) return false; state = next; return true; }
};
struct OpenGate {
    bool value = false;
    mutable std::function<void()> before_load;
    void store(bool next) { value = next; }
    bool load() const {
        if (before_load) { auto run = std::move(before_load); before_load = {}; run(); }
        return value;
    }
};
struct Application {
    StateMachine state_machine_;
    AudioService audio_service_;
    std::unique_ptr<Protocol> protocol_ = std::make_unique<Protocol>();
    std::atomic<uint64_t> waiting_turn_{0};
    OpenGate waiting_turn_open_;
    std::atomic_bool conversation_events_enabled_{true};
    std::atomic<uint64_t> accepted_channel_token_{7};
    uint64_t waiting_response_turn_ = 0;
    WaitingResponse waiting_response_;
    bool paused = false;
    bool play_popup_on_listening_ = false;
    int event_group_ = 0;
    struct { std::function<void(bool)> on_vad_change; } callbacks;
    std::deque<std::function<void()>> scheduled;
    DeviceState GetDeviceState() const { return state_machine_.state; }
    bool IsListeningPaused() const { return paused; }
    uint32_t ListeningPauseSequence() const { return 2; }
    void Schedule(std::function<void()>&& work) { scheduled.push_back(std::move(work)); }
    void Drain() { while (!scheduled.empty()) { auto work = std::move(scheduled.front());
                                             scheduled.pop_front(); work(); } }
    bool IsConversationEventCurrent(uint64_t token, uint32_t pause) const {
        return token == 7 && pause == 2 && !paused && conversation_events_enabled_.load();
    }
    bool SetDeviceState(DeviceState);
    void BeginWaitingResponseTurn();
    void EndWaitingResponseTurn();
    void HandleWaitingSpeech(uint64_t, Protocol::ChannelToken, uint32_t);
    void HandleWaitingRecognition(uint64_t, bool);
    void UpdateWaitingResponse();
    void InstallVad();
    void HandleWakeWordDetectedEvent();
    void BeginWakeWordInvoke(const std::string&) {}
    void StopNotification() {}
    void AbortSpeaking(int) {}
    int GetDefaultListeningMode() const { return 0; }
    void SetListeningMode(int) {}
};
'''
        checks = r'''
int main() {
    Application app;
    auto& display = Board::GetInstance().display;
    assert(app.SetDeviceState(kDeviceStateListening));
    app.BeginWaitingResponseTurn();
    const auto first = app.waiting_turn_.load();
    app.HandleWaitingRecognition(first, true);
    assert(!app.waiting_response_.waiting());
    app.HandleWaitingSpeech(first, 6, 2); // old channel
    app.HandleWaitingRecognition(first, true);
    assert(!app.waiting_response_.waiting());
    app.HandleWaitingSpeech(first, 7, 2);
    app.HandleWaitingRecognition(first, true);
    assert(app.waiting_response_.waiting() && display.status == "waiting");
    now_ms = 10000;
    app.UpdateWaitingResponse();
    assert(!app.waiting_response_.waiting() && display.status == "listening");
    assert(app.GetDeviceState() == kDeviceStateListening); // display timeout only
    assert(app.SetDeviceState(kDeviceStateSpeaking));
    assert(app.SetDeviceState(kDeviceStateListening));
    app.BeginWaitingResponseTurn();
    const auto second = app.waiting_turn_.load();
    assert(second != first);
    app.HandleWaitingSpeech(first, 7, 2); // queued VAD from first turn
    app.HandleWaitingRecognition(second, true); // old STT before fresh speech
    assert(!app.waiting_response_.waiting());
    app.HandleWaitingSpeech(second, 7, 2);
    app.HandleWaitingRecognition(first, true); // queued STT from first turn
    assert(!app.waiting_response_.waiting());
    app.HandleWaitingRecognition(second, true);
    assert(app.waiting_response_.waiting());
    app.EndWaitingResponseTurn(); // answer sentence / cancellation
    assert(!app.waiting_response_.waiting());
    app.HandleWaitingRecognition(second, true);
    assert(!app.waiting_response_.waiting());
    app.BeginWaitingResponseTurn();
    app.paused = true;
    app.HandleWaitingSpeech(app.waiting_turn_.load(), 7, 2);
    app.HandleWaitingRecognition(app.waiting_turn_.load(), true);
    assert(!app.waiting_response_.waiting());
    const auto before_invalid = app.waiting_turn_.load();
    app.state_machine_.reject = true;
    assert(!app.SetDeviceState(kDeviceStateIdle));
    assert(app.waiting_turn_.load() == before_invalid);
    app.state_machine_.reject = false;
    app.paused = false;
    app.BeginWaitingResponseTurn();
    app.InstallVad();
    // Interrupt a VAD callback at its first open-gate read. The main task
    // advances through an entire answer and next listening turn meanwhile.
    app.waiting_turn_open_.before_load = [&]() {
        app.SetDeviceState(kDeviceStateSpeaking);
        app.SetDeviceState(kDeviceStateListening);
        app.BeginWaitingResponseTurn();
    };
    app.callbacks.on_vad_change(true);
    app.Drain();
    app.HandleWaitingRecognition(app.waiting_turn_.load(), true);
    assert(!app.waiting_response_.waiting()); // old VAD must not become new speech
    auto current = app.waiting_turn_.load();
    app.HandleWaitingSpeech(current, 7, 2);
    app.HandleWaitingRecognition(current, true);
    assert(app.waiting_response_.waiting());
    app.HandleWakeWordDetectedEvent(); // listen -> listen is still a new turn
    assert(app.waiting_turn_.load() != current);
    assert(!app.waiting_response_.waiting());
    app.HandleWaitingRecognition(app.waiting_turn_.load(), true);
    assert(!app.waiting_response_.waiting()); // must observe a new VAD first
}
'''
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "application_waiting.cc"
            vad = definition(production, "callbacks.on_vad_change = [this]")
            source.write_text(platform + '\n'.join(definition(production, s) for s in signatures)
                              + '\nvoid Application::InstallVad() { ' + vad + '; }\n'
                              + checks, encoding="utf-8")
            binary = Path(directory) / "application_waiting"
            result = subprocess.run(compiler + ["-std=c++23", "-I", str(ROOT), str(source),
                                                "-o", str(binary)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
