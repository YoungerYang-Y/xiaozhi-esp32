"""Host barriers for bounded local Ogg admission, cancellation and checked PCM output."""

import unittest
import subprocess
import tempfile
from pathlib import Path

from test_audio_capture_pause import PLATFORM, method, source, without_includes
from test_checked_settings import compiler_command
from test_home_volume import (NVS_PLATFORM, AUDIO_PLATFORM, CODEC_PLATFORM, audio_source,
                              box_source, settings_source, vendor_volume_source)


ROOT = Path(__file__).resolve().parents[2]


class LocalSoundContractTest(unittest.TestCase):
    def test_admission_and_cancellation_are_explicit(self):
        header = (ROOT / "main/audio/audio_service.h").read_text(encoding="utf-8")
        self.assertIn("bool TryPlayLocalSound(std::string_view sound, uint32_t id);", header)
        self.assertIn("void CancelLocalSound(uint32_t id = 0);", header)
        self.assertIn("on_local_sound_started", header)
        self.assertIn("on_local_sound_finished", header)


LOCAL_PLATFORM = r'''
#define pdPASS 1
std::atomic<bool> fail_task_create{false};
std::vector<HostTask*> local_tasks;
int xTaskCreate(void (*f)(void*), const char* name, size_t, void* arg, int, TaskHandle_t* handle) {
    if (std::string(name)!="local_sound") { *handle=nullptr; return pdPASS; }
    if (fail_task_create) return 0;
    auto* task = new HostTask;
    *handle = task;
    local_tasks.push_back(task);
    ++task_creations;
    task->thread = std::thread([=] { current_task=task; f(arg); });
    return pdPASS;
}
int xTaskCreatePinnedToCore(void (*f)(void*), const char* name, size_t stack, void* arg,
                            int priority, TaskHandle_t* handle, int) {
    return xTaskCreate(f,name,stack,arg,priority,handle);
}
void join_local_tasks() {
    for (auto* task : local_tasks) { task->thread.join(); delete task; }
    local_tasks.clear();
}
Gate commit_gate, output_gate, decode_gate, enable_gate, wait_gap_gate, post_write_gate, worker_exit_gate;
std::atomic<bool> pause_after_successful_write{false}, pause_worker_exit_unlock{false};
std::function<bool()> worker_exit_condition;
struct TrackedServiceMutex : std::mutex {
    void unlock() {
        std::mutex::unlock();
        if (current_task && pause_worker_exit_unlock && worker_exit_condition &&
            worker_exit_condition() && pause_worker_exit_unlock.exchange(false)) worker_exit_gate.Wait();
    }
};
std::atomic<int> commit_visit{0}, commit_hold_visit{0};
std::atomic<int> queue_wait_count{0};
std::atomic<bool> lose_cancel_notification{false};
std::atomic<bool> hold_output{false}, hold_decode{false}, hold_enable{false};
std::atomic<int> output_attempts{0}, legacy_writes{0};
std::atomic<int> output_hold_visit{0}, output_fail_visit{0};
std::atomic<bool> decode_ok{true}, decode_empty{false};
int esp_opus_dec_decode(void*, esp_audio_dec_in_raw_t*, esp_audio_dec_out_frame_t* out,
                       esp_audio_dec_info_t*) {
    if (hold_decode.exchange(false)) decode_gate.Wait();
    if (!decode_ok) return -1;
    out->decoded_size = decode_empty ? 0 : 4;
    return ESP_AUDIO_ERR_OK;
}
struct OutputVolumeStatus { int volume; bool persisted, applied; int error; };
struct AudioCodec {
    bool supported=true, enable_ok=true, write_ok=true;
    std::atomic<bool> enabled{false};
    std::atomic<int> volume{30};
    int output_sample_rate() const { return 16000; }
    bool output_enabled() const { return enabled; }
    bool SupportsCheckedOutput() const { return supported; }
    OutputVolumeStatus GetOutputVolumeStatus() const { return {volume, true, enabled, 0}; }
    void EnableOutput(bool value) {
        if (hold_enable.exchange(false)) enable_gate.Wait();
        enabled = value && enable_ok;
    }
    bool OutputDataChecked(std::vector<int16_t>& pcm) {
        int visit=++output_attempts;
        if (hold_output.exchange(false) || visit==output_hold_visit) output_gate.Wait();
        bool written=enabled && volume > 0 && write_ok && visit!=output_fail_visit && !pcm.empty();
        if (written && pause_after_successful_write.exchange(false)) post_write_gate.Wait();
        return written;
    }
    void OutputData(std::vector<int16_t>&) { ++legacy_writes; }
};
struct TrackedConditionVariable {
    std::condition_variable_any cv;
    template<class Mutex, class Predicate> void wait(std::unique_lock<Mutex>& lock, Predicate p) {
        cv.wait(lock, p);
    }
    template<class Mutex, class Time, class Predicate>
    bool wait_until(std::unique_lock<Mutex>& lock, Time deadline, Predicate p) {
        int visit = ++commit_visit;
        if (visit == commit_hold_visit) { lock.unlock(); commit_gate.Wait(); lock.lock(); }
        if (!p()) {
            ++queue_wait_count; cv.notify_all();
            if (lose_cancel_notification.exchange(false)) {
                // Reproduce notify after predicate evaluation but before wait.
                wait_gap_gate.Wait();
                cv.wait_until(lock, deadline);
                return p();
            }
        }
        return cv.wait_until(lock, deadline, p);
    }
    template<class Mutex, class Time>
    std::cv_status wait_until(std::unique_lock<Mutex>& lock, Time deadline) {
        return cv.wait_until(lock, deadline);
    }
    void notify_all() { cv.notify_all(); }
};
'''


CHECKS = r'''
std::string ogg(int packets=2, uint8_t toc=24) {
    std::string head="OpusHead";
    head.resize(19, 0); head[9]=1; head[12]=0x80; head[13]=0x3e;
    std::string data(27, 0); data.replace(0,4,"OggS"); data[26]=packets+2;
    data.push_back(19); data.push_back(8);
    for(int i=0;i<packets;++i) data.push_back(1);
    data += head; data += "OpusTags";
    for(int i=0;i<packets;++i) data.push_back(toc);
    return data;
}
struct Receipts {
    std::mutex mutex; std::condition_variable cv;
    std::vector<uint32_t> starts;
    std::vector<std::pair<uint32_t,bool>> finishes;
    std::vector<std::pair<uint64_t,uint64_t>> decoded;
    std::vector<std::string> order;
    std::atomic<int> drains{0};
    AudioServiceCallbacks Callbacks() {
        AudioServiceCallbacks cb;
        cb.on_local_sound_started=[&](uint32_t id) {
            std::lock_guard<std::mutex> lock(mutex); starts.push_back(id); order.push_back("started"); cv.notify_all();
        };
        cb.on_local_sound_finished=[&](uint32_t id,bool completed) {
            std::lock_guard<std::mutex> lock(mutex); finishes.emplace_back(id,completed); order.push_back("finished"); cv.notify_all();
        };
        cb.on_incoming_audio_decoded=[&](uint64_t token,uint64_t request) {
            std::lock_guard<std::mutex> lock(mutex); decoded.emplace_back(token,request); cv.notify_all();
        };
        cb.on_playback_drained=[&] {
            std::lock_guard<std::mutex> lock(mutex); ++drains; cv.notify_all();
        };
        return cb;
    }
    void Finished(int count) {
        std::unique_lock<std::mutex> lock(mutex);
        assert(cv.wait_for(lock, 3s, [&] { return finishes.size() >= count; }));
    }
    void Started(int count) {
        std::unique_lock<std::mutex> lock(mutex);
        assert(cv.wait_for(lock, 3s, [&] { return starts.size() >= count; }));
    }
    void NoFinishedFor(std::chrono::milliseconds duration=50ms) {
        std::unique_lock<std::mutex> lock(mutex);
        assert(!cv.wait_for(lock,duration,[&] { return !finishes.empty(); }));
    }
};
void idle(AudioService& audio) {
    std::unique_lock<std::mutex> lock(audio.audio_queue_mutex_);
    assert(audio.audio_queue_cv_.cv.wait_for(lock,3s,[&] { return !audio.IsLocalSoundBusy(); }));
}
void pending(AudioService& audio, size_t count) {
    std::unique_lock<std::mutex> lock(audio.audio_queue_mutex_);
    assert(audio.audio_queue_cv_.cv.wait_for(lock,3s,[&] {
        return audio.local_sound_pending_packets_ >= count;
    }));
}
std::unique_ptr<AudioStreamPacket> packet(uint64_t token=0, uint32_t id=0) {
    auto p=std::make_unique<AudioStreamPacket>();
    p->payload={24}; p->source_channel_token=token; p->source_request_id=token+1000; p->playback_id=id;
    return p;
}
int main(int argc,char** argv) {
    assert(argc==2); std::string scenario=argv[1];
    Receipts receipts;
    {
        AudioCodec codec; AudioService audio;
        audio.codec_=&codec; audio.service_stopped_=false;
        audio.opus_decoder_=reinterpret_cast<void*>(1); audio.decoder_frame_size_=960;
        audio.decoder_sample_rate_=16000;
        auto callbacks=receipts.Callbacks(); audio.SetCallbacks(callbacks);
        std::thread decoder, output;
        std::future<void> stop_waiter;
        auto run=[&] {
            decoder=std::thread([&] { audio.OpusCodecTask(); });
            output=std::thread([&] { audio.AudioOutputTask(); });
        };
        if (scenario=="worker-restart") {
            run(); assert(audio.TryPlayLocalSound(ogg(),1)); receipts.Finished(1); idle(audio);
            worker_exit_condition=[&] { return audio.service_stopped_.load() && !audio.IsLocalSoundBusy(); };
            pause_worker_exit_unlock=true;
            audio.Stop(); decoder.join(); output.join();
            worker_exit_gate.Until(1);
            audio.Start();
            assert(audio.TryPlayLocalSound(ogg(),2));
            assert(task_creations==2); // Retired old handle must not suppress a replacement worker.
            auto new_handle=audio.local_sound_task_handle_;
            worker_exit_gate.Release();
            run(); receipts.Finished(2); idle(audio);
            assert(audio.local_sound_task_handle_==new_handle);
            assert(receipts.finishes[1]==std::make_pair(2u,true));
        } else if (scenario=="latch-before-cancel" || scenario=="write-before-cancel" ||
                   scenario=="write-before-reset" || scenario=="write-before-stop" ||
                   scenario=="inflight-failure-cancel" || scenario=="deadline-decode") {
            if(scenario=="latch-before-cancel") commit_hold_visit=2;
            if(scenario.find("write-before-")==0) pause_after_successful_write=true;
            if(scenario=="inflight-failure-cancel") { hold_output=true; codec.write_ok=false; }
            if(scenario=="deadline-decode") hold_decode=true;
            assert(audio.TryPlayLocalSound(ogg(),1));
            if(scenario=="latch-before-cancel") commit_gate.Until(1);
            run();
            if(scenario=="latch-before-cancel") {
                std::unique_lock<std::mutex> lock(audio.audio_queue_mutex_);
                assert(audio.audio_queue_cv_.cv.wait_for(lock,3s,[&] { return audio.local_sound_started_; }));
                lock.unlock(); audio.CancelLocalSound(); commit_gate.Release();
            } else if(scenario.find("write-before-")==0) {
                post_write_gate.Until(1);
                if(scenario=="write-before-reset") audio.ResetDecoder();
                else if(scenario=="write-before-stop") {
                    stop_waiter=std::async(std::launch::async,[&] { audio.Stop(); });
                    assert(stop_waiter.wait_for(30ms)==std::future_status::timeout);
                }
                else audio.CancelLocalSound();
                receipts.NoFinishedFor();
                assert(audio.IsLocalSoundBusy());
                post_write_gate.Release();
            } else if(scenario=="inflight-failure-cancel") {
                output_gate.Until(1); audio.CancelLocalSound(); receipts.NoFinishedFor();
                assert(audio.IsLocalSoundBusy()); output_gate.Release();
            } else {
                decode_gate.Until(1);
                receipts.NoFinishedFor(250ms); // Fixture deadline is 120ms; hardware remains in flight.
                assert(audio.IsLocalSoundBusy() && audio.local_sound_live_generation_.load()==0);
                assert(audio.PushPacketToDecodeQueue(packet(88)));
                decode_gate.Release();
            }
            receipts.Finished(1); idle(audio);
            if (stop_waiter.valid()) stop_waiter.get();
            assert(!receipts.finishes[0].second);
            bool wrote=scenario=="latch-before-cancel" || scenario.find("write-before-")==0;
            assert(receipts.starts.size()==(wrote?1:0));
            assert(receipts.order==(wrote ? std::vector<std::string>{"started","finished"} :
                                          std::vector<std::string>{"finished"}));
            if(scenario=="deadline-decode") {
                std::unique_lock<std::mutex> lock(audio.audio_queue_mutex_);
                assert(audio.audio_queue_cv_.cv.wait_for(lock,3s,[&] { return audio.IsPlaybackDrainedLocked(); }));
                assert(output_attempts==0 && legacy_writes==1);
            }
        } else if (scenario=="admission") {
            assert(!audio.TryPlayLocalSound(ogg(),0));
            assert(!audio.TryPlayLocalSound({},1));
            assert(!audio.TryPlayLocalSound(std::string(65537,'x'),1));
            codec.supported=false; assert(!audio.TryPlayLocalSound(ogg(),1)); codec.supported=true;
            fail_task_create=true; assert(!audio.TryPlayLocalSound(ogg(),1));
            assert(!audio.IsLocalSoundBusy()); fail_task_create=false;
            std::unique_lock<std::mutex> lock(audio.audio_queue_mutex_);
            auto attempt=std::async(std::launch::async,[&] { return audio.TryPlayLocalSound(ogg(),1); });
            assert(attempt.wait_for(100ms)==std::future_status::ready && !attempt.get());
            lock.unlock();
            assert(audio.TryPlayLocalSound(ogg(),1)); pending(audio,2);
            assert(!audio.CommitLocalSoundPacket(packet(),1,std::chrono::steady_clock::now()-1ms));
            assert(!audio.TryPlayLocalSound(ogg(),2));
            audio.CancelLocalSound(99); assert(audio.IsLocalSoundBusy());
            lock.lock();
            audio.audio_decode_queue_.push_back(packet(88));
            auto cancel=std::async(std::launch::async,[&] { audio.CancelLocalSound(1); });
            assert(cancel.wait_for(100ms)==std::future_status::ready); cancel.get();
            lock.unlock(); receipts.Finished(1); idle(audio);
            assert(receipts.starts.empty() && !receipts.finishes[0].second);
            assert(audio.audio_decode_queue_.size()==1);
            assert(audio.audio_decode_queue_.front()->source_channel_token==88);
            audio.audio_decode_queue_.clear();
            assert(task_creations==1);
        } else if (scenario=="cancel-before-commit" || scenario=="reset-before-commit" ||
                   scenario=="stop-before-commit" || scenario=="cancel-queue-wait" ||
                   scenario=="cancel-lost-wakeup") {
            commit_hold_visit=1;
            assert(audio.TryPlayLocalSound(ogg(),1)); commit_gate.Until(1);
            if (scenario=="reset-before-commit") audio.ResetDecoder();
            if (scenario=="stop-before-commit") {
                stop_waiter=std::async(std::launch::async,[&] { audio.Stop(); });
                assert(stop_waiter.wait_for(30ms)==std::future_status::timeout);
            }
            if (scenario=="cancel-queue-wait" || scenario=="cancel-lost-wakeup") {
                std::lock_guard<std::mutex> lock(audio.audio_queue_mutex_);
                for(int i=0;i<MAX_DECODE_PACKETS_IN_QUEUE;++i)
                    audio.audio_decode_queue_.push_back(packet(88));
            }
            if (scenario=="cancel-lost-wakeup") lose_cancel_notification=true;
            if (scenario!="cancel-queue-wait" && scenario!="cancel-lost-wakeup") audio.CancelLocalSound();
            commit_gate.Release();
            if (scenario=="cancel-queue-wait") {
                std::unique_lock<std::mutex> lock(audio.audio_queue_mutex_);
                assert(audio.audio_queue_cv_.cv.wait_for(lock,3s,[&] { return queue_wait_count>0; }));
                audio.CancelLocalSound();
            }
            if (scenario=="cancel-lost-wakeup") {
                wait_gap_gate.Until(1);
                audio.CancelLocalSound(); wait_gap_gate.Release();
            }
            receipts.Finished(1); idle(audio);
            assert(receipts.starts.empty() && !receipts.finishes[0].second);
            assert(audio.audio_decode_queue_.size()==(
                scenario=="cancel-queue-wait" || scenario=="cancel-lost-wakeup" ? 20:0));
            audio.audio_decode_queue_.clear();
            if(scenario=="stop-before-commit") {
                stop_waiter.get();
                std::unique_lock<std::mutex> lock(audio.audio_queue_mutex_);
                assert(audio.audio_queue_cv_.cv.wait_for(lock,3s,[&] { return audio.local_sound_task_handle_==nullptr; }));
                audio.service_stopped_=false;
            }
            run(); assert(audio.TryPlayLocalSound(ogg(),2));
            assert(!audio.CommitLocalSoundPacket(packet(),1,std::chrono::steady_clock::now()+100ms));
            receipts.Finished(2); idle(audio);
            assert(receipts.starts.size()==1 && receipts.starts[0]==2);
            assert(receipts.finishes[1]==std::make_pair(2u,true));
            assert(task_creations==(scenario=="stop-before-commit" ? 2:1));
        } else if (scenario=="muted" || scenario=="malformed" || scenario=="overlong" ||
                   scenario=="unsupported-duration" || scenario=="empty-ogg") {
            if (scenario=="muted") codec.volume=0;
            auto sound=scenario=="malformed" ? ogg()+"OggS" :
                       scenario=="overlong" ? ogg(200) :
                       scenario=="unsupported-duration" ? ogg(1,128) :
                       scenario=="empty-ogg" ? std::string("junk") : ogg();
            assert(audio.TryPlayLocalSound(sound,1)); receipts.Finished(1); idle(audio);
            assert(receipts.starts.empty() && !receipts.finishes[0].second);
            assert(output_attempts==0 && audio.audio_decode_queue_.empty());
            assert(codec.volume==(scenario=="muted" ? 0:30));
        } else if (scenario=="incoming") {
            run();
            for(auto [token,id] : {std::pair<uint64_t,uint32_t>{88,0},{0,0},{99,3}})
                assert(audio.PushPacketToDecodeQueue(packet(token,id)));
            {
                std::unique_lock<std::mutex> lock(audio.audio_queue_mutex_);
                assert(audio.audio_queue_cv_.cv.wait_for(lock,3s,[&] { return audio.IsPlaybackDrainedLocked(); }));
            }
            assert((receipts.decoded==std::vector<std::pair<uint64_t,uint64_t>>{{88,1088}}));
            for(int kind=0;kind<2;++kind) {
                decode_ok=kind!=0; decode_empty=kind==1;
                assert(audio.PushPacketToDecodeQueue(packet(100+kind)));
                std::unique_lock<std::mutex> lock(audio.audio_queue_mutex_);
                assert(audio.audio_queue_cv_.cv.wait_for(lock,3s,[&] { return audio.IsPlaybackDrainedLocked(); }));
            }
            assert((receipts.decoded==std::vector<std::pair<uint64_t,uint64_t>>{{88,1088}}));
        } else {
            if(scenario=="write-failure") codec.write_ok=false;
            if(scenario=="enable-failure") codec.enable_ok=false;
            if(scenario=="first-frame-cancel" || scenario=="mute-before-write" ||
               scenario=="producer-first") hold_output=true;
            if(scenario=="enable-cancel") hold_enable=true;
            if(scenario=="decode-reset") hold_decode=true;
            if(scenario=="drain-first") commit_hold_visit=2;
            if(scenario=="empty-pcm") decode_empty=true;
            if(scenario=="after-start-cancel") output_hold_visit=2;
            if(scenario=="after-start-failure") output_fail_visit=2;
            assert(audio.TryPlayLocalSound(ogg(),1));
            if(scenario=="drain-first") commit_gate.Until(1);
            if(scenario=="producer-first") pending(audio,2);
            run();
            if(scenario=="first-frame-cancel" || scenario=="mute-before-write" ||
               scenario=="producer-first") {
                output_gate.Until(1);
                if(scenario=="first-frame-cancel") {
                    audio.CancelLocalSound(); receipts.NoFinishedFor();
                    assert(audio.IsLocalSoundBusy() && !audio.TryPlayLocalSound(ogg(),2));
                }
                if(scenario=="mute-before-write") codec.volume=0;
                if(scenario!="first-frame-cancel") {
                    std::lock_guard<std::mutex> lock(receipts.mutex);
                    assert(receipts.finishes.empty());
                }
                output_gate.Release();
            }
            if(scenario=="enable-cancel") {
                enable_gate.Until(1); audio.CancelLocalSound(); enable_gate.Release();
            }
            if(scenario=="decode-reset") {
                decode_gate.Until(1);
                auto reset=std::async(std::launch::async,[&] { audio.ResetDecoder(); });
                // Cancel first to fence the checked write while ResetDecoder waits for decoder.
                audio.CancelLocalSound(); decode_gate.Release(); reset.get();
            }
            if(scenario=="drain-first") {
                std::unique_lock<std::mutex> lock(receipts.mutex);
                assert(receipts.cv.wait_for(lock,3s,[&] { return receipts.drains>0; }));
                assert(receipts.finishes.empty()); lock.unlock(); commit_gate.Release();
            }
            if(scenario=="after-start-cancel") {
                output_gate.Until(1); receipts.Started(1);
                audio.CancelLocalSound(); output_gate.Release();
            }
            receipts.Finished(1); idle(audio);
            const bool success=scenario=="success" || scenario=="producer-first" || scenario=="drain-first";
            assert((receipts.finishes==std::vector<std::pair<uint32_t,bool>>({{1,success}})));
            assert(receipts.starts.size()==(success || scenario=="after-start-cancel" ||
                                           scenario=="after-start-failure" || scenario=="first-frame-cancel" ? 1:0));
            assert(receipts.decoded.empty());
        }
        audio.service_stopped_=true; audio.CancelLocalSound(); audio.audio_queue_cv_.notify_all();
        if(decoder.joinable()) decoder.join(); if(output.joinable()) output.join();
    }
    join_local_tasks();
}
'''


class LocalSoundExecutionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        build = Path(cls.temp.name)
        platform = PLATFORM[:PLATFORM.index("    struct AudioCodec {")]
        platform = platform.replace("inline int esp_opus_dec_decode(void*, void*, void*, void*) { return -1; }", "")
        # The production packet layout is owned by Protocol, without network dependencies.
        packet_header = source("main/protocols/protocol.h")
        start = packet_header.index("struct AudioStreamPacket {")
        packet_header = packet_header[start:packet_header.index("\n};", start) + 3]
        platform += LOCAL_PLATFORM + packet_header + "\nstruct AudioDebugger {};\nstruct AudioEngine {};\n"
        header = without_includes(source("main/audio/audio_service.h")).replace("private:", "public:")
        header = header.replace("std::mutex", "TrackedServiceMutex")
        header = header.replace("kLocalSoundTimeoutMs = 12000", "kLocalSoundTimeoutMs = 120")
        header = header.replace("std::condition_variable audio_queue_cv_;", "TrackedConditionVariable audio_queue_cv_;")
        names = ["AudioService::AudioService()", "AudioService::~AudioService()",
                 "AudioService::TryPlayLocalSound(", "AudioService::CancelLocalSound(",
                 "AudioService::IsLocalSoundCurrentLocked(", "AudioService::CommitLocalSoundPacket(",
                 "AudioService::FinishLocalSoundPacketLocked(", "AudioService::RemoveLocalSoundPacketsLocked(",
                 "AudioService::NotifyLocalSoundStartedLocked(",
                 "AudioService::LocalSoundTask()", "AudioService::AudioOutputTask()",
                 "AudioService::OpusCodecTask()", "AudioService::IsPlaybackDrainedLocked()",
                 "AudioService::MarkPlaybackDrainedLocked()", "AudioService::ResetDecoder()",
                 "AudioService::Stop()",
                 "AudioService::Start()",
                 "AudioService::PushPacketToDecodeQueue(", "AudioService::SetCallbacks("]
        program = (platform + without_includes(source("main/audio/fixed_queue.h")) +
                   without_includes(source("main/audio/demuxer/ogg_demuxer.h")) +
                   without_includes(source("main/audio/demuxer/ogg_demuxer.cc")) + header +
                   "\n".join(method("main/audio/audio_service.cc", name).replace("std::mutex", "TrackedServiceMutex")
                             for name in names) +
                   "\nvoid AudioService::SetDecodeSampleRate(int,int) {}\n"
                   "void AudioService::AudioInputTask() {}\n"
                   "void AudioService::ResetCapture() {}\n" + CHECKS)
        cpp = build / "local_sound.cc"
        cls.binary = build / "local_sound"
        cpp.write_text(program, encoding="utf-8")
        compiled = subprocess.run(compiler_command() + ["-std=c++17", "-pthread", str(cpp),
                                  "-o", str(cls.binary)], capture_output=True, text=True, timeout=45)
        if compiled.returncode:
            raise AssertionError(compiled.stdout + compiled.stderr)

    def test_real_local_worker_decode_and_output_tasks(self):
        scenarios = ("admission", "cancel-before-commit", "reset-before-commit", "stop-before-commit",
                     "cancel-queue-wait", "cancel-lost-wakeup",
                     "muted", "malformed", "overlong", "unsupported-duration", "empty-ogg",
                     "success", "producer-first", "drain-first", "first-frame-cancel", "enable-cancel",
                     "mute-before-write", "write-failure", "enable-failure", "decode-reset", "empty-pcm",
                     "after-start-cancel", "after-start-failure", "incoming")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                result = subprocess.run([str(self.binary), scenario], capture_output=True,
                                        text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_request_owned_receipts_and_worker_retirement(self):
        for scenario in ("write-before-cancel", "write-before-reset", "write-before-stop",
                         "latch-before-cancel", "inflight-failure-cancel", "deadline-decode", "worker-restart"):
            with self.subTest(scenario=scenario):
                result = subprocess.run([str(self.binary), scenario], capture_output=True,
                                        text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class CheckedLocalOutputTest(unittest.TestCase):
    def test_real_box_checked_output_and_legacy_contract(self):
        checks = r'''
int main() {
    saved["output_volume"]=0;
    BoxAudioCodec codec; codec.Start();
    std::vector<int16_t> pcm{1,2};
#if CONFIG_WAVESHARE_1_85C_HOME
    assert(codec.SupportsCheckedOutput());
    assert(!codec.OutputDataChecked(pcm) && checked_writes==0);
    codec.EnableOutput(true);
    assert(!codec.OutputDataChecked(pcm) && checked_writes==0);
    assert(codec.SetOutputVolumeChecked(30).applied);
    assert(codec.OutputDataChecked(pcm) && checked_writes==1);
    // A failed new setting restored the old hardware value successfully.
    // Its transaction error must not suppress an otherwise usable speaker.
    commit_results={92, ESP_OK}; commit_before_failure=true;
    const auto failed_setting = codec.SetOutputVolumeChecked(40);
    assert(failed_setting.error==92 && failed_setting.volume==30 && failed_setting.applied);
    assert(codec.OutputDataChecked(pcm) && checked_writes==2);
    write_results={87};
    assert(!codec.OutputDataChecked(pcm) && checked_writes==3);
    codec.EnableOutput(false);
    assert(!codec.OutputDataChecked(pcm) && checked_writes==3);
    output_open_results={83}; codec.EnableOutput(true);
    assert(!codec.OutputDataChecked(pcm) && checked_writes==3);
    codec.EnableOutput(true);
    std::vector<int16_t> empty;
    assert(!codec.OutputDataChecked(empty) && checked_writes==3);
#else
    assert(!codec.SupportsCheckedOutput() && !codec.OutputDataChecked(pcm));
    codec.EnableOutput(true); codec.OutputData(pcm);
    assert(checked_writes==1);
#endif
}
'''
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory)
            cpp = build / "checked_output.cc"
            vendor_set = method("managed_components/espressif__esp_codec_dev/esp_codec_dev.c",
                                "int esp_codec_dev_set_out_vol(")
            cpp.write_text("\n".join((NVS_PLATFORM, AUDIO_PLATFORM, vendor_volume_source(), CODEC_PLATFORM,
                                       vendor_set, settings_source(), audio_source(), box_source(), checks)),
                           encoding="utf-8")
            for home in (0, 1):
                binary = build / f"checked_output_{home}"
                result = subprocess.run(compiler_command() + ["-std=c++17", "-pthread",
                                        f"-DCONFIG_WAVESHARE_1_85C_HOME={home}", str(cpp), "-o", str(binary)],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
