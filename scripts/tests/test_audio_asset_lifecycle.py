"""Run AudioService's real update/control paths with the hardware engine substituted."""
import unittest
import test_audio_capture_pause as capture
from test_audio_capture_pause import method


class AudioAssetLifecycleTests(unittest.TestCase):
    compile_and_run = capture.AudioCapturePauseTests.compile_and_run

    def test_asset_fence_preserves_pause_and_resumes_without_capture(self):
        self.compile_and_run(self.program(), "assets")

    def test_failed_asset_fence_can_resume_existing_engine(self):
        self.compile_and_run(self.program(), "assets-fail")

    def program(self):
        program = capture.AudioCapturePauseTests.service_program(self)
        program = program.replace("bool wake=false, voice=false;", "bool wake=false, voice=false, local=false;")
        program = program.replace("void ResetCapture() override { wake=voice=false; }", """
            void ResetCapture() override { wake=voice=local=false; }
            bool HasLocalCommands() const override { return true; }
            bool StartLocalCommands(uint64_t generation) override { local=generation!=0; return local; }
            void StopLocalCommands() override { local=false; }
        """)
        names = ["AudioService::CreateAudioEngine()", "AudioService::PrepareForAssetUpdate()",
                 "AudioService::ResumeAfterAssetUpdate()", "AudioService::HasLocalCommands()",
                 "AudioService::StartLocalCommands(", "AudioService::StopLocalCommands()",
                 "AudioService::ResetDecoder()", "AudioService::SetModelsList("]
        program = program.replace("void AudioService::ResetDecoder() {}", "")
        program = program.replace("int main(int argc, char** argv)",
                                  "using AfeAudioEngine = FakeEngine;\n" +
                                  "\n".join(method("main/audio/audio_service.cc", name) for name in names) +
                                  "\nint main(int argc, char** argv)")
        program = program.replace('if (scenario == "gate") {', r'''
            if (scenario == "assets" || scenario == "assets-fail") {
                audio.SetInputPaused(true);
                auto* previous = audio.audio_engine_.get();
                // LocalSoundTask remains resident after an owned prompt ends;
                // its idle task must not prevent replacing unrelated assets.
                audio.local_sound_task_handle_ = reinterpret_cast<TaskHandle_t>(1);
                if (scenario == "assets-fail") audio.output_in_flight_ = true;
                bool ready = audio.PrepareForAssetUpdate();
                assert(ready == (scenario == "assets"));
                assert(audio.input_paused());
                assert(!audio.HasLocalCommands() && !audio.StartLocalCommands(9));
                audio.EnableWakeWordDetection(true);
                audio.EnableVoiceProcessing(true);
                assert(!(xEventGroupGetBits(audio.event_group_) & 15));
                if (scenario == "assets-fail") {
                    assert(audio.audio_engine_.get() == previous);
                    audio.output_in_flight_ = false;
                } else {
                    assert(!audio.audio_engine_ && !audio.audio_engine_initialized_);
                    srmodel_list_t models;
                    audio.SetModelsList(&models);
                }
                audio.ResumeAfterAssetUpdate();
                assert(audio.audio_engine_ && audio.audio_engine_initialized_);
                assert(audio.HasLocalCommands() && audio.input_paused());
                assert(!(xEventGroupGetBits(audio.event_group_) & 15));
                assert(!audio.StartLocalCommands(10));
                audio.SetInputPaused(false);
                assert(!(xEventGroupGetBits(audio.event_group_) & 15));
                assert(audio.StartLocalCommands(11));
                assert((xEventGroupGetBits(audio.event_group_) & 15) == AS_EVENT_LOCAL_COMMAND_RUNNING);
                audio.StopLocalCommands();
                assert(!(xEventGroupGetBits(audio.event_group_) & AS_EVENT_LOCAL_COMMAND_RUNNING));
                audio.SetInputPaused(true);
                assert(!(xEventGroupGetBits(audio.event_group_) & 15));
                audio.local_sound_task_handle_ = nullptr;
            } else if (scenario == "gate") {
        ''')
        return program


if __name__ == "__main__":
    unittest.main()
