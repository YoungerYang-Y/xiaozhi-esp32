"""Deterministic host barriers for the real capture and wake-word cancellation code."""

import os
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def source(path):
    return (ROOT / path).read_text(encoding="utf-8")


def without_includes(text):
    return "\n".join(line for line in text.splitlines() if not line.startswith("#include")) + "\n"


def method(path, name):
    text = source(path)
    start = text.index(name)
    start = text.rfind("\n", 0, start) + 1
    opening = text.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


PLATFORM = r'''
    #include <algorithm>
    #include <atomic>
    #include <cassert>
    #include <chrono>
    #include <condition_variable>
    #include <cstdint>
    #include <cstdlib>
    #include <cstring>
    #include <deque>
    #include <functional>
    #include <future>
    #include <memory>
    #include <mutex>
    #include <optional>
    #include <string>
    #include <thread>
    #include <vector>
    using namespace std::chrono_literals;
    #define CONFIG_IDF_TARGET_ESP32S3 1
    #define CONFIG_USE_AUDIO_PROCESSOR 1
    #define CONFIG_SEND_WAKE_WORD_DATA 1
    #define ESP_LOGE(...) ((void)0)
    #define ESP_LOGW(...) ((void)0)
    #define ESP_LOGI(...) ((void)0)
    #define ESP_LOGD(...) ((void)0)
    #define MALLOC_CAP_SPIRAM 1
    #define MALLOC_CAP_INTERNAL 2
    #define MALLOC_CAP_8BIT 4
    #define pdTRUE 1
    #define pdFALSE 0
    #define portMAX_DELAY 0xffffffff
    #define pdMS_TO_TICKS(x) (x)
    #define ESP_FAIL -1
    #define ESP_AUDIO_ERR_OK 0
    #define ESP_AUDIO_MONO 1
    #define ESP_AUDIO_BIT16 16
    #define ESP_AUDIO_SAMPLE_RATE_16K 16000
    #define ESP_OPUS_BITRATE_AUTO 0
    #define ESP_OPUS_ENC_APPLICATION_AUDIO 0
    #define ESP_AUDIO_DEC_RECOVERY_NONE 0
    enum { ESP_OPUS_ENC_FRAME_DURATION_5_MS, ESP_OPUS_ENC_FRAME_DURATION_10_MS,
        ESP_OPUS_ENC_FRAME_DURATION_20_MS, ESP_OPUS_ENC_FRAME_DURATION_40_MS,
        ESP_OPUS_ENC_FRAME_DURATION_60_MS, ESP_OPUS_ENC_FRAME_DURATION_80_MS,
        ESP_OPUS_ENC_FRAME_DURATION_100_MS, ESP_OPUS_ENC_FRAME_DURATION_120_MS };
    using esp_opus_enc_frame_duration_t = int;
    struct esp_opus_enc_config_t {
        int sample_rate, channel, bits_per_sample, bitrate, frame_duration;
        int application_mode, complexity;
        bool enable_fec, enable_dtx, enable_vbr;
    };
    struct esp_audio_enc_in_frame_t { uint8_t* buffer; uint32_t len; };
    struct esp_audio_enc_out_frame_t { uint8_t* buffer; uint32_t len, encoded_bytes; };
    struct esp_audio_dec_in_raw_t { uint8_t* buffer; uint32_t len, consumed; int frame_recover; };
    struct esp_audio_dec_out_frame_t { uint8_t* buffer; uint32_t len, decoded_size; };
    struct esp_audio_dec_info_t {};
    using esp_ae_rate_cvt_handle_t = void*;
    using esp_ae_sample_t = void*;
    using esp_timer_handle_t = void*;
    struct srmodel_list_t {};
    struct Gate {
        std::mutex mutex;
        std::condition_variable cv;
        int entered = 0, permits = 0;
        void Wait() {
            std::unique_lock<std::mutex> lock(mutex);
            ++entered;
            cv.notify_all();
            assert(cv.wait_for(lock, 3s, [&] { return permits > 0; }));
            --permits;
        }
        void Until(int count) {
            std::unique_lock<std::mutex> lock(mutex);
            assert(cv.wait_for(lock, 3s, [&] { return entered >= count; }));
        }
        void Release(int count = 1) {
            std::lock_guard<std::mutex> lock(mutex);
            permits += count;
            cv.notify_all();
        }
    };
    Gate encode_gate, read_gate, fetch_gate, callback_gate;
    inline void* heap_caps_malloc(size_t n, int) { return std::malloc(n); }
    inline void heap_caps_free(void* p) { std::free(p); }
    inline void esp_system_abort(const char*) { std::abort(); }
    inline int64_t esp_timer_get_time() { return 1; }
    inline void esp_timer_stop(void*) {}
    inline void esp_timer_start_periodic(void*, int) {}
    inline void esp_opus_enc_close(void*) {}
    std::atomic<int> encoder_resets{0};
    inline void esp_opus_enc_reset(void*) { ++encoder_resets; }
    inline void esp_opus_dec_close(void*) {}
    inline void esp_opus_dec_reset(void*) {}
    inline void esp_ae_rate_cvt_close(void*) {}
    inline void esp_ae_rate_cvt_reset(void*) {}
    inline int esp_opus_enc_open(esp_opus_enc_config_t*, size_t, void** encoder) {
        *encoder = reinterpret_cast<void*>(1);
        return 0;
    }
    inline void esp_opus_enc_get_frame_size(void*, int* frame, int* output) {
        *frame = 4; *output = 2;
    }
    inline int esp_opus_enc_process(void*, esp_audio_enc_in_frame_t* in,
                                    esp_audio_enc_out_frame_t* out) {
        encode_gate.Wait();
        out->buffer[0] = reinterpret_cast<int16_t*>(in->buffer)[0];
        out->encoded_bytes = 1;
        return 0;
    }
    inline int esp_opus_dec_decode(void*, void*, void*, void*) { return -1; }
    inline void esp_ae_rate_cvt_get_max_out_sample_num(void*, uint32_t n, uint32_t* out) { *out=n; }
    inline void esp_ae_rate_cvt_process(void*, void*, uint32_t, void*, uint32_t*) {}
    struct EventGroup {
        std::mutex mutex;
        std::condition_variable cv;
        uint32_t bits = 0;
    };
    using EventGroupHandle_t = EventGroup*;
    using EventBits_t = uint32_t;
    inline auto xEventGroupCreate() { return new EventGroup; }
    inline void vEventGroupDelete(EventGroup* g) { delete g; }
    inline uint32_t xEventGroupGetBits(EventGroup* g) {
        std::lock_guard<std::mutex> lock(g->mutex); return g->bits;
    }
    inline void xEventGroupSetBits(EventGroup* g, uint32_t bits) {
        std::lock_guard<std::mutex> lock(g->mutex); g->bits |= bits; g->cv.notify_all();
    }
    inline void xEventGroupClearBits(EventGroup* g, uint32_t bits) {
        std::lock_guard<std::mutex> lock(g->mutex); g->bits &= ~bits;
    }
    struct StopTask {};
    std::atomic<bool> stop_afe{false};
    inline uint32_t xEventGroupWaitBits(EventGroup* g, uint32_t bits, bool, bool, uint32_t) {
        std::unique_lock<std::mutex> lock(g->mutex);
        assert(g->cv.wait_for(lock, 3s, [&] { return (g->bits & bits) || stop_afe; }));
        if (stop_afe) throw StopTask{};
        return g->bits;
    }
    struct HostTask {
        std::mutex mutex;
        std::condition_variable cv;
        int notifications = 0;
        bool deleted = false;
        std::thread thread;
    };
    using TaskHandle_t = HostTask*;
    using StaticTask_t = int;
    using StackType_t = uint32_t;
    thread_local HostTask* current_task = nullptr;
    std::atomic<int> task_creations{0};
    inline TaskHandle_t xTaskCreateStatic(void (*f)(void*), const char*, size_t,
                                         void* arg, int, StackType_t*, StaticTask_t*) {
        auto* task = new HostTask;
        ++task_creations;
        task->thread = std::thread([=] { current_task = task; f(arg); });
        return task;
    }
    inline void xTaskNotifyGive(HostTask* task) {
        std::lock_guard<std::mutex> lock(task->mutex);
        ++task->notifications; task->cv.notify_all();
    }
    inline void ulTaskNotifyTake(bool, uint32_t) {
        std::unique_lock<std::mutex> lock(current_task->mutex);
        current_task->cv.wait(lock, [] { return current_task->notifications > 0; });
        current_task->notifications = 0;
    }
    inline void vTaskSuspend(void*) {
        std::unique_lock<std::mutex> lock(current_task->mutex);
        current_task->cv.wait(lock, [] { return current_task->deleted; });
    }
    inline void vTaskDelete(HostTask* task) {
        if (!task) return;
        { std::lock_guard<std::mutex> lock(task->mutex); task->deleted=true; task->cv.notify_all(); }
        task->thread.join(); delete task;
    }
    inline void vTaskDelay(int) {}
    struct AudioCodec {
        bool input = false;
        int input_channels() const { return 1; }
        int input_sample_rate() const { return 16000; }
        int output_sample_rate() const { return 16000; }
        bool input_reference() const { return false; }
        bool input_enabled() const { return input; }
        void EnableInput(bool enable) { input=enable; }
        bool InputData(std::vector<int16_t>& data) { read_gate.Wait(); return true; }
    };
    struct AudioDebugger {};
    struct AudioStreamPacket {
        int frame_duration=60, sample_rate=16000;
        uint32_t timestamp=0, playback_id=0, media_position_ms=0;
        uint64_t source_channel_token=0;
        uint64_t source_request_id=0;
        std::vector<uint8_t> payload;
    };
    struct TrackedConditionVariable {
        std::condition_variable cv;
        Gate* waiting = nullptr;
        template <class Predicate>
        void wait(std::unique_lock<std::mutex>& lock, Predicate predicate) {
            if (!predicate() && waiting) waiting->Release();
            cv.wait(lock, predicate);
        }
        void notify_all() { cv.notify_all(); }
    };
'''


FAKE_ENGINE = r'''
    struct FakeEngine : AudioEngine {
        bool wake=false, voice=false;
        std::atomic<int> feeds{0};
        bool Initialize(AudioCodec*, int, srmodel_list_t*) override { return true; }
        void Feed(std::vector<int16_t>&&) override { ++feeds; }
        void EnableWakeWordDetection(bool enabled) override { wake=enabled; }
        void EnableVoiceProcessing(bool enabled) override { voice=enabled; }
        void EnableDeviceAec(bool) override {}
        void ResetCapture() override { wake=voice=false; }
        bool HasWakeWord() const override { return true; }
        bool IsWakeWordDetectionEnabled() const override { return wake; }
        bool IsVoiceProcessingEnabled() const override { return voice; }
        bool IsAfeWakeWord() const override { return true; }
        std::vector<std::string> GetWakeWords() const override { return {}; }
        size_t GetFeedSize() const override { return 2; }
        void OnWakeWordDetected(std::function<void(const std::string&)>) override {}
        void OnOutput(std::function<void(std::vector<int16_t>&&)>) override {}
        void OnVadStateChange(std::function<void(bool)>) override {}
        void EncodeWakeWordData() override {}
        bool GetWakeWordOpus(std::vector<uint8_t>&) override { return false; }
        const std::string& GetLastDetectedWakeWord() const override { static std::string s; return s; }
    };
'''


class AudioCapturePauseTests(unittest.TestCase):
    def test_persistent_pause_and_capture_reset_are_public(self):
        header = (ROOT / "main/audio/audio_service.h").read_text(encoding="utf-8")
        self.assertIn("void SetInputPaused(bool paused);", header)
        self.assertIn("void ResetCapture();", header)

    def compile_and_run(self, program, scenario):
        compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not compiler or shutil.which(compiler[0]) is None:
            self.skipTest("requires a host C++17 compiler")
        with tempfile.TemporaryDirectory() as directory:
            cpp = Path(directory) / "capture.cc"
            binary = Path(directory) / "capture"
            cpp.write_text(program, encoding="utf-8")
            result = subprocess.run(compiler + ["-std=c++17", "-pthread", str(cpp), "-o", str(binary)],
                                    capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            result = subprocess.run([str(binary), scenario], capture_output=True, text=True, timeout=15)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def service_program(self):
        names = ["AudioService::AudioService()", "AudioService::~AudioService()",
                 "AudioService::SetInputPaused(", "AudioService::ResetCapture()",
                 "AudioService::ResetCaptureLocked()", "AudioService::AudioInputTask()",
                 "AudioService::ReadAudioData(", "AudioService::OpusCodecTask()",
                 "AudioService::PushTaskToEncodeQueue(", "AudioService::EnableWakeWordDetection(",
                 "AudioService::EnableVoiceProcessing(", "AudioService::EnableAudioTesting(",
                 "AudioService::InitializeAudioEngine()", "AudioService::IsPlaybackDrainedLocked()",
                 "AudioService::MarkPlaybackDrainedLocked()"]
        names += ["AudioService::CancelLocalSound(", "AudioService::IsLocalSoundCurrentLocked(",
                  "AudioService::FinishLocalSoundPacketLocked(",
                  "AudioService::RemoveLocalSoundPacketsLocked("]
        return (textwrap.dedent(PLATFORM)
                + without_includes(source("main/audio/fixed_queue.h"))
                + without_includes(source("main/audio/audio_engine.h"))
                + without_includes(source("main/audio/audio_service.h")).replace("private:", "public:")
                + FAKE_ENGINE
                + "\n".join(method("main/audio/audio_service.cc", name) for name in names)
                + r'''
                    void AudioService::ResetDecoder() {}
                    void AudioService::SetDecodeSampleRate(int, int) {}
                    int main(int argc, char** argv) {
                        AudioService audio;
                        AudioCodec codec;
                        audio.codec_ = &codec;
                        audio.service_stopped_ = false;
                        auto engine = std::make_unique<FakeEngine>();
                        auto* fake = engine.get();
                        audio.audio_engine_ = std::move(engine);
                        audio.audio_engine_initialized_ = true;
                        std::string scenario = argv[1];
                        if (scenario == "gate") {
                            audio.EnableWakeWordDetection(true);
                            audio.EnableVoiceProcessing(true);
                            audio.EnableAudioTesting(true);
                            audio.SetInputPaused(true);
                            audio.EnableWakeWordDetection(true);
                            audio.EnableVoiceProcessing(true);
                            audio.EnableAudioTesting(true);
                            assert(!fake->wake && !fake->voice);
                            assert(!(xEventGroupGetBits(audio.event_group_) & 7));
                            audio.SetInputPaused(false);
                            assert(!(xEventGroupGetBits(audio.event_group_) & 7));
                            audio.EnableWakeWordDetection(true);
                            assert(fake->wake);
                        } else if (scenario == "disable-vad") {
                            audio.EnableVoiceProcessing(true);
                            audio.voice_detected_.store(true);
                            audio.EnableVoiceProcessing(false);
                            assert(!audio.IsVoiceDetected());
                            audio.EnableVoiceProcessing(true);
                            assert(!audio.IsVoiceDetected());
                        } else if (scenario == "read") {
                            audio.EnableVoiceProcessing(true);
                            audio.audio_input_need_warmup_ = false;
                            std::thread input([&] { audio.AudioInputTask(); });
                            read_gate.Until(1);
                            audio.SetInputPaused(true);
                            audio.SetInputPaused(false);
                            audio.EnableVoiceProcessing(true);
                            audio.audio_input_need_warmup_ = false;
                            read_gate.Release();
                            read_gate.Until(2);
                            assert(fake->feeds == 0);
                            audio.service_stopped_ = true;
                            read_gate.Release();
                            input.join();
                        } else {
                            auto type = scenario == "encode_testing" ? kAudioTaskTypeEncodeToTestingQueue
                                                                     : kAudioTaskTypeEncodeToSendQueue;
                            audio.opus_encoder_ = reinterpret_cast<void*>(1);
                            audio.encoder_frame_size_ = 2;
                            audio.encoder_outbuf_size_ = 2;
                            std::thread encoder([&] { audio.OpusCodecTask(); });
                            audio.PushTaskToEncodeQueue(type, {1, 1});
                            encode_gate.Until(1);
                            audio.ResetCapture();
                            audio.PushTaskToEncodeQueue(type, {2, 2});
                            encode_gate.Release();
                            encode_gate.Until(2);
                            {
                                std::lock_guard<std::mutex> lock(audio.audio_queue_mutex_);
                                assert(audio.audio_send_queue_.empty());
                                assert(audio.audio_testing_queue_.empty());
                            }
                            audio.PushTaskToEncodeQueue(type, {3, 3});
                            encode_gate.Release();
                            encode_gate.Until(3);
                            assert(encoder_resets == 2);
                            {
                                std::lock_guard<std::mutex> lock(audio.audio_queue_mutex_);
                                auto& queue = audio.audio_testing_queue_;
                                if (type == kAudioTaskTypeEncodeToTestingQueue) {
                                    assert(queue.size() == 1 && queue.front()->payload[0] == 2);
                                } else {
                                    assert(audio.audio_send_queue_.size() == 1);
                                    assert(audio.audio_send_queue_.front()->payload[0] == 2);
                                }
                                audio.service_stopped_ = true;
                                audio.audio_queue_cv_.notify_all();
                            }
                            encode_gate.Release();
                            encoder.join();
                        }
                    }
                ''')

    def test_pause_cannot_be_overridden_and_resume_does_not_start_capture(self):
        self.compile_and_run(self.service_program(), "gate")

    def test_old_read_cannot_feed_a_resumed_engine(self):
        self.compile_and_run(self.service_program(), "read")

    def test_disabled_voice_processing_retires_its_vad_observation(self):
        self.compile_and_run(self.service_program(), "disable-vad")

    def test_dequeued_encoder_cannot_repopulate_send_after_reset(self):
        self.compile_and_run(self.service_program(), "encode_send")

    def test_dequeued_encoder_cannot_repopulate_testing_after_reset(self):
        self.compile_and_run(self.service_program(), "encode_testing")

    def debugger_program(self):
        # The clock is a system boundary immediately after the first generation
        # check. Blocking it reproduces a completed read that has not acquired
        # the debugger publication fence yet, without editing capture logic.
        platform = r'''
            std::atomic<int> debugger_publications{0};
            bool debugger_blocked = false, read_completion_blocked = false;
            Gate debugger_gate, read_completion_gate;
            struct AudioDebugger {
                void Feed(const std::vector<int16_t>&) {
                    if (debugger_blocked) debugger_gate.Wait();
                    ++debugger_publications;
                }
            };
            struct ReadCompletionClock {
                static std::chrono::steady_clock::time_point now() {
                    if (read_completion_blocked) read_completion_gate.Wait();
                    return std::chrono::steady_clock::now();
                }
            };
        '''
        program = self.service_program().split("int main(int argc, char** argv)", 1)[0]
        program = program.replace("std::chrono::steady_clock::now()", "ReadCompletionClock::now()")
        program = program.replace("struct AudioDebugger {};", platform)
        return "#define CONFIG_USE_AUDIO_DEBUGGER 1\n" + program + r'''
            int main(int, char** argv) {
                AudioService audio;
                AudioCodec codec;
                audio.codec_ = &codec;
                audio.service_stopped_ = false;
                audio.audio_engine_ = std::make_unique<FakeEngine>();
                audio.audio_engine_initialized_ = true;
                const std::string scenario = argv[1];
                if (scenario == "debug_paused") {
                    audio.SetInputPaused(true);
                    std::vector<int16_t> data;
                    assert(!audio.ReadAudioData(data, 16000, 2));
                    assert(read_gate.entered == 0 && !codec.input_enabled());
                    assert(audio.audio_debugger_ == nullptr && debugger_publications == 0);
                    return 0;
                }

                read_completion_blocked = scenario == "debug_after_read";
                debugger_blocked = scenario == "debug_in_flight";
                auto read = std::async(std::launch::async, [&] {
                    std::vector<int16_t> data;
                    return audio.ReadAudioData(data, 16000, 2);
                });
                read_gate.Until(1);
                if (scenario == "debug_old_read") {
                    audio.SetInputPaused(true);
                    audio.SetInputPaused(false);
                    read_gate.Release();
                    assert(read.wait_for(2s) == std::future_status::ready && !read.get());
                    assert(debugger_publications == 0);
                } else if (scenario == "debug_after_read") {
                    read_gate.Release();
                    read_completion_gate.Until(1);
                    audio.SetInputPaused(true);
                    audio.SetInputPaused(false);
                    read_completion_gate.Release();
                    assert(read.wait_for(2s) == std::future_status::ready && !read.get());
                    assert(debugger_publications == 0);
                } else {
                    read_gate.Release();
                    debugger_gate.Until(1);
                    Gate pause_started;
                    auto pause = std::async(std::launch::async, [&] {
                        pause_started.Release();
                        audio.SetInputPaused(true);
                    });
                    pause_started.Wait();
                    assert(pause.wait_for(30ms) == std::future_status::timeout);
                    debugger_gate.Release();
                    assert(pause.wait_for(2s) == std::future_status::ready);
                    pause.get();
                    assert(read.wait_for(2s) == std::future_status::ready && read.get());
                    // An already-entered publication must finish before pause
                    // returns. No old read remains able to publish afterward.
                    assert(audio.input_paused() && debugger_publications == 1);
                }
            }
        '''

    def test_debugger_paused_input_does_not_read_or_publish(self):
        self.compile_and_run(self.debugger_program(), "debug_paused")

    def test_debugger_old_read_across_pause_resume_does_not_publish(self):
        self.compile_and_run(self.debugger_program(), "debug_old_read")

    def test_debugger_completed_old_read_cannot_publish_after_pause_fence(self):
        self.compile_and_run(self.debugger_program(), "debug_after_read")

    def test_debugger_publication_in_flight_finishes_before_pause_returns(self):
        self.compile_and_run(self.debugger_program(), "debug_in_flight")

    def test_wake_encoder_cancellation_preserves_new_session_and_releases_waiter(self):
        header = without_includes(source("main/audio/wake_words/wake_word_audio_cache.h"))
        header = header.replace("private:", "public:").replace(
            "std::condition_variable cv_;", "TrackedConditionVariable cv_;")
        self.compile_and_run(textwrap.dedent(PLATFORM) + header
                             + without_includes(source("main/audio/wake_words/wake_word_audio_cache.cc"))
                             + r'''
            int main(int, char**) {
                WakeWordAudioEncoder encoder;
                assert(encoder.Initialize(8));
                const int16_t old_pcm[] = {1, 1}, fresh_pcm[] = {2, 2};
                encoder.Store(old_pcm, 2);
                encoder.Encode({});
                encode_gate.Until(1);
                Gate waiting;
                encoder.cv_.waiting = &waiting;
                auto waiter = std::async(std::launch::async, [&] {
                    std::vector<uint8_t> opus;
                    return encoder.GetOpus(opus);
                });
                waiting.Wait();
                encoder.Reset();
                assert(waiter.wait_for(2s) == std::future_status::ready && !waiter.get());
                encoder.cv_.waiting = nullptr;
                encoder.Store(fresh_pcm, 2);
                encoder.Encode({});
                encode_gate.Release();
                encode_gate.Until(2);
                encode_gate.Release();
                std::vector<uint8_t> opus;
                assert(encoder.GetOpus(opus) && opus.size() == 1 && opus[0] == 2);
                assert(!encoder.GetOpus(opus));
                assert(task_creations == 1);
            }
        ''', "wake")

    def afe_program(self):
        names = ["AfeAudioEngine::AfeAudioEngine()", "AfeAudioEngine::Feed(",
                 "AfeAudioEngine::EnableWakeWordDetection(", "AfeAudioEngine::EnableVoiceProcessing(",
                 "AfeAudioEngine::ResetCapture()", "AfeAudioEngine::EnableDeviceAec(",
                 "AfeAudioEngine::HasWakeWord()", "AfeAudioEngine::GetWakeWords()",
                 "AfeAudioEngine::IsWakeWordDetectionEnabled()",
                 "AfeAudioEngine::IsVoiceProcessingEnabled()", "AfeAudioEngine::GetFeedSize()",
                 "AfeAudioEngine::OnWakeWordDetected(", "AfeAudioEngine::OnOutput(",
                 "AfeAudioEngine::OnVadStateChange(", "AfeAudioEngine::UpdateActiveState()",
                 "AfeAudioEngine::UpdateAecState()", "AfeAudioEngine::ApplyAfeControls()",
                 "AfeAudioEngine::ApplyPendingReset()", "AfeAudioEngine::ProcessingTask()",
                 "AfeAudioEngine::HandleWakeWordResult(", "AfeAudioEngine::HandleVoiceResult(",
                 "AfeAudioEngine::OutputRawAudio(", "AfeAudioEngine::EncodeWakeWordData()",
                 "AfeAudioEngine::GetWakeWordOpus("]
        return (textwrap.dedent(PLATFORM)
                + without_includes(source("main/audio/audio_engine.h"))
                + without_includes(source("main/audio/wake_words/wake_word_audio_cache.h"))
                + without_includes(source("main/audio/wake_words/wake_word_audio_cache.cc"))
                + r'''
                    #define AS_OPUS_ENC_CONFIG() {}
                    constexpr bool kUseAfeForVoiceProcessing = true;
                    enum { WAKENET_DETECTED = 1, VAD_SPEECH = 1, VAD_SILENCE = 0 };
                    struct esp_afe_sr_data_t {};
                    struct afe_fetch_result_t {
                        int16_t* data;
                        int data_size=4, ret_value=0, wakeup_state=0;
                        int wakenet_model_index=1, vad_state=VAD_SPEECH;
                    };
                    int16_t samples[] = {1, 1};
                    afe_fetch_result_t result{samples};
                    struct esp_afe_sr_iface_t {
                        int (*get_feed_chunksize)(esp_afe_sr_data_t*) =
                            [](esp_afe_sr_data_t*) { return 2; };
                        void (*feed)(esp_afe_sr_data_t*, int16_t*) = [](esp_afe_sr_data_t*, int16_t*) {};
                        afe_fetch_result_t* (*fetch_with_delay)(esp_afe_sr_data_t*, uint32_t) =
                            [](esp_afe_sr_data_t*, uint32_t) {
                                fetch_gate.Wait();
                                if (stop_afe) throw StopTask{};
                                return &result;
                            };
                        void (*reset_buffer)(esp_afe_sr_data_t*) = [](esp_afe_sr_data_t*) {};
                        void (*enable_wakenet)(esp_afe_sr_data_t*) = [](esp_afe_sr_data_t*) {};
                        void (*disable_wakenet)(esp_afe_sr_data_t*) = [](esp_afe_sr_data_t*) {};
                        void (*enable_aec)(esp_afe_sr_data_t*) = [](esp_afe_sr_data_t*) {};
                        void (*disable_aec)(esp_afe_sr_data_t*) = [](esp_afe_sr_data_t*) {};
                    };
                    struct CustomWakeWord {
                        void Start() {} void Stop() {} void ResetCapture() {}
                        void FeedMono(const int16_t*, size_t) {}
                        std::vector<std::string> GetWakeWords() const { return {}; }
                        void EncodeWakeWordData() {}
                        bool GetWakeWordOpus(std::vector<uint8_t>&) { return false; }
                    };
                '''
                + without_includes(source("main/audio/engines/afe_audio_engine.h")).replace(
                    "private:", "public:")
                + "\n".join(method("main/audio/engines/afe_audio_engine.cc", name) for name in names)
                + r'''
                    AfeAudioEngine::~AfeAudioEngine() = default;
                    bool AfeAudioEngine::Initialize(AudioCodec*, int, srmodel_list_t*) { return true; }
                    int main(int, char** argv) {
                        AfeAudioEngine engine;
                        AudioCodec codec;
                        esp_afe_sr_data_t data;
                        esp_afe_sr_iface_t iface;
                        engine.codec_ = &codec;
                        engine.afe_data_ = &data;
                        engine.afe_iface_ = &iface;
                        engine.frame_samples_ = 2;
                        std::atomic<int> outputs{0};
                        std::string scenario = argv[1];
                        const bool wake = scenario == "wake_callback";
                        auto callback = [&] {
                            callback_gate.Wait();
                            ++outputs;
                        };
                        if (wake) {
                            engine.wake_detector_ = AfeAudioEngine::WakeDetector::kWakeNet;
                            engine.wake_words_ = {"hello"};
                            result.wakeup_state = WAKENET_DETECTED;
                            engine.OnWakeWordDetected([&](const std::string&) { callback(); });
                            engine.EnableWakeWordDetection(true);
                        } else {
                            engine.OnOutput([&](std::vector<int16_t>&&) { callback(); });
                            engine.EnableVoiceProcessing(true);
                        }
                        std::thread processing([&] {
                            try { engine.ProcessingTask(); } catch (StopTask&) {}
                        });
                        fetch_gate.Until(1);
                        if (scenario == "fetch") {
                            engine.ResetCapture();
                            engine.EnableVoiceProcessing(true);
                            fetch_gate.Release();
                            fetch_gate.Until(2);
                            assert(outputs == 0);
                        } else {
                            fetch_gate.Release();
                            callback_gate.Until(1);
                            Gate reset_started;
                            auto reset = std::async(std::launch::async, [&] {
                                reset_started.Release();
                                engine.ResetCapture();
                            });
                            reset_started.Wait();
                            assert(reset.wait_for(30ms) == std::future_status::timeout);
                            callback_gate.Release();
                            assert(reset.wait_for(2s) == std::future_status::ready);
                            reset.get();
                            assert(outputs == 1);
                        }
                        stop_afe = true;
                        xEventGroupSetBits(engine.event_group_, 7);
                        fetch_gate.Release(10);
                        processing.join();
                        assert(outputs == (scenario == "fetch" ? 0 : 1));
                    }
                ''')

    def test_afe_fetch_from_old_generation_cannot_reach_new_session(self):
        self.compile_and_run(self.afe_program(), "fetch")

    def test_afe_reset_waits_for_output_callback_commit(self):
        self.compile_and_run(self.afe_program(), "voice_callback")

    def test_afe_reset_waits_for_wake_callback_commit(self):
        self.compile_and_run(self.afe_program(), "wake_callback")

    def test_multinet_reset_fences_detect_callback_and_clears_encoder_cache(self):
        names = ["CustomWakeWord::Start()", "CustomWakeWord::Stop()",
                 "CustomWakeWord::ResetCapture()", "CustomWakeWord::Feed(",
                 "CustomWakeWord::FeedMono(", "CustomWakeWord::FeedSamples(",
                 "CustomWakeWord::OnWakeWordDetected(", "CustomWakeWord::GetFeedSize()",
                 "CustomWakeWord::EncodeWakeWordData()", "CustomWakeWord::GetWakeWordOpus(",
                 "CustomWakeWord::GetWakeWords()"]
        self.compile_and_run(textwrap.dedent(PLATFORM)
                             + without_includes(source("main/audio/wake_word.h"))
                             + without_includes(source("main/audio/wake_words/wake_word_audio_cache.h")).replace(
                                 "private:", "public:")
                             + without_includes(source("main/audio/wake_words/wake_word_audio_cache.cc"))
                             + r'''
                #define AS_OPUS_ENC_CONFIG() {}
                struct model_iface_data_t {};
                enum esp_mn_state_t { ESP_MN_STATE_DETECTED, ESP_MN_STATE_TIMEOUT };
                struct esp_mn_results_t { int num = 1; int command_id[1] = {1}; };
                esp_mn_results_t mn_result;
                struct esp_mn_iface_t {
                    int (*get_samp_chunksize)(model_iface_data_t*) = [](model_iface_data_t*) { return 2; };
                    esp_mn_state_t (*detect)(model_iface_data_t*, int16_t*) =
                        [](model_iface_data_t*, int16_t*) { return ESP_MN_STATE_DETECTED; };
                    esp_mn_results_t* (*get_results)(model_iface_data_t*) =
                        [](model_iface_data_t*) { return &mn_result; };
                    void (*clean)(model_iface_data_t*) = [](model_iface_data_t*) {};
                };
            '''
                             + without_includes(source("main/audio/wake_words/custom_wake_word.h")).replace(
                                 "private:", "public:")
                             + "\n".join(method("main/audio/wake_words/custom_wake_word.cc", name)
                                         for name in names)
                             + r'''
                CustomWakeWord::CustomWakeWord() = default;
                CustomWakeWord::~CustomWakeWord() = default;
                bool CustomWakeWord::Initialize(AudioCodec*, srmodel_list_t*) { return true; }
                int main(int, char**) {
                    CustomWakeWord wake;
                    AudioCodec codec;
                    model_iface_data_t model;
                    esp_mn_iface_t iface;
                    wake.codec_ = &codec;
                    wake.multinet_ = &iface;
                    wake.multinet_model_data_ = &model;
                    wake.commands_.push_back({"hello", "hello", "wake"});
                    assert(wake.wake_word_encoder_.Initialize(8));
                    std::atomic<int> callbacks{0};
                    wake.OnWakeWordDetected([&](const std::string&) {
                        callback_gate.Wait();
                        ++callbacks;
                    });
                    wake.Start();
                    const int16_t pcm[] = {1, 1};
                    std::thread feed([&] { wake.FeedMono(pcm, 2); });
                    callback_gate.Until(1);
                    Gate started;
                    auto reset = std::async(std::launch::async, [&] {
                        started.Release();
                        wake.ResetCapture();
                    });
                    started.Wait();
                    assert(reset.wait_for(30ms) == std::future_status::timeout);
                    callback_gate.Release();
                    feed.join();
                    assert(reset.wait_for(2s) == std::future_status::ready);
                    reset.get();
                    assert(callbacks == 1 && !wake.running_);
                    assert(wake.input_buffer_.empty());
                    assert(wake.wake_word_encoder_.cache_.Size() == 0);
                    std::vector<uint8_t> opus;
                    assert(!wake.GetWakeWordOpus(opus));
                }
            ''',
                             "multinet")


if __name__ == "__main__":
    unittest.main()
