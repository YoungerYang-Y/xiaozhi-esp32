"""Exercise the production common device tools with hardware boundary adapters."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class HomeDeviceToolsTests(unittest.TestCase):
    def test_checked_home_results_and_legacy_contract(self):
        compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not compiler or not shutil.which(compiler[0]):
            self.skipTest("requires a host C++23 compiler")
        source = (ROOT / "main/mcp_tools.cc").read_text(encoding="utf-8")
        start = source.index("void McpServer::AddCommonTools()")
        end = source.index("#ifdef HAVE_LVGL", start)
        method = source[start:end] + "\n}\n"
        platform = r'''
#include <cassert>
#include <cstdlib>
#include <expected>
#include <functional>
#include <map>
#include <mutex>
#include <string>
#include <variant>
#include <vector>
#include <cJSON.h>
using esp_err_t = int;
constexpr int ESP_OK = 0, ESP_FAIL = -1;
#define TAG "HostTools"
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGI(...) ((void)0)
using ReturnValue = std::variant<bool, int, std::string, cJSON*>;
using ToolResult = std::expected<ReturnValue, std::string>;
constexpr int kPropertyTypeInteger = 0;
struct Property {
    std::string name;
    int minimum, maximum, input = 0;
    Property(std::string name, int, int minimum, int maximum)
        : name(std::move(name)), minimum(minimum), maximum(maximum) {}
    template<class T> T value() const { return static_cast<T>(input); }
};
struct PropertyList {
    std::vector<Property> schema;
    int input = 0;
    PropertyList() = default;
    PropertyList(std::initializer_list<Property> values) : schema(values) {}
    explicit PropertyList(int value) : input(value) {}
    Property operator[](const char* name) const {
        Property p{name, 0, 0, 100}; p.input = input; return p;
    }
};
struct OutputVolumeStatus { int volume; bool persisted; bool applied; esp_err_t error; };
struct BacklightStatus { int brightness; int applied_brightness; bool display_off; bool persisted; esp_err_t error; };
struct AudioCodec {
    int volume = 70, checked_calls = 0, legacy_calls = 0;
    bool fail = false;
    void SetOutputVolume(int v) { ++legacy_calls; volume = v; }
    OutputVolumeStatus SetOutputVolumeChecked(int v) {
        ++checked_calls;
        if (fail) return {volume, true, false, ESP_FAIL};
        volume = v;
        return {volume, true, false, ESP_OK};
    }
};
struct Backlight {
    int brightness = 75, checked_calls = 0, legacy_calls = 0;
    bool off = true, fail = false;
    void SetBrightness(uint8_t v, bool) { ++legacy_calls; brightness = v; }
    BacklightStatus SetBrightnessChecked(uint8_t v) {
        ++checked_calls;
        if (fail) return {brightness, 0, off, true, ESP_FAIL};
        brightness = v;
        return {brightness, off ? 0 : brightness, off, true, ESP_OK};
    }
};
struct Board {
    AudioCodec codec;
    Backlight backlight;
    bool speaker_present = true, screen_present = true;
    static Board& GetInstance() { static Board board; return board; }
    AudioCodec* GetAudioCodec() { return speaker_present ? &codec : nullptr; }
    Backlight* GetBacklight() { return screen_present ? &backlight : nullptr; }
    std::string GetDeviceStatusJson() { return "{}"; }
};
struct RegisteredTool {
    PropertyList properties;
    std::function<ToolResult(const PropertyList&)> callback;
};
struct McpServer {
    std::mutex tools_mutex_;
    std::map<std::string, RegisteredTool> tools_;
    template<class F> void AddTool(const char* name, const char*, PropertyList p, F callback) {
        tools_.insert_or_assign(name, RegisteredTool{p, [callback](const PropertyList& values) -> ToolResult {
            return callback(values);
        }});
    }
    void AddCommonTools();
};
'''
        checks = r'''
int main() {
    auto& board = Board::GetInstance();
    McpServer server;
    server.AddCommonTools();
    auto& volume = server.tools_.at("self.audio_speaker.set_volume");
    auto& brightness = server.tools_.at("self.screen.set_brightness");
    assert(volume.properties.schema[0].minimum == 0);
    assert(volume.properties.schema[0].maximum == 100);
    auto result = volume.callback(PropertyList(0));
    assert(result.has_value());
#if CONFIG_WAVESHARE_1_85C_HOME
    assert(brightness.properties.schema[0].minimum == 1);
    assert(board.codec.checked_calls == 1 && board.codec.legacy_calls == 0);
    assert(std::holds_alternative<cJSON*>(*result));
    auto json = std::get<cJSON*>(*result);
    assert(cJSON_GetObjectItem(json, "volume")->valueint == 0);
    assert(cJSON_IsTrue(cJSON_GetObjectItem(json, "persisted")));
    assert(cJSON_IsFalse(cJSON_GetObjectItem(json, "applied")));
    cJSON_Delete(json);
    board.codec.fail = true;
    assert(!volume.callback(PropertyList(30)).has_value());
    board.speaker_present = false;
    assert(!volume.callback(PropertyList(30)).has_value());
    result = brightness.callback(PropertyList(50));
    assert(result.has_value() && std::holds_alternative<cJSON*>(*result));
    json = std::get<cJSON*>(*result);
    assert(cJSON_GetObjectItem(json, "brightness")->valueint == 50);
    assert(cJSON_GetObjectItem(json, "applied_brightness")->valueint == 0);
    assert(cJSON_IsTrue(cJSON_GetObjectItem(json, "display_off")));
    assert(cJSON_IsTrue(cJSON_GetObjectItem(json, "persisted")));
    cJSON_Delete(json);
    assert(board.backlight.off && board.backlight.checked_calls == 1);
    board.backlight.fail = true;
    assert(!brightness.callback(PropertyList(20)).has_value());
#else
    assert(brightness.properties.schema[0].minimum == 0);
    assert(board.codec.checked_calls == 0 && board.codec.legacy_calls == 1);
    assert(std::get<bool>(*result));
    result = brightness.callback(PropertyList(0));
    assert(result.has_value() && std::get<bool>(*result));
    assert(board.backlight.checked_calls == 0 && board.backlight.legacy_calls == 1);
#endif
    board.screen_present = false;
    McpServer no_screen;
    no_screen.AddCommonTools();
    assert(no_screen.tools_.count("self.screen.set_brightness") == 0);
}
'''
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            cpp = directory / "home_tools.cc"
            cpp.write_text(platform + method + checks, encoding="utf-8")
            for policy, flags in (("home", ["-DCONFIG_WAVESHARE_1_85C_HOME=1"]),
                                  ("legacy", ["-DCONFIG_WAVESHARE_1_85C_HOME=0"])):
                with self.subTest(policy=policy):
                    binary = directory / policy
                    compiled = subprocess.run(
                        compiler + ["-std=c++23", "-I/usr/include/cjson", str(cpp),
                                    *flags, "-lcjson", "-o", str(binary)],
                        capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
                    executed = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
                    self.assertEqual(executed.returncode, 0, executed.stdout + executed.stderr)


if __name__ == "__main__":
    unittest.main()
