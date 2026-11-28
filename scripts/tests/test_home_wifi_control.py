"""Run production Wi-Fi control methods with synchronous manager event adapters."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_startup_wake_word_hint import definition

ROOT = Path(__file__).resolve().parents[2]


class HomeWifiControlTests(unittest.TestCase):
    def test_disabled_boot_config_exit_and_timeout_do_not_reconnect(self):
        if not shutil.which("c++"):
            self.skipTest("requires a host C++ compiler")
        source = (ROOT / "main/boards/common/wifi_board.cc").read_text()
        signatures = ["bool WifiBoard::InitializeWifi()", "void WifiBoard::StartNetwork()",
                      "bool WifiBoard::SetNetworkEnabled(bool enabled)",
                      "bool WifiBoard::RequestWifiConfiguration()",
                      "bool WifiBoard::IsNetworkConnected() const",
                      "bool WifiBoard::IsWifiConfigurationActive() const",
                      "bool WifiBoard::HasNetworkConfiguration() const",
                      "void WifiBoard::TryWifiConnect()",
                      "void WifiBoard::OnNetworkEvent(NetworkEvent event, const std::string& data)",
                      "void WifiBoard::OnWifiConnectTimeout(void* arg)",
                      "void WifiBoard::StartWifiConfigMode()"]
        methods = "\n".join(definition(source, signature) for signature in signatures)
        platform = r'''
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <functional>
#include <string>
#include <vector>
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
constexpr int ESP_OK=0, ESP_MAC_WIFI_STA=0, CONNECT_TIMEOUT_SEC=60;
const char* TAG="test";
int esp_read_mac(uint8_t*,int) { return -1; }
inline int delays=0, timer_starts=0;
void esp_timer_stop(void*) {}
void esp_timer_start_once(void*,uint64_t) { ++timer_starts; }
#define pdMS_TO_TICKS(x) (x)
void vTaskDelay(int) { ++delays; }
namespace Lang {
constexpr const char* CODE="zh-CN";
namespace Strings {
constexpr const char *CONNECT_TO_HOTSPOT="connect", *ACCESS_VIA_BROWSER="browser", *WIFI_CONFIG_MODE="config";
}
namespace Sounds { constexpr const char* OGG_WIFICONFIG="config"; }
}
constexpr int kDeviceStateWifiConfiguring=1;
inline int app_mutations=0;
struct Application {
    static Application& GetInstance() { static Application a; return a; }
    void SetDeviceState(int) { ++app_mutations; }
    template<class F> void Schedule(F f) { ++app_mutations; f(); }
    void Alert(const char*,const char*,const char*,const char*) { ++app_mutations; }
};
enum class NetworkEvent { Scanning,Connecting,Connected,Disconnected,WifiConfigModeEnter,WifiConfigModeExit };
enum class WifiEvent { Scanning,Connecting,Connected,Disconnected,ConfigModeEnter,ConfigModeExit };
using NetworkEventCallback=std::function<void(NetworkEvent,const std::string&)>;
struct WifiManagerConfig { std::string ssid_prefix, language, station_hostname; int advanced_groups=0; };
int MakeXiaozhiPortalSettings() { return 0; }
struct SsidManager {
    bool saved=true;
    static SsidManager& GetInstance() { static SsidManager s; return s; }
    std::vector<int> GetSsidList() { return saved?std::vector<int>{1}:std::vector<int>{}; }
};
struct WifiManager {
    bool initialized=false, config=false, connected=false;
    int stations=0, aps=0, stops=0;
    std::function<void(WifiEvent,const std::string&)> callback;
    static WifiManager& GetInstance() { static WifiManager w; return w; }
    bool Initialize(const WifiManagerConfig&) { initialized=true; return true; }
    void SetEventCallback(decltype(callback) f) { callback=std::move(f); }
    void StartStation() { ++stations; }
    void StopStation() { ++stops; connected=false; }
    void StartConfigAp() { ++aps; config=true; callback(WifiEvent::ConfigModeEnter,""); }
    void StopConfigAp() { if(config) { config=false; callback(WifiEvent::ConfigModeExit,""); } }
    bool IsConnected() { return connected; }
    bool IsConfigMode() { return config; }
    std::string GetApSsid() { return "test"; }
    std::string GetApWebUrl() { return "test"; }
};
struct WifiBoard {
    void* connect_timer_=nullptr;
    bool in_config_mode_=false;
    std::atomic<bool> network_enabled_{false}, suppress_config_reconnect_{false};
    NetworkEventCallback network_event_callback_;
    bool InitializeWifi(); void StartNetwork(); bool SetNetworkEnabled(bool);
    bool RequestWifiConfiguration(); bool IsNetworkConnected() const;
    bool IsWifiConfigurationActive() const; bool HasNetworkConfiguration() const;
    void TryWifiConnect(); void OnNetworkEvent(NetworkEvent,const std::string& data="");
    static void OnWifiConnectTimeout(void*); void StartWifiConfigMode();
};
'''
        checks = r'''
int main() {
    WifiBoard board;
    auto& wifi=WifiManager::GetInstance();
#if CONFIG_WAVESHARE_1_85C_HOME_OFFLINE
    assert(board.SetNetworkEnabled(false));
    assert(wifi.initialized && wifi.stations==0 && wifi.aps==0);
    assert(board.SetNetworkEnabled(true)); assert(wifi.stations==1);
    WifiBoard::OnWifiConnectTimeout(&board);
    assert(wifi.aps==0 && timer_starts==0 && delays==0);
    assert(board.RequestWifiConfiguration()); assert(wifi.config);
    assert(app_mutations==0); // owner main task makes all application transitions
    auto stations=wifi.stations;
    assert(board.SetNetworkEnabled(false));
    assert(!wifi.config && wifi.stations==stations); // synchronous Exit must not reconnect
    assert(board.RequestWifiConfiguration());
    wifi.StopConfigAp(); assert(wifi.stations==stations+1); // saved credentials still connect
    SsidManager::GetInstance().saved=false;
    assert(board.SetNetworkEnabled(true)); assert(wifi.aps==2); // application owns first-boot config
#else
    assert(!board.SetNetworkEnabled(false)); assert(!wifi.initialized);
    board.StartNetwork(); assert(wifi.stations==1 && timer_starts==1);
    WifiBoard::OnWifiConnectTimeout(&board); assert(wifi.aps==1);
    assert(app_mutations>0); // old non-HOME timeout/config behavior retained
#endif
}
'''
        with tempfile.TemporaryDirectory() as directory:
            build=Path(directory)
            (build / "test.cc").write_text(platform + methods + checks)
            for home in (0, 1):
                with self.subTest(home=home):
                    binary=build / f"wifi-{home}"
                    compiled=subprocess.run(["c++", "-std=c++20", f"-DCONFIG_WAVESHARE_1_85C_HOME_OFFLINE={home}",
                        "-DCONFIG_USE_HOTSPOT_WIFI_PROVISIONING=1", str(build / "test.cc"), "-o", str(binary)],
                        capture_output=True, text=True, timeout=30)
                    self.assertEqual(compiled.returncode,0,compiled.stdout+compiled.stderr)
                    run=subprocess.run([str(binary)],capture_output=True,text=True,timeout=10)
                    self.assertEqual(run.returncode,0,run.stdout+run.stderr)


if __name__ == "__main__":
    unittest.main()
