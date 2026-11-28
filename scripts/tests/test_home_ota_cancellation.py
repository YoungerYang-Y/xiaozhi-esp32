"""Run production config parsing: cancellation must precede persistent effects."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_startup_wake_word_hint import definition

ROOT = Path(__file__).resolve().parents[2]


class HomeOtaCancellationTests(unittest.TestCase):
    def test_cancelled_response_never_updates_settings_or_clock(self):
        if not shutil.which("c++"):
            self.skipTest("requires a host C++ compiler")
        method=definition((ROOT / "main/ota.cc").read_text(), "NetworkResult<> Ota::CheckVersion()")
        platform=r'''
#include <cassert>
#include <expected>
#include <functional>
#include <memory>
#include <string>
#include <sys/time.h>
#include <cJSON.h>
#include "main/cjson_utils.h"
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
struct Error { static Error InvalidArgument(){return{};} static Error ProtocolError(){return{};}
    static Error NetworkUnavailable(){return{};} static Error HttpFailed(int){return{};}
    std::string ToString()const{return "error";}
};
using NetworkError=Error;
template<class T=void> using NetworkResult=std::expected<T,Error>;
inline bool cancelled=false;
inline int settings_writes=0, clock_writes=0, opens=0;
inline std::function<void()> response_hook, settings_read_hook;
struct Http {
    void SetContent(std::string){}
    NetworkResult<> Open(const std::string&,const std::string&){++opens;return{};}
    NetworkResult<int> GetStatusCode(){return 200;}
    std::string ReadAll(){
        if(response_hook) response_hook();
        return R"({"mqtt":{"endpoint":"mqtt","client_id":"id"},"websocket":{"url":"wss://server"},"server_time":{"timestamp":1700000000000}})";
    }
    void Close(){}
};
struct Board {
    static Board& GetInstance(){static Board b;return b;}
    std::string GetSystemInfoJson(){return "{}";}
};
struct Description{ const char* version="2.6.0"; };
const Description* esp_app_get_description(){static Description d;return &d;}
struct Protocol { static void AddClientCapabilities(cJSON*,bool){} };
struct Settings {
    Settings(const char*,bool){}
    std::string GetString(const char*){if(settings_read_hook)settings_read_hook();return "old";}
    int GetInt(const char*){return 0;}
    void SetString(const char*,const char*){++settings_writes;}
    void SetInt(const char*,int){++settings_writes;}
};
int HostSetTime(const timeval*,void*){++clock_writes;return 0;}
#define settimeofday HostSetTime
struct Ota {
    std::function<bool()> cancellation_check_;
    bool IsCancelled() const {return cancellation_check_&&cancellation_check_();}
    bool has_activation_code_=false,has_activation_challenge_=false;
    bool has_mqtt_config_=false,has_websocket_config_=false,has_server_time_=false,has_new_version_=false;
    std::string current_version_,activation_message_,activation_code_,activation_challenge_,firmware_version_,firmware_url_;
    int activation_timeout_ms_=30000;
    std::string GetCheckVersionUrl(){return "https://test/ota/";}
    bool IsValidUrl(const std::string&){return true;}
    std::unique_ptr<Http> SetupHttp(){return std::make_unique<Http>();}
    bool IsNewVersionAvailable(const std::string&,const std::string&){return false;}
    NetworkResult<> CheckVersion();
};
'''
        checks=r'''
int main(){
    Ota ota;
    ota.cancellation_check_=[]{return cancelled;};
    response_hook=[]{cancelled=true;};
    assert(!ota.CheckVersion());
    assert(settings_writes==0 && clock_writes==0 && opens==1);
    cancelled=false;response_hook=nullptr;
    // Cancellation may also arrive after parse, before an individual NVS write.
    settings_read_hook=[]{cancelled=true;};
    assert(!ota.CheckVersion());
    assert(settings_writes==0 && clock_writes==0);
    settings_read_hook=nullptr;
    // Default callers have no cancellation hook and retain the old behavior.
    Ota legacy; assert(legacy.CheckVersion());
    assert(settings_writes==3 && clock_writes==1);
}
'''
        with tempfile.TemporaryDirectory() as directory:
            build=Path(directory)
            (build / "test.cc").write_text(platform+method+checks)
            compiled=subprocess.run(["c++","-std=c++23",f"-I{ROOT}","-I/usr/include/cjson",
                str(build / "test.cc"),"-lcjson","-o",str(build / "test")],capture_output=True,text=True,timeout=30)
            self.assertEqual(compiled.returncode,0,compiled.stdout+compiled.stderr)
            run=subprocess.run([str(build / "test")],capture_output=True,text=True,timeout=10)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)


if __name__ == "__main__":
    unittest.main()
