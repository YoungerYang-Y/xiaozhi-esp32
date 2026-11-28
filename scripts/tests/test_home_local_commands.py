"""Exercise real MultiNet dispatch at the SDK boundary, including cancellation fencing."""
import unittest
import test_audio_capture_pause as capture
from test_audio_capture_pause import PLATFORM, source, without_includes, method


class LocalCommandTests(unittest.TestCase):
    compile_and_run = capture.AudioCapturePauseTests.compile_and_run

    def test_two_stage_dispatch_epoch_single_result_and_cancel(self):
        names = ["CustomWakeWord::Start()", "CustomWakeWord::Stop()",
                 "CustomWakeWord::StartLocalCommands(", "CustomWakeWord::HasLocalCommands()",
                 "CustomWakeWord::IsLocalAction(", "CustomWakeWord::ResetCapture()",
                 "CustomWakeWord::FeedSamples(", "CustomWakeWord::FeedMono(",
                 "CustomWakeWord::Feed(", "CustomWakeWord::GetFeedSize()",
                 "CustomWakeWord::OnLocalCommand(", "CustomWakeWord::OnWakeWordDetected("]
        try:
            implementation = "\n".join(method("main/audio/wake_words/custom_wake_word.cc", name)
                                       for name in names)
        except ValueError:
            self.fail("MultiNet local-command phase is not implemented")
        program = PLATFORM.replace("#define CONFIG_SEND_WAKE_WORD_DATA 1", "#define CONFIG_SEND_WAKE_WORD_DATA 0") + r'''
            struct WakeWordAudioEncoder { void Reset() {} };
            struct model_iface_data_t {};
            enum esp_mn_state_t { ESP_MN_STATE_DETECTED, ESP_MN_STATE_TIMEOUT };
            struct esp_mn_results_t { int num = 1; int command_id[3] = {1,2,3}; };
            esp_mn_results_t result;
            bool block_detect = false;
            struct esp_mn_iface_t {
                int (*get_samp_chunksize)(model_iface_data_t*) = [](model_iface_data_t*) { return 2; };
                esp_mn_state_t (*detect)(model_iface_data_t*, int16_t*) = [](model_iface_data_t*, int16_t*) {
                    if (block_detect) fetch_gate.Wait();
                    return ESP_MN_STATE_DETECTED;
                };
                esp_mn_results_t* (*get_results)(model_iface_data_t*) = [](model_iface_data_t*) { return &result; };
                void (*clean)(model_iface_data_t*) = [](model_iface_data_t*) {};
            };
        ''' + without_includes(source("main/audio/wake_word.h")) + without_includes(
            source("main/audio/wake_words/custom_wake_word.h")).replace("private:", "public:") + implementation + r'''
            CustomWakeWord::CustomWakeWord() = default;
            CustomWakeWord::~CustomWakeWord() = default;
            bool CustomWakeWord::Initialize(AudioCodec*, srmodel_list_t*) { return true; }
            void CustomWakeWord::EncodeWakeWordData() {}
            bool CustomWakeWord::GetWakeWordOpus(std::vector<uint8_t>&) { return false; }
            std::vector<std::string> CustomWakeWord::GetWakeWords() const { return {}; }
            int main(int, char**) {
                CustomWakeWord wake;
                AudioCodec codec;
                model_iface_data_t data;
                esp_mn_iface_t iface;
                wake.codec_ = &codec; wake.multinet_ = &iface; wake.multinet_model_data_ = &data;
                assert(!wake.HasLocalCommands());
                wake.commands_ = {{"hello", "hello", "wake"}, {"screen", "screen", "screen_on"},
                                  {"cancel", "cancel", "cancel"}, {"unsafe", "unsafe", "reboot"}};
                assert(wake.HasLocalCommands());
                int wakes = 0, commands = 0;
                uint64_t epoch = 0;
                std::string action;
                wake.OnWakeWordDetected([&](const std::string&) { ++wakes; });
                wake.OnLocalCommand([&](const std::string& value, uint64_t generation) {
                    ++commands; action=value; epoch=generation;
                });
                int16_t pcm[] = {1,1};
                auto feed = [&](int id) { result.command_id[0]=id; wake.FeedMono(pcm, 2); };
                wake.Start(); feed(2); assert(commands == 0); feed(1); assert(wakes == 1);
                assert(!wake.StartLocalCommands(0));
                assert(wake.StartLocalCommands(42)); feed(1); assert(commands == 0);
                feed(0); feed(100); feed(4); assert(commands == 0);
                feed(2); feed(3); assert(commands == 1 && action == "screen_on" && epoch == 42);
                assert(wake.StartLocalCommands(43)); feed(3);
                assert(commands == 2 && action == "cancel" && epoch == 43);
                assert(wake.StartLocalCommands(44)); wake.Stop(); feed(2); assert(commands == 2);
                assert(wake.StartLocalCommands(45));
                block_detect = true;
                std::thread processing([&] { feed(2); });
                fetch_gate.Until(1);
                auto stopping = std::async(std::launch::async, [&] { wake.Stop(); });
                assert(stopping.wait_for(30ms) == std::future_status::timeout);
                fetch_gate.Release(); processing.join(); stopping.get();
                assert(commands == 2); // Stop invalidates before waiting for detect.
            }
        '''
        self.compile_and_run(program, "commands")


if __name__ == "__main__":
    unittest.main()
