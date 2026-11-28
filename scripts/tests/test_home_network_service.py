"""Exercise the bounded recovery worker with real WS/MQTT and platform adapters."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

from test_protocol_health import STUBS, TRANSPORT_STUBS

ROOT = Path(__file__).resolve().parents[2]


class HomeNetworkServiceTests(unittest.TestCase):
    def test_recovery_evidence_cancellation_and_mailbox(self):
        self.assertTrue((ROOT / "main/home_network_service.cc").exists(),
                        "bounded recovery worker has not been implemented")
        compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not compiler or not shutil.which(compiler[0]):
            self.skipTest("requires a host C++ compiler")
        stubs = {**STUBS, **TRANSPORT_STUBS}
        stubs["esp_timer.h"] = stubs["esp_timer.h"].replace("#include <cstdint>", "#include <cstdint>\n#include <atomic>").replace("inline int64_t clock_us = 0;", "inline std::atomic<int64_t> clock_us{0};")
        stubs["esp_timer.h"] = stubs["esp_timer.h"].replace("struct Timer {", "inline int timer_creations=0;\nstruct Timer {").replace("*t = new Timer;", "++timer_creations; *t = new Timer;")
        stubs["freertos/FreeRTOS.h"] += "\n#define pdPASS 1\n"
        stubs["freertos/task.h"] = r'''
#pragma once
#include <functional>
#include <thread>
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
inline bool task_create_ok=true;
inline std::function<void()> pending_task;
inline int xTaskCreate(void (*f)(void*),const char*,unsigned,void* arg,int,void*) {
    if (!task_create_ok) return 0;
    pending_task=[=] { f(arg); }; return pdPASS;
}
inline void vTaskDelete(void*) {}
inline void vTaskDelay(unsigned ms) { clock_us+=ms*1000; std::this_thread::yield(); }
inline void RunWorker() { auto f=std::move(pending_task); pending_task=nullptr; f(); }
'''
        stubs["transport_state.h"] += "\ninline std::function<void()> goodbye_hook;\n"
        stubs["mqtt.h"] = stubs["mqtt.h"].replace('bool Publish(const std::string&, const std::string& text) {',
            'bool Publish(const std::string&, const std::string& text) { if(text.find("goodbye")!=std::string::npos && goodbye_hook) goodbye_hook();')
        stubs["application.h"] = r'''
#pragma once
#include <cassert>
constexpr int kDeviceStateIdle=0;
inline int app_work=0;
struct Application {
    static Application& GetInstance() { static Application a; return a; }
    int GetDeviceState() { return kDeviceStateIdle; }
    template<class F> void Schedule(F) { ++app_work; }
    void NotifyProtocolPending() { ++app_work; }
};
'''
        stubs["ota.h"] = r'''
#pragma once
#include <functional>
#include <string>
#include "transport_state.h"
#include "esp_timer.h"
inline bool check_ok=true, mqtt_config=false, ws_config=true, activation_code=false;
inline bool activation_challenge=false, activation_ok=true;
inline int check_count=0, activation_count=0, marked_count=0;
inline std::function<void()> check_hook;
constexpr int ESP_FAIL=-1;
struct Ota {
    std::function<bool()> cancelled;
    void SetCancellationCheck(std::function<bool()> f) { cancelled=std::move(f); }
    Result CheckVersion() { ++check_count; if(check_hook) check_hook(); return {check_ok}; }
    bool HasMqttConfig() { return mqtt_config; }
    bool HasWebsocketConfig() { return ws_config; }
    bool HasServerTime() { return true; }
    bool HasActivationCode() { return activation_code; }
    bool HasActivationChallenge() { return activation_challenge; }
    std::string GetActivationMessage() { return "activate device"; }
    std::string GetActivationCode() { return "123456"; }
    int Activate() { ++activation_count; return activation_ok?ESP_OK:ESP_FAIL; }
    void MarkCurrentVersionValid() { ++marked_count; }
};
'''
        stubs["board.h"] = TRANSPORT_STUBS["board.h"].replace(
            "struct Board {", "inline bool network_connected=true;\nstruct Board {").replace(
            "Network* GetNetwork()", "bool IsNetworkConnected() const { return network_connected; }\n    Network* GetNetwork()")
        source = r'''
#include <cassert>
#include <chrono>
#include <atomic>
#include <psa/crypto.h>
#include "home_network_service.h"
#include "ota.h"
#include "board.h"
#include "application.h"
#include "freertos/task.h"
std::string Pong(std::string bytes) {
    bytes[0]=3; std::array<uint8_t,16> key; for(unsigned i=0;i<16;++i) key[i]=i;
    unsigned length=0; uint8_t digest[32];
    HMAC(EVP_sha256(),key.data(),16,reinterpret_cast<const uint8_t*>(bytes.data()),16,digest,&length);
    memcpy(bytes.data()+16,digest,16); return bytes;
}
int main() {
    HomeNetworkService service;
    HomeNetworkService::Result result; result.generation=99;
    assert(!service.TakeResult(result) && result.generation==99);
    assert(!service.StartRecovery(0));
    task_create_ok=false; assert(!service.StartRecovery(1) && !service.Busy()); task_create_ok=true;
    assert(service.StartRecovery(1) && service.Busy());
    assert(!service.StartRecovery(2));
    RunWorker(); assert(!service.Busy()); assert(!service.StartRecovery(2));
    assert(service.TakeResult(result) && result.generation==1 && result.ready && !result.use_mqtt);
    assert(result.has_server_time && app_work==0);
    // MQTT hello is enough when the server did not negotiate UDP probing.
    mqtt_config=true; ws_config=false;
    assert(service.StartRecovery(2)); RunWorker();
    assert(service.TakeResult(result) && result.ready && result.use_mqtt);
    goodbye_hook=[] { mqtt_publish_ok=false; };
    assert(service.StartRecovery(20)); RunWorker();
    assert(service.TakeResult(result) && !result.ready); // close must not hide a new transport failure
    goodbye_hook=nullptr; mqtt_publish_ok=true;
    mqtt_notify_connected=false;
    assert(service.StartRecovery(21)); RunWorker();
    assert(service.TakeResult(result) && !result.ready); // socket-only success is insufficient
    mqtt_notify_connected=true;
    // A goodbye received immediately after hello must fail without scheduling app work.
    hello_dispatch=[](auto deliver) {
        deliver();
        Board::GetInstance().network.mqtt->message("test",R"({"type":"goodbye","session_id":"current"})");
    };
    assert(service.StartRecovery(22)); RunWorker();
    assert(service.TakeResult(result) && !result.ready && app_work==0);
    hello_dispatch=nullptr;
    mqtt_hello.insert(mqtt_hello.size()-1,R"(,"features":{"udp_probe":true})");
    udp_on_send=[&](const auto& packet) { Board::GetInstance().network.udp->message(Pong(packet)); };
    assert(service.StartRecovery(3)); RunWorker();
    assert(service.TakeResult(result) && result.ready);
    udp_on_send=nullptr;
    assert(service.StartRecovery(4)); RunWorker();
    assert(service.TakeResult(result) && !result.ready && !result.error.empty());
    // Cancellation while config I/O was in flight cannot publish a late success.
    check_hook=[&] { service.Invalidate(6); };
    assert(service.StartRecovery(5)); RunWorker();
    assert(service.TakeResult(result) && result.generation==5 && !result.ready);
    check_hook=nullptr;
    activation_code=true; activation_challenge=true;
    const auto checks=check_count, activations=activation_count;
    assert(service.StartRecovery(6)); RunWorker();
    assert(service.TakeResult(result) && !result.ready && !result.activation_message.empty());
    assert(check_count==checks+1 && activation_count==activations+1); // no activation/check loop
    activation_code=false; activation_challenge=false;
    check_ok=false; assert(service.StartRecovery(7)); RunWorker();
    assert(service.TakeResult(result) && !result.ready && !result.error.empty()); check_ok=true;
    assert(service.StartRecovery(8)); service.Invalidate(9); RunWorker();
    assert(service.TakeResult(result) && !result.ready && result.generation==8);
    network_connected=false;
    assert(service.StartRecovery(9)); RunWorker();
    assert(service.TakeResult(result) && !result.ready);
    assert(app_work==0 && timer_creations==0);
    network_connected=true; mqtt_config=false; ws_config=true;
    assert(service.StartRecovery(10)); RunWorker(); service.Invalidate(11);
    assert(service.TakeResult(result) && !result.ready && result.generation==10);
    // A late hello completing after mode cancellation cannot publish ready.
    hello_dispatch=[&](auto deliver) { service.Invalidate(13); deliver(); };
    assert(service.StartRecovery(12)); RunWorker();
    assert(service.TakeResult(result) && !result.ready && result.generation==12);
    hello_dispatch=nullptr;
    // Destruction waits for a genuinely running worker, including blocked config I/O.
    std::atomic<bool> io_entered{false}, release_io{false}, destroying{false}, destroyed{false};
    check_hook=[&] { io_entered=true; while(!release_io) std::this_thread::yield(); };
    auto owned=std::make_unique<HomeNetworkService>();
    assert(owned->StartRecovery(14));
    std::thread worker([] { RunWorker(); });
    const auto limit=std::chrono::steady_clock::now()+std::chrono::seconds(3);
    while(!io_entered && std::chrono::steady_clock::now()<limit) std::this_thread::yield();
    assert(io_entered);
    std::thread destroyer([&] { destroying=true; owned.reset(); destroyed=true; });
    while(!destroying) std::this_thread::yield();
    assert(!destroyed); release_io=true;
    worker.join(); destroyer.join(); assert(destroyed);
    check_hook=nullptr;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory)
            for name, contents in stubs.items():
                target = build / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents)
            for name in ("home_network_service.h", "home_network_service.cc"):
                shutil.copyfile(ROOT / "main" / name, build / name)
            (build / "test.cc").write_text(source)
            flags = shlex.split(os.environ.get("MCP_CJSON_CFLAGS", "-I/usr/include/cjson"))
            libs = shlex.split(os.environ.get("MCP_CJSON_LIBS", "-lcjson"))
            compiled = subprocess.run(compiler + ["-std=c++20", "-pthread", "-Wall", "-Wextra", "-Werror",
                "-Wno-unused-parameter", f"-I{build}", f"-I{ROOT}", f"-I{ROOT / 'main'}", *flags,
                str(build / "test.cc"), str(build / "home_network_service.cc"),
                *(str(ROOT / "main/protocols" / name) for name in ("protocol.cc", "mqtt_protocol.cc", "websocket_protocol.cc")),
                *libs, "-lcrypto", "-o", str(build / "test")], capture_output=True, text=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            run = subprocess.run([str(build / "test")], capture_output=True, text=True, timeout=15)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
