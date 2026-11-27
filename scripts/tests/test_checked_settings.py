"""Run the production Settings implementation against a fault-injecting NVS boundary."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


NVS_PLATFORM = r'''
#include <cassert>
#include <cstdint>
#include <deque>
#include <map>
#include <string>
#include <vector>
using esp_err_t = int;
using nvs_handle_t = unsigned;
constexpr int ESP_OK = 0, ESP_FAIL = -1, ESP_ERR_INVALID_ARG = 0x102;
constexpr int ESP_ERR_INVALID_STATE = 0x103, ESP_ERR_NOT_SUPPORTED = 0x106;
constexpr int ESP_ERR_NVS_NOT_FOUND = 0x1102;
constexpr int NVS_READONLY = 0, NVS_READWRITE = 1;
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define ESP_ERROR_CHECK(e) assert((e) == ESP_OK)
#define ESP_ERROR_CHECK_WITHOUT_ABORT(e) ((void)(e))
struct NvsHandle { std::map<std::string, int32_t> pending; };
std::map<nvs_handle_t, NvsHandle> nvs_handles;
std::map<std::string, int32_t> saved;
std::deque<int> open_results, set_results, commit_results, get_results;
unsigned next_handle = 1;
int commits = 0, sets = 0;
bool commit_before_failure = false;
int result(std::deque<int>& results) {
    if (results.empty()) return ESP_OK;
    int value = results.front(); results.pop_front(); return value;
}
int nvs_open(const char* ns, int, nvs_handle_t* handle) {
    assert(std::string(ns) == "audio");
    int err = result(open_results);
    if (err != ESP_OK) return err;
    *handle = next_handle++;
    nvs_handles.emplace(*handle, NvsHandle{});
    return ESP_OK;
}
void nvs_close(nvs_handle_t handle) { assert(nvs_handles.erase(handle) == 1); }
int nvs_set_i32(nvs_handle_t handle, const char* key, int32_t value) {
    ++sets;
    int err = result(set_results);
    if (err != ESP_OK) return err;
    nvs_handles.at(handle).pending[key] = value;
    return ESP_OK;
}
int nvs_get_i32(nvs_handle_t handle, const char* key, int32_t* value) {
    assert(nvs_handles.count(handle));
    int err = result(get_results);
    if (err != ESP_OK) return err;
    if (!saved.count(key)) return ESP_ERR_NVS_NOT_FOUND;
    *value = saved.at(key);
    return ESP_OK;
}
int nvs_commit(nvs_handle_t handle) {
    ++commits;
    int err = result(commit_results);
    if (err == ESP_OK || commit_before_failure) {
        for (const auto& [key, value] : nvs_handles.at(handle).pending) saved[key] = value;
    }
    return err;
}
int nvs_get_str(nvs_handle_t, const char*, char*, size_t*) { return ESP_ERR_NVS_NOT_FOUND; }
int nvs_set_str(nvs_handle_t, const char*, const char*) { return ESP_OK; }
int nvs_get_u8(nvs_handle_t, const char*, uint8_t*) { return ESP_ERR_NVS_NOT_FOUND; }
int nvs_set_u8(nvs_handle_t, const char*, uint8_t) { return ESP_OK; }
int nvs_erase_key(nvs_handle_t, const char*) { return ESP_OK; }
int nvs_erase_all(nvs_handle_t) { return ESP_OK; }
'''


def without_includes(text):
    return "\n".join(line for line in text.splitlines() if not line.startswith("#include")) + "\n"


def settings_source():
    return "\n".join(without_includes((ROOT / path).read_text(encoding="utf-8"))
                     for path in ("main/settings.h", "main/settings.cc"))


def compiler_command():
    compiler = shlex.split(os.environ.get("CXX", "c++"))
    if not compiler or not shutil.which(compiler[0]):
        raise unittest.SkipTest("A host C++ compiler is required")
    return compiler


class CheckedSettingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        build = Path(cls.temp.name)
        checks = r'''
int main(int argc, char** argv) {
    assert(argc == 2);
    std::string scenario = argv[1];
    saved["output_volume"] = 37;
    if (scenario == "success") {
        {
            Settings settings("audio", true);
            assert(settings.TrySetIntAndCommit("output_volume", 0) == ESP_OK);
            assert(saved.at("output_volume") == 0);
            int32_t value = -1;
            assert(settings.TryGetInt("output_volume", value) == ESP_OK && value == 0);
        }
        assert(commits == 1);
    } else if (scenario == "open-failure") {
        open_results = {71};
        Settings settings("audio", true);
        assert(settings.TrySetIntAndCommit("output_volume", 9) == 71);
        int32_t value = 81;
        assert(settings.TryGetInt("output_volume", value) == 71 && value == 81);
        assert(sets == 0 && commits == 0);
    } else if (scenario == "write-open-failure") {
        open_results = {ESP_OK, 72};
        Settings settings("audio", true);
        assert(settings.TrySetIntAndCommit("output_volume", 9) == 72);
        assert(sets == 0 && commits == 0);
    } else if (scenario == "set-failure") {
        set_results = {73};
        {
            Settings settings("audio", true);
            assert(settings.TrySetIntAndCommit("output_volume", 9) == 73);
        }
        assert(commits == 0 && saved.at("output_volume") == 37);
    } else if (scenario == "commit-failure") {
        commit_results = {74};
        {
            Settings settings("audio", true);
            assert(settings.TrySetIntAndCommit("output_volume", 9) == 74);
        }
        assert(commits == 1 && saved.at("output_volume") == 37);
    } else if (scenario == "failed-write-does-not-leak") {
        commit_results = {74, ESP_OK};
        {
            Settings settings("audio", true);
            assert(settings.TrySetIntAndCommit("output_volume", 9) == 74);
            assert(settings.TrySetIntAndCommit("different_key", 4) == ESP_OK);
        }
        assert(commits == 2 && saved.at("output_volume") == 37);
        assert(saved.at("different_key") == 4);
    } else if (scenario == "read-failure") {
        Settings settings("audio");
        get_results = {75};
        int32_t value = 81;
        assert(settings.TryGetInt("output_volume", value) == 75 && value == 81);
    } else if (scenario == "readonly") {
        Settings settings("audio");
        assert(settings.TrySetIntAndCommit("output_volume", 9) == ESP_ERR_INVALID_STATE);
        assert(sets == 0 && commits == 0);
    } else if (scenario == "legacy") {
        {
            Settings settings("audio", true);
            settings.SetInt("output_volume", 9);
            assert(commits == 0);
        }
        assert(commits == 1 && saved.at("output_volume") == 9);
    } else {
        assert(false);
    }
}
'''
        source = build / "checked_settings.cc"
        source.write_text(NVS_PLATFORM + settings_source() + checks, encoding="utf-8")
        cls.binary = build / "checked_settings"
        compiled = subprocess.run(compiler_command() + ["-std=c++17", str(source), "-o", str(cls.binary)],
                                  capture_output=True, text=True, timeout=30)
        if compiled.returncode:
            raise AssertionError(compiled.stdout + compiled.stderr)

    def test_production_checked_settings(self):
        for scenario in ("success", "open-failure", "write-open-failure", "set-failure",
                         "commit-failure", "failed-write-does-not-leak", "read-failure",
                         "readonly", "legacy"):
            with self.subTest(scenario=scenario):
                result = subprocess.run([str(self.binary), scenario], capture_output=True,
                                        text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
