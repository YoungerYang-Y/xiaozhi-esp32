"""Exercise production backlight/settings code at the NVS, LEDC and timer boundaries."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from test_checked_settings import NVS_PLATFORM, ROOT, compiler_command, settings_source, without_includes


BACKLIGHT_PLATFORM = r'''
#include <algorithm>
#include <atomic>
#include <functional>
#include <mutex>
#include <thread>
using gpio_num_t = int;
struct HostTimer {
    void (*callback)(void*);
    void* arg;
    std::atomic<bool> active{false};
};
using esp_timer_handle_t = HostTimer*;
constexpr int ESP_TIMER_TASK = 0;
struct esp_timer_create_args_t {
    void (*callback)(void*);
    void* arg;
    int dispatch_method;
    const char* name;
    bool skip_unhandled_events;
};
std::vector<HostTimer*> timers;
int esp_timer_create(const esp_timer_create_args_t* args, esp_timer_handle_t* timer) {
    *timer = new HostTimer{args->callback, args->arg};
    timers.push_back(*timer);
    return ESP_OK;
}
int esp_timer_start_periodic(esp_timer_handle_t timer, uint64_t) {
    timer->active = true;
    return ESP_OK;
}
int esp_timer_stop(esp_timer_handle_t timer) {
    if (!timer->active) return ESP_ERR_INVALID_STATE;
    timer->active = false;
    return ESP_OK;
}
int esp_timer_delete(esp_timer_handle_t timer) {
    timers.erase(std::find(timers.begin(), timers.end(), timer));
    delete timer;
    return ESP_OK;
}
void tick(int count = 1, bool queued = false) {
    while (count-- > 0) {
        for (auto* timer : timers) {
            if (timer->active || queued) timer->callback(timer->arg);
        }
    }
}
constexpr int LEDC_LOW_SPEED_MODE = 0, LEDC_TIMER_10_BIT = 10;
constexpr int LEDC_TIMER_0 = 0, LEDC_AUTO_CLK = 0;
constexpr int LEDC_CHANNEL_0 = 0, LEDC_INTR_DISABLE = 0;
struct ledc_timer_config_t {
    int speed_mode, duty_resolution, timer_num;
    uint32_t freq_hz;
    int clk_cfg;
    bool deconfigure;
};
struct ledc_channel_config_t {
    int gpio_num, speed_mode, channel, intr_type, timer_sel;
    uint32_t duty;
    int hpoint;
    struct { bool output_invert; } flags;
};
std::deque<int> duty_results, update_results;
uint32_t pending_duty = 0, applied_duty = 0;
int duty_calls = 0, update_calls = 0;
int ledc_timer_config(const ledc_timer_config_t*) { return ESP_OK; }
int ledc_channel_config(const ledc_channel_config_t*) { return ESP_OK; }
int ledc_stop(int, int, int) { return ESP_OK; }
int ledc_set_duty(int, int, uint32_t duty) {
    ++duty_calls;
    int err = result(duty_results);
    if (err == ESP_OK) pending_duty = duty;
    return err;
}
int ledc_update_duty(int, int) {
    ++update_calls;
    int err = result(update_results);
    if (err == ESP_OK) applied_duty = pending_duty;
    return err;
}
'''


def backlight_source():
    return "\n".join(without_includes((ROOT / path).read_text(encoding="utf-8"))
                     for path in ("main/boards/common/backlight.h", "main/boards/common/backlight.cc"))


class BacklightSettingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        build = Path(cls.temp.name)
        checks = r'''
class HostBacklight : public Backlight {
public:
    int applied = -1;
    void SetBrightnessImpl(uint8_t value) override {
        assert(brightness() == value);
        applied = target_brightness_;
        brightness_ = target_brightness_;
    }
};
class HostPwmBacklight : public PwmBacklight {
public:
    HostPwmBacklight() : PwmBacklight(1) {}
    int calls = 0;
    void SetBrightnessImpl(uint8_t value) override {
        ++calls;
        PwmBacklight::SetBrightnessImpl(value);
    }
};
void check(const BacklightStatus& status, int configured, int applied,
           bool off, bool persisted, int error = ESP_OK) {
    assert(status.brightness == configured);
    assert(status.applied_brightness == applied);
    assert(status.display_off == off);
    assert(status.persisted == persisted);
    assert(status.error == error);
    if (applied >= 0) assert(applied_duty == (1023u * applied) / 100);
}
int main(int argc, char** argv) {
    assert(argc == 2);
    std::string scenario = argv[1];
    if (scenario == "restore-clamp") {
        saved["brightness"] = 300;
        PwmBacklight backlight(1);
        backlight.RestoreBrightness();
        tick(400);
        assert(backlight.brightness() == 100);
        assert(applied_duty == 1023);
        check(backlight.GetStatus(), 100, 100, false, false);
    } else if (scenario == "manual-off") {
        PwmBacklight backlight(1);
        check(backlight.SetBrightnessChecked(37), 37, 37, false, true);
        assert(backlight.SetDisplayOff(true) == ESP_OK);
        check(backlight.GetStatus(), 37, 0, true, true);
        assert(backlight.display_off() && backlight.brightness() == 0);
        const auto writes_before = brightness_writes.size();
        check(backlight.SetBrightnessChecked(81), 81, 0, true, true);
        assert(saved.at("brightness") == 81);
        assert(backlight.brightness() == 0);
        tick(100, true);
        check(backlight.GetStatus(), 81, 0, true, true);
        assert(backlight.SetDisplayOff(false) == ESP_OK);
        check(backlight.GetStatus(), 81, 81, false, true);
        assert(brightness_writes.size() == writes_before + 1);
        for (auto value : brightness_writes) assert(value != 0);
    } else if (scenario == "restart") {
        {
            PwmBacklight backlight(1);
            check(backlight.SetBrightnessChecked(62), 62, 62, false, true);
            assert(backlight.SetDisplayOff(true) == ESP_OK);
        }
        PwmBacklight restarted(1);
        restarted.RestoreBrightness();
        tick(110);
        check(restarted.GetStatus(), 62, 62, false, true);
    } else if (scenario == "restore-defaults") {
        for (int value : {0, -19, 2147483647}) {
            saved["brightness"] = value;
            PwmBacklight backlight(1);
            backlight.RestoreBrightness();
            tick(110);
            const int expected = value <= 0 ? 10 : 100;
            check(backlight.GetStatus(), expected, expected, false, false);
            assert(saved.at("brightness") == value);
        }
        saved.clear();
        PwmBacklight backlight(1);
        backlight.RestoreBrightness();
        tick(110);
        check(backlight.GetStatus(), 75, 75, false, false);
    } else if (scenario == "range") {
        PwmBacklight backlight(1);
        check(backlight.SetBrightnessChecked(37), 37, 37, false, true);
        int previous_calls = duty_calls, previous_sets = sets;
        for (int value : {0, 101, 255}) {
            check(backlight.SetBrightnessChecked(value), 37, 37, false, true, ESP_ERR_INVALID_ARG);
            assert(duty_calls == previous_calls && sets == previous_sets);
        }
        check(backlight.SetBrightnessChecked(1), 1, 1, false, true);
        check(backlight.SetBrightnessChecked(100), 100, 100, false, true);
    } else if (scenario == "set-failure" || scenario == "commit-failure" ||
               scenario == "ambiguous-commit" || scenario == "storage-rollback-failure" ||
               scenario == "rollback-read-failure") {
        PwmBacklight backlight(1);
        check(backlight.SetBrightnessChecked(37), 37, 37, false, true);
        const int previous_commits = commits;
        int expected_error = 73;
        bool expected_persisted = true;
        if (scenario == "set-failure" || scenario == "rollback-read-failure") {
            set_results = {73};
            if (scenario == "rollback-read-failure") {
                get_results = {ESP_OK, 79};
                expected_persisted = false;
            }
        } else {
            expected_error = 74;
            commit_results = {74};
            if (scenario == "ambiguous-commit") commit_apply_results = {1};
            if (scenario == "storage-rollback-failure") {
                commit_results = {74, 75};
                commit_apply_results = {1, 0};
                expected_persisted = false;
            }
        }
        check(backlight.SetBrightnessChecked(60), 37, 37, false,
              expected_persisted, expected_error);
        assert(saved.at("brightness") == (scenario == "storage-rollback-failure" ? 60 : 37));
        assert(commits == previous_commits + (expected_error == 73 ? 1 : 2));
        tick(100, true);
        assert(backlight.brightness() == 37);
    } else if (scenario == "hardware-set-failure" || scenario == "hardware-update-failure") {
        PwmBacklight backlight(1);
        check(backlight.SetBrightnessChecked(37), 37, 37, false, true);
        const int previous_sets = sets, previous_updates = update_calls;
        if (scenario == "hardware-set-failure") duty_results = {76};
        else update_results = {77};
        const int expected_error = scenario == "hardware-set-failure" ? 76 : 77;
        check(backlight.SetBrightnessChecked(60), 37, 37, false, true, expected_error);
        assert(sets == previous_sets && saved.at("brightness") == 37);
        assert(update_calls == previous_updates + (expected_error == 76 ? 1 : 2));
    } else if (scenario == "hardware-rollback-failure") {
        PwmBacklight backlight(1);
        check(backlight.SetBrightnessChecked(37), 37, 37, false, true);
        commit_results = {74};
        duty_results = {ESP_OK, 78};
        check(backlight.SetBrightnessChecked(60), 37, -1, false, true, 74);
        assert(applied_duty == (1023u * 60) / 100);
        check(backlight.SetBrightnessChecked(80), 80, 80, false, true);
    } else if (scenario == "open-read-failure") {
        PwmBacklight backlight(1);
        check(backlight.SetBrightnessChecked(37), 37, 37, false, true);
        const int previous_calls = duty_calls, previous_sets = sets;
        open_results = {71};
        check(backlight.SetBrightnessChecked(60), 37, 37, false, false, 71);
        get_results = {72};
        check(backlight.SetBrightnessChecked(60), 37, 37, false, false, 72);
        assert(duty_calls == previous_calls && sets == previous_sets);
    } else if (scenario == "manual-failure") {
        PwmBacklight backlight(1);
        check(backlight.SetBrightnessChecked(37), 37, 37, false, true);
        duty_results = {76};
        assert(backlight.SetDisplayOff(true) == 76);
        check(backlight.GetStatus(), 37, 37, false, true, 76);
        assert(backlight.SetDisplayOff(true) == ESP_OK);
        update_results = {77};
        assert(backlight.SetDisplayOff(false) == 77);
        check(backlight.GetStatus(), 37, 0, true, true, 77);
        check(backlight.SetBrightnessChecked(60), 60, 0, true, true);
        assert(backlight.SetDisplayOff(false) == ESP_OK);
        check(backlight.GetStatus(), 60, 60, false, true);
    } else if (scenario == "fade-interleave") {
        PwmBacklight backlight(1);
        check(backlight.SetBrightnessChecked(37), 37, 37, false, true);
        backlight.SetBrightness(80);
        tick(3);
        assert(backlight.brightness() == 40);
        assert(backlight.SetDisplayOff(true) == ESP_OK);
        tick(100, true);
        assert(backlight.brightness() == 0 && applied_duty == 0);
        backlight.SetBrightness(91);
        tick(100, true);
        assert(backlight.brightness() == 0 && backlight.display_off());
        check(backlight.SetBrightnessChecked(62), 62, 0, true, true);
        assert(backlight.SetDisplayOff(false) == ESP_OK);
        tick(100, true);
        check(backlight.GetStatus(), 62, 62, false, true);
    } else if (scenario == "last-success") {
        PwmBacklight backlight(1);
        for (int value : {20, 80, 1, 100, 43}) {
            check(backlight.SetBrightnessChecked(value), value, value, false, true);
        }
        commit_results = {74};
        check(backlight.SetBrightnessChecked(88), 43, 43, false, true, 74);
        check(backlight.SetBrightnessChecked(67), 67, 67, false, true);
        tick(120, true);
        check(backlight.GetStatus(), 67, 67, false, true);
        assert(saved.at("brightness") == 67);
    } else if (scenario == "concurrent-callbacks") {
        PwmBacklight backlight(1);
        check(backlight.SetBrightnessChecked(37), 37, 37, false, true);
        std::atomic<bool> start{false};
        std::vector<std::thread> callers;
        for (int caller = 0; caller < 3; ++caller) {
            callers.emplace_back([&, caller] {
                while (!start) std::this_thread::yield();
                for (int i = 0; i < 100; ++i) {
                    const int value = (i + caller * 33) % 100 + 1;
                    const auto status = backlight.SetBrightnessChecked(value);
                    assert(status.brightness == value && status.error == ESP_OK);
                }
            });
        }
        callers.emplace_back([&] {
            while (!start) std::this_thread::yield();
            for (int i = 0; i < 500; ++i) tick(1, true);
        });
        start = true;
        for (auto& caller : callers) caller.join();
        check(backlight.SetBrightnessChecked(67), 67, 67, false, true);
        tick(100, true);
        check(backlight.GetStatus(), 67, 67, false, true);
        assert(saved.at("brightness") == 67);
    } else if (scenario == "off-save-failure") {
        PwmBacklight backlight(1);
        check(backlight.SetBrightnessChecked(37), 37, 37, false, true);
        assert(backlight.SetDisplayOff(true) == ESP_OK);
        commit_results = {74};
        check(backlight.SetBrightnessChecked(60), 37, 0, true, true, 74);
        assert(saved.at("brightness") == 37);
        tick(100, true);
        assert(applied_duty == 0 && backlight.display_off());
        assert(backlight.SetDisplayOff(false) == ESP_OK);
        check(backlight.GetStatus(), 37, 37, false, true);
    } else if (scenario == "legacy-and-derived") {
        PwmBacklight backlight(1);
        backlight.SetBrightness(255, true);
        assert(backlight.brightness() == 0);
        tick(110);
        assert(backlight.brightness() == 100 && saved.at("brightness") == 100);
        backlight.SetBrightness(0, true);
        tick(110);
        assert(backlight.brightness() == 0 && saved.at("brightness") == 0);
        {
            HostBacklight custom;
            auto status = custom.SetBrightnessChecked(47);
            assert(status.error == ESP_OK && status.applied_brightness == 47);
            assert(custom.applied == 47);
            assert(custom.SetDisplayOff(true) == ESP_OK && custom.applied == 0);
            assert(custom.SetDisplayOff(false) == ESP_OK && custom.applied == 47);
        }
        {
            HostPwmBacklight custom;
            check(custom.SetBrightnessChecked(55), 55, 55, false, true);
            assert(custom.calls == 1);
            duty_results = {76};
            check(custom.SetBrightnessChecked(60), 55, 55, false, true, 76);
            assert(custom.calls == 3);
        }
    } else {
        assert(false);
    }
}
'''
        source = build / "backlight.cc"
        platform = NVS_PLATFORM.replace('== "audio"', '== "display"')
        platform = platform.replace('#include <vector>', '#include <vector>\n'
                                    'std::vector<int32_t> brightness_writes;\n'
                                    'std::deque<int> commit_apply_results;')
        platform = platform.replace('++sets;', '++sets; brightness_writes.push_back(value);')
        commit_injection = 'int err = result(commit_results);'
        assert commit_injection in platform and 'bool commit_before_failure =' in platform
        platform = platform.replace(commit_injection, commit_injection + '\n'
                                    '    commit_before_failure = result(commit_apply_results) != 0;')
        source.write_text(platform + BACKLIGHT_PLATFORM + settings_source() + backlight_source() + checks,
                          encoding="utf-8")
        cls.binary = build / "backlight"
        compiled = subprocess.run(compiler_command() + ["-std=c++20", "-pthread", str(source),
                                  "-o", str(cls.binary)],
                                  capture_output=True, text=True, timeout=30)
        if compiled.returncode:
            raise AssertionError(compiled.stdout + compiled.stderr)

    def test_production_backlight_settings(self):
        for scenario in ("restore-clamp", "manual-off", "restart", "restore-defaults", "range",
                         "set-failure", "commit-failure", "ambiguous-commit",
                         "storage-rollback-failure", "rollback-read-failure",
                         "hardware-set-failure", "hardware-update-failure",
                         "hardware-rollback-failure", "open-read-failure", "manual-failure",
                         "fade-interleave", "last-success", "concurrent-callbacks",
                         "off-save-failure", "legacy-and-derived"):
            with self.subTest(scenario=scenario):
                result = subprocess.run([str(self.binary), scenario], capture_output=True,
                                        text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
