"""Exercise the production Wi-Fi status JSON with board capability adapters."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

from test_startup_wake_word_hint import definition

ROOT = Path(__file__).resolve().parents[2]


class HomeStatusJsonTests(unittest.TestCase):
    def test_coarse_battery_and_link_state_without_charging_or_percentage(self):
        compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not compiler or not shutil.which(compiler[0]):
            self.skipTest("requires a host C++ compiler")
        method = definition((ROOT / "main/boards/common/wifi_board.cc").read_text(),
                            "std::string WifiBoard::GetDeviceStatusJson()")
        platform = r'''
#include <cassert>
#include <cstring>
#include <string>
#include <cJSON.h>
#include "main/boards/common/battery_status.h"
constexpr int ESP_OK = 0;
struct Application {
    static Application& GetInstance() { static Application app; return app; }
    const char* GetServiceStateName() const { return "available"; }
};
struct AudioCodec {
    struct Status { int volume = 30; bool persisted = true, applied = false; int error = 0; };
    Status GetOutputVolumeStatus() { return {}; }
    int output_volume() { return 30; }
};
struct Backlight {
    struct Status { int brightness = 50; bool display_off = true, persisted = true;
                    int applied_brightness = 0, error = 0; };
    Status GetStatus() { return {}; }
    int brightness() { return 50; }
};
struct Theme { std::string name() { return "light"; } };
struct Display { Theme theme; int height() { return 360; } Theme* GetTheme() { return &theme; } };
struct Board {
    BatteryStatus battery{true, BatteryState::Unknown, BatteryBand::Unknown};
    int legacy_reads = 0;
    AudioCodec codec; Backlight backlight; Display display;
    static Board& GetInstance() { static Board board; return board; }
    AudioCodec* GetAudioCodec() { return &codec; }
    Backlight* GetBacklight() { return &backlight; }
    Display* GetDisplay() { return &display; }
    BatteryStatus GetBatteryStatus() const { return battery; }
    bool GetBatteryLevel(int& level, bool& charging, bool& discharging) {
        ++legacy_reads; level = 100; charging = true; discharging = false; return true;
    }
    bool GetTemperature(float&) { return false; }
};
struct WifiManager {
    bool connected = false;
    static WifiManager& GetInstance() { static WifiManager wifi; return wifi; }
    bool IsConnected() const { return connected; }
    std::string GetSsid() { return "test-network"; }
    int GetRssi() { return -45; }
};
struct WifiBoard {
    void AddSystemStatus(cJSON*) {}
    std::string GetDeviceStatusJson();
};
'''
        checks = r'''
int main() {
    WifiBoard board;
    auto& hardware = Board::GetInstance();
    auto json = cJSON_Parse(board.GetDeviceStatusJson().c_str());
    assert(json);
    auto battery = cJSON_GetObjectItem(json, "battery");
#if CONFIG_WAVESHARE_1_85C_HOME
    assert(hardware.legacy_reads == 0);
    assert(strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(battery, "state")), "unknown") == 0);
    assert(!cJSON_HasObjectItem(battery, "band"));
    assert(!cJSON_HasObjectItem(battery, "charging"));
    assert(!cJSON_HasObjectItem(battery, "level"));
    auto network = cJSON_GetObjectItem(json, "network");
    assert(cJSON_IsFalse(cJSON_GetObjectItem(network, "connected")));
    assert(strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(network, "signal")), "unknown") == 0);
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
    assert(strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(network, "service_state")), "unknown") == 0);
#else
    assert(!cJSON_HasObjectItem(network, "service_state"));
#endif
    cJSON_Delete(json);
    WifiManager::GetInstance().connected = true;
    hardware.battery = {true, BatteryState::Unclassified, BatteryBand::TwoBars};
    json = cJSON_Parse(board.GetDeviceStatusJson().c_str());
    battery = cJSON_GetObjectItem(json, "battery");
    assert(strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(battery, "state")), "unclassified") == 0);
    assert(strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(battery, "band")), "two_bars") == 0);
    assert(cJSON_IsTrue(cJSON_GetObjectItem(cJSON_GetObjectItem(json, "network"), "connected")));
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
    assert(strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(cJSON_GetObjectItem(json, "network"),
                                                        "service_state")), "available") == 0);
#endif
    assert(hardware.legacy_reads == 0);
#else
    assert(hardware.legacy_reads == 1);
    assert(cJSON_GetNumberValue(cJSON_GetObjectItem(battery, "level")) == 100);
    assert(cJSON_IsTrue(cJSON_GetObjectItem(battery, "charging")));
#endif
    cJSON_Delete(json);
}
'''
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "status.cc"
            source.write_text(platform + method + checks, encoding="utf-8")
            for home, alerts in ((1, 0), (1, 1), (0, 0)):
                with self.subTest(home=home, alerts=alerts):
                    binary = Path(directory) / f"status-{home}-{alerts}"
                    compiled = subprocess.run(
                        compiler + ["-std=c++23", "-I", str(ROOT), "-I/usr/include/cjson",
                                    f"-DCONFIG_WAVESHARE_1_85C_HOME={home}", str(source),
                                    f"-DCONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS={alerts}",
                                    "-lcjson", "-o", str(binary)],
                        capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
                    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
