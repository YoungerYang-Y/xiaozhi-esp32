"""Run production protocol paths with host platform and transport adapters."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]

STUBS = {
    "esp_log.h": "#pragma once\ntemplate<class... T> void HostLog(T&&...) {}\n#define ESP_LOGE(...) HostLog(__VA_ARGS__)\n#define ESP_LOGI(...) HostLog(__VA_ARGS__)\n#define ESP_LOGW(...) HostLog(__VA_ARGS__)\n#define ESP_LOGD(...) HostLog(__VA_ARGS__)\n",
    "assets.h": r'''
#pragma once
#include <string>
struct Assets {
    struct Capability { bool glyph_push = false; std::string bundle, charset; int size=0, bpp=0; };
    static Assets& GetInstance() { static Assets a; return a; }
    Capability text_font_capability() { return {}; }
};
''',
}

TRANSPORT_STUBS = {
    "freertos/FreeRTOS.h": "#pragma once\n#define pdTRUE 1\n#define pdFALSE 0\n#define pdMS_TO_TICKS(x) (x)\n",
    "freertos/event_groups.h": r'''
#pragma once
#include <functional>
using EventBits_t = unsigned;
using EventGroupHandle_t = unsigned*;
inline std::function<void()> wait_hook;
inline EventGroupHandle_t xEventGroupCreate() { return new unsigned(0); }
inline void vEventGroupDelete(EventGroupHandle_t e) { delete e; }
inline void xEventGroupSetBits(EventGroupHandle_t e, unsigned b) { *e |= b; }
inline void xEventGroupClearBits(EventGroupHandle_t e, unsigned b) { *e &= ~b; }
inline unsigned xEventGroupWaitBits(EventGroupHandle_t e, unsigned b, int clear, int, int) {
    if (wait_hook) { auto f = std::move(wait_hook); wait_hook = nullptr; f(); }
    const auto bits = *e & b;
    if (clear) *e &= ~b;
    return bits;
}
''',
    "esp_timer.h": r'''
#pragma once
#include <cstdint>
inline int64_t clock_us = 0;
inline int64_t esp_timer_get_time() { return clock_us; }
struct Timer { int64_t due = 0; bool active = false; };
using esp_timer_handle_t = Timer*;
struct esp_timer_create_args_t { void (*callback)(void*); void* arg=nullptr; const char* name=nullptr; };
constexpr int ESP_OK = 0;
inline int esp_timer_create(const esp_timer_create_args_t*, Timer** t) { *t = new Timer; return 0; }
inline void esp_timer_stop(Timer* t) { if (t) t->active = false; }
inline void esp_timer_delete(Timer* t) { delete t; }
inline void esp_timer_start_once(Timer* t, int64_t delay) { t->due = clock_us+delay; t->active = true; }
''',
    "esp_random.h": "#pragma once\ninline unsigned esp_random() { return 0; }\n",
    "application.h": r'''
#pragma once
constexpr int kDeviceStateIdle = 0;
struct Application {
    static Application& GetInstance() { static Application a; return a; }
    int GetDeviceState() { return kDeviceStateIdle; }
    template<class F> void Schedule(F f) { f(); }
    void NotifyProtocolPending() {}
};
''',
    "settings.h": r'''
#pragma once
#include <string>
struct Settings {
    Settings(const char*, bool) {}
    std::string GetString(const char* key) { return std::string(key)=="endpoint" ? "localhost:1883" : "test"; }
    int GetInt(const char*, int fallback=1) { return fallback; }
};
''',
    "system_info.h": "#pragma once\nstruct SystemInfo { static std::string GetMacAddress() { return \"test\"; } };\n",
    "assets/lang_config.h": r'''
#pragma once
#define OPUS_FRAME_DURATION_MS 60
namespace Lang { namespace Strings {
constexpr const char* SERVER_ERROR = "error";
constexpr const char* SERVER_TIMEOUT = "timeout";
constexpr const char* SERVER_NOT_CONNECTED = "not connected";
constexpr const char* SERVER_NOT_FOUND = "not found";
} }
''',
    "transport_state.h": r'''
#pragma once
#include <functional>
#include <string>
struct Result {
    bool ok = true;
    explicit operator bool() const { return ok; }
    Result error() const { return {}; }
    std::string ToString() const { return "test error"; }
};
inline bool create_udp_ok=true, udp_connect_ok=true, udp_send_ok=true;
inline bool create_ws_ok=true, ws_connect_ok=true, ws_send_ok=true;
inline bool ws_disconnect_on_destroy=false;
inline bool ws_disconnect_during_connect=false;
inline bool mqtt_connect_ok=true, mqtt_notify_connected=true, mqtt_publish_ok=true;
inline std::function<void(const std::string&)> udp_on_send;
inline std::function<void(std::function<void()>)> hello_dispatch;
inline std::string mqtt_hello = R"({"type":"hello","transport":"udp","session_id":"current","udp":{"server":"test","port":1234,"key":"000102030405060708090a0b0c0d0e0f","nonce":"01000000112233440000000000000000"}})";
inline std::string ws_hello = R"({"type":"hello","transport":"websocket","session_id":"current"})";
''',
    "mqtt.h": r'''
#pragma once
#include "transport_state.h"
struct Mqtt {
    std::function<void()> connected, disconnected;
    std::function<void(const std::string&, const std::string&)> message;
    bool online=false;
    void SetKeepAlive(int) {}
    void OnConnected(decltype(connected) f) { connected=std::move(f); }
    void OnDisconnected(decltype(disconnected) f) { disconnected=std::move(f); }
    void OnMessage(decltype(message) f) { message=std::move(f); }
    bool IsConnected() { return online; }
    Result Connect(const std::string&, int, const std::string&, const std::string&, const std::string&) {
        online=mqtt_connect_ok;
        if (online && mqtt_notify_connected) connected();
        return {online};
    }
    bool Publish(const std::string&, const std::string& text) {
        if (mqtt_publish_ok && text.find("\"type\":\"hello\"")!=std::string::npos && !mqtt_hello.empty()) {
            auto deliver=[callback=message, response=mqtt_hello] { callback("test",response); };
            if (hello_dispatch) hello_dispatch(deliver); else deliver();
        }
        return mqtt_publish_ok;
    }
};
''',
    "udp.h": r'''
#pragma once
#include "transport_state.h"
#include <vector>
struct Udp {
    std::function<void(const std::string&)> message;
    std::function<void(const std::string&)> on_send;
    std::vector<std::string> sent;
    void OnMessage(decltype(message) f) { message=std::move(f); }
    Result Connect(const std::string&, int) { return {udp_connect_ok}; }
    int Send(const std::string& data) {
        sent.push_back(data);
        if (on_send) on_send(data);
        return udp_send_ok ? static_cast<int>(data.size()) : -1;
    }
};
''',
    "web_socket.h": r'''
#pragma once
#include "transport_state.h"
struct WebSocket {
    std::function<void(const char*,size_t,bool)> data;
    std::function<void()> disconnected, on_send, on_text_send;
    bool online=true;
    ~WebSocket() { if (ws_disconnect_on_destroy && disconnected) disconnected(); }
    void SetHeader(const char*,const char*) {}
    bool IsConnected() const { return online; }
    void OnData(decltype(data) f) { data=std::move(f); }
    void OnPing(std::function<void(const char*,size_t)>) {}
    void OnDisconnected(decltype(disconnected) f) { disconnected=std::move(f); }
    Result Connect(const char*) {
        online=ws_connect_ok;
        if (!online && ws_disconnect_during_connect) disconnected();
        return {online};
    }
    bool Send(const std::string& text) {
        if (on_text_send && text.find("\"type\":\"hello\"")==std::string::npos) on_text_send();
        if (ws_send_ok && text.find("\"type\":\"hello\"")!=std::string::npos && !ws_hello.empty()) {
            auto deliver=[callback=data, response=ws_hello] { callback(response.data(),response.size(),false); };
            if (hello_dispatch) hello_dispatch(deliver); else deliver();
        }
        return ws_send_ok;
    }
    bool Send(const void*,size_t,bool) { if (on_send) on_send(); return ws_send_ok; }
    void SendPong(const char*,size_t) {}
    void Deliver(const std::string& text, bool binary=false) { data(text.data(),text.size(),binary); }
};
''',
    "board.h": r'''
#pragma once
#include "mqtt.h"
#include "udp.h"
#include "web_socket.h"
#include <memory>
struct Network {
    Mqtt* mqtt=nullptr; Udp* udp=nullptr; WebSocket* ws=nullptr;
    std::unique_ptr<Mqtt> CreateMqtt(int) { auto p=std::make_unique<Mqtt>(); mqtt=p.get(); return p; }
    std::unique_ptr<Udp> CreateUdp(int) {
        if (!create_udp_ok) return nullptr;
        auto p=std::make_unique<Udp>(); p->on_send=udp_on_send; udp=p.get(); return p;
    }
    std::unique_ptr<WebSocket> CreateWebSocket(int) {
        if (!create_ws_ok) return nullptr;
        auto p=std::make_unique<WebSocket>(); ws=p.get(); return p;
    }
};
struct Board {
    Network network;
    static Board& GetInstance() { static Board b; return b; }
    Network* GetNetwork() { return &network; }
    std::string GetUuid() { return "test"; }
    std::string GetBoardType() { return "test"; }
};
''',
    "psa/crypto.h": r'''
#pragma once
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <array>
#include <map>
#include <openssl/hmac.h>
#include <openssl/crypto.h>
using psa_key_id_t = unsigned;
using psa_status_t = int;
using psa_key_attributes_t = int;
using psa_cipher_operation_t = int;
constexpr unsigned PSA_KEY_ID_NULL=0, PSA_SUCCESS=0, PSA_KEY_ATTRIBUTES_INIT=0;
constexpr unsigned PSA_CIPHER_OPERATION_INIT=0, PSA_KEY_TYPE_HMAC=0, PSA_KEY_TYPE_AES=0;
constexpr unsigned PSA_KEY_USAGE_SIGN_MESSAGE=0, PSA_KEY_USAGE_VERIFY_MESSAGE=0;
constexpr unsigned PSA_KEY_USAGE_ENCRYPT=0, PSA_KEY_USAGE_DECRYPT=0, PSA_ALG_SHA_256=0, PSA_ALG_CTR=0;
#define PSA_ALG_HMAC(x) (x)
#define PSA_ALG_TRUNCATED_MAC(x,y) (x)
inline std::map<unsigned,std::array<uint8_t,16>> keys;
inline unsigned next_key=0;
inline bool crypto_ok=true, sign_ok=true;
inline int psa_crypto_init() { return crypto_ok ? 0 : -1; }
inline void psa_set_key_type(int*,unsigned) {}
inline void psa_set_key_bits(int*,unsigned) {}
inline void psa_set_key_usage_flags(int*,unsigned) {}
inline void psa_set_key_algorithm(int*,unsigned) {}
inline void psa_reset_key_attributes(int*) {}
inline int psa_import_key(int*,const uint8_t* bytes,size_t size,unsigned* id) {
    if (!crypto_ok || size!=16) return -1;
    *id=++next_key; memcpy(keys[*id].data(),bytes,16); return 0;
}
inline int psa_destroy_key(unsigned id) { keys.erase(id); return 0; }
inline int psa_mac_compute(unsigned id,unsigned,const uint8_t* input,size_t size,uint8_t* out,size_t capacity,size_t* length) {
    if (!sign_ok || !keys.count(id) || capacity<16) return -1;
    unsigned count=0; uint8_t digest[32];
    HMAC(EVP_sha256(),keys[id].data(),16,input,size,digest,&count);
    memcpy(out,digest,16); *length=16; return 0;
}
inline int psa_mac_verify(unsigned id,unsigned alg,const uint8_t* input,size_t size,const uint8_t* tag,size_t tag_size) {
    uint8_t actual[16]; size_t length=0;
    return tag_size==16 && psa_mac_compute(id,alg,input,size,actual,16,&length)==0 && CRYPTO_memcmp(actual,tag,16)==0 ? 0 : -1;
}
inline int psa_cipher_encrypt_setup(int*,unsigned,unsigned) { return crypto_ok ? 0 : -1; }
inline int psa_cipher_set_iv(int*,const uint8_t*,size_t) { return 0; }
inline int psa_cipher_update(int*,const uint8_t* in,size_t n,uint8_t* out,size_t,size_t* len) { memcpy(out,in,n); *len=n; return 0; }
inline int psa_cipher_finish(int*,uint8_t*,size_t,size_t* len) { *len=0; return 0; }
inline int psa_cipher_abort(int*) { return 0; }
''',
}


class ProtocolHealthTest(unittest.TestCase):
    def compile_and_run(self, source, sources, stubs=None, extra_flags=()):
        compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not compiler or not shutil.which(compiler[0]):
            self.skipTest("requires a host C++ compiler")
        flags = shlex.split(os.environ.get("MCP_CJSON_CFLAGS", "-I/usr/include/cjson"))
        libs = shlex.split(os.environ.get("MCP_CJSON_LIBS", "-lcjson"))
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory)
            for name, text in {**STUBS, **(stubs or {})}.items():
                path = build / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            (build / "test.cc").write_text(source)
            result = subprocess.run(
                compiler + ["-std=c++20", "-pthread", "-Wall", "-Wextra", "-Werror",
                            "-Wno-unused-parameter", f"-I{build}", f"-I{ROOT}", *flags,
                            str(build / "test.cc"),
                            *(str(ROOT / "main/protocols" / name) for name in sources),
                            *libs, *extra_flags, "-o", str(build / "test")],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(build / "test")], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_optional_callback_keeps_stage_and_source_identity(self):
        self.compile_and_run(r'''
#include <cassert>
#include "main/protocols/protocol.h"
struct TestProtocol : Protocol {
    bool Start() override { return true; }
    bool OpenAudioChannel() override { return true; }
    void CloseAudioChannel(bool) override {}
    bool IsAudioChannelOpened() const override { return false; }
    bool SendAudio(std::unique_ptr<AudioStreamPacket>) override { return true; }
    bool SendText(const std::string&) override { return true; }
    using Protocol::EmitHealthEvent;
};
int main() {
    using Stage = Protocol::HealthStage;
    using Outcome = Protocol::HealthOutcome;
    TestProtocol p;
    p.EmitHealthEvent({Stage::Media, Outcome::Failed, 7, 7, true}); // no consumer
    std::vector<Protocol::HealthEvent> events;
    p.OnHealthEvent([&](const auto& e) { events.push_back(e); });
    p.EmitHealthEvent({Stage::Media, Outcome::Started, 7, 7, true});
    p.EmitHealthEvent({Stage::Control, Outcome::Succeeded, 8, 0});
    assert(events.size() == 2);
    assert(events[0].stage == Stage::Media && events[0].attempt_id == 7);
    assert(events[0].source_channel_token == 7 && events[0].udp_probe);
    assert(events[1].stage == Stage::Control && events[1].attempt_id == 8);
    assert(events[1].source_channel_token == 0 && !events[1].udp_probe);
    assert(p.channel_token() == 0); // health events never advance conversations
    p.OnHealthEvent(nullptr);
    p.EmitHealthEvent({Stage::Media, Outcome::Retired, 7, 7});
    assert(events.size() == 2);
}
''', ["protocol.cc"])

    def _assert_hello_completion_stays_with_open(self, mqtt):
        stubs = {**TRANSPORT_STUBS, "freertos/event_groups.h": r'''
#pragma once
#include <functional>
#include <mutex>
using EventBits_t = unsigned;
struct HostEvents { std::mutex mutex; unsigned bits=0; };
using EventGroupHandle_t = HostEvents*;
inline std::function<void()> wait_hook;
inline std::function<void()> before_wait;
inline EventGroupHandle_t xEventGroupCreate() { return new HostEvents; }
inline void vEventGroupDelete(EventGroupHandle_t e) { delete e; }
inline void xEventGroupSetBits(EventGroupHandle_t e, unsigned b) {
    std::lock_guard<std::mutex> lock(e->mutex); e->bits|=b;
}
inline void xEventGroupClearBits(EventGroupHandle_t e, unsigned b) {
    std::lock_guard<std::mutex> lock(e->mutex); e->bits&=~b;
}
inline unsigned read_bits(EventGroupHandle_t e) {
    std::lock_guard<std::mutex> lock(e->mutex); return e->bits;
}
inline std::function<unsigned(EventGroupHandle_t,unsigned,int)> controlled_wait;
inline unsigned xEventGroupWaitBits(EventGroupHandle_t e, unsigned b, int clear, int, int) {
    return controlled_wait(e,b,clear);
}
'''}
        self.compile_and_run(r'''
#include <cassert>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <thread>
#include "main/protocols/mqtt_protocol.h"
#include "main/protocols/websocket_protocol.h"
#include "board.h"
#if TEST_MQTT
using SelectedProtocol=MqttProtocol;
#else
using SelectedProtocol=WebsocketProtocol;
#endif
struct Gate {
    std::mutex mutex;
    std::condition_variable changed;
    bool parser_paused=false, release_parser=false, b_waiting=false;
    bool old_parser_done=false, inspected=false, allow_b_completion=false;
    unsigned stale_bits=0;
    template<class Predicate> void wait(Predicate predicate) {
        std::unique_lock<std::mutex> lock(mutex);
        assert(changed.wait_for(lock,std::chrono::seconds(5),predicate));
    }
    template<class Mutation> void set(Mutation mutation) {
        std::lock_guard<std::mutex> lock(mutex); mutation(); changed.notify_all();
    }
} gate;
int main() {
    SelectedProtocol protocol;
    std::thread first_receiver;
    std::function<void()> deliver_b;
    std::atomic<uint64_t> first_token{0};
    std::atomic<bool> real_b_hello{false}, b_finished_without_hello{false};
    int dispatched=0, waits=0;
    protocol.OnHealthEvent([&](const auto& event) {
        (void)protocol.IsAudioChannelOpened(); // production health callbacks remain lock-free
        if (event.stage==Protocol::HealthStage::Handshake &&
            event.outcome==Protocol::HealthOutcome::Started && first_token==0) {
            first_token=event.source_channel_token;
        }
        if (event.stage==Protocol::HealthStage::Media &&
            event.outcome==Protocol::HealthOutcome::Started &&
            event.source_channel_token==first_token) {
            gate.set([&] { gate.parser_paused=true; });
            gate.wait([&] { return gate.release_parser; });
        }
    });
    hello_dispatch=[&](auto deliver) {
        if (++dispatched==1) first_receiver=std::thread(std::move(deliver));
        else deliver_b=std::move(deliver);
    };
    controlled_wait=[&](auto events,unsigned bit,int clear) {
        if (++waits==1) {
            // A has parsed its hello and emitted health, but cannot set its completion bit.
            gate.wait([&] { return gate.parser_paused; });
            return 0u; // deterministic expiration of A's real OpenAudioChannel wait
        }
        gate.set([&] { gate.b_waiting=true; });
        gate.wait([&] { return gate.old_parser_done; });
        const auto stale=read_bits(events)&bit;
        gate.set([&] { gate.stale_bits=stale; gate.inspected=true; });
        if (!stale) gate.wait([&] { return gate.allow_b_completion; });
        const auto result=read_bits(events)&bit;
        if (clear) xEventGroupClearBits(events,bit);
        return result;
    };
    assert(!protocol.OpenAudioChannel());
    bool b_result=false;
    std::thread second_opener([&] {
        b_result=protocol.OpenAudioChannel();
        b_finished_without_hello=!real_b_hello.load();
    });
    gate.wait([&] { return gate.b_waiting; }); // B already cleared the shared event bit
    gate.set([&] { gate.release_parser=true; });
    first_receiver.join(); // run the old parser's actual production completion path
    gate.set([&] { gate.old_parser_done=true; });
    gate.wait([&] { return gate.inspected; });
    if (gate.stale_bits) {
        second_opener.join();
#if TEST_MQTT
        assert(!b_finished_without_hello && "old MQTT hello prematurely failed the new opener");
#else
        assert(!b_finished_without_hello && "old WebSocket hello falsely opened the new channel");
#endif
    }
    assert(deliver_b);
    real_b_hello=true;
    deliver_b(); // only B's real hello may complete B's OpenAudioChannel
    gate.set([&] { gate.allow_b_completion=true; });
    second_opener.join();
    assert(b_result && !b_finished_without_hello);
    assert(protocol.session_id()=="current");
    protocol.CloseAudioChannel(false);
    hello_dispatch=nullptr;
}
''', ["protocol.cc", "mqtt_protocol.cc", "websocket_protocol.cc"], stubs,
            ["-lcrypto", f"-DTEST_MQTT={int(mqtt)}"])

    def test_websocket_old_hello_completion_cannot_open_next_channel(self):
        self._assert_hello_completion_stays_with_open(False)

    def test_mqtt_old_hello_completion_cannot_fail_next_channel(self):
        self._assert_hello_completion_stays_with_open(True)

    def test_transport_evidence_failures_and_stale_sources(self):
        self.compile_and_run(r'''
#include <cassert>
#include <algorithm>
#include "main/protocols/mqtt_protocol.h"
#include "main/protocols/websocket_protocol.h"
#include "board.h"
using Stage=Protocol::HealthStage;
using Outcome=Protocol::HealthOutcome;
using Event=Protocol::HealthEvent;
std::vector<Event> events;
auto count(Stage stage, Outcome outcome) {
    return std::count_if(events.begin(),events.end(),[&](const auto& e) { return e.stage==stage && e.outcome==outcome; });
}
void watch(Protocol& p) {
    p.OnHealthEvent([&p](const Event& e) {
        (void)p.IsAudioChannelOpened(); // health callbacks must run outside channel/crypto locks
        assert(e.attempt_id!=0);
        if (e.stage!=Stage::Control) assert(e.attempt_id==e.source_channel_token);
        else assert(e.source_channel_token==0);
        events.push_back(e);
    });
}
std::unique_ptr<AudioStreamPacket> audio() {
    auto p=std::make_unique<AudioStreamPacket>(); p->payload={1,2,3}; return p;
}
std::string authenticated_pong(std::string packet) {
    packet[0]=3;
    std::array<uint8_t,16> key; for (unsigned i=0;i<16;++i) key[i]=i;
    unsigned length=0; uint8_t digest[32];
    HMAC(EVP_sha256(),key.data(),16,reinterpret_cast<const uint8_t*>(packet.data()),16,digest,&length);
    memcpy(packet.data()+16,digest,16);
    return packet;
}
int main() {
    auto& net=Board::GetInstance().network;
    MqttProtocol p; watch(p);
    mqtt_notify_connected=false;
    assert(p.Start());
    assert(count(Stage::Control,Outcome::Started)==1 && count(Stage::Control,Outcome::Succeeded)==0);
    const auto control=events.back().attempt_id;
    auto old_connected=net.mqtt->connected, old_disconnected=net.mqtt->disconnected;
    auto old_control_message=net.mqtt->message;
    net.mqtt->connected();
    assert(events.back().stage==Stage::Control && events.back().attempt_id==control);
    assert(p.channel_token()==0);
    net.mqtt->disconnected();
    assert(events.back().stage==Stage::Control && events.back().outcome==Outcome::Failed);
    assert(events.back().attempt_id==control && p.channel_token()==0);
    assert(p.OpenAudioChannel());
    const auto first=p.channel_token();
    assert(count(Stage::Handshake,Outcome::Started)==1 && count(Stage::Handshake,Outcome::Succeeded)==1);
    assert(count(Stage::Media,Outcome::Started)==1 && !events.back().udp_probe);
    assert(count(Stage::Media,Outcome::Succeeded)==0);
    auto size=events.size(); net.mqtt->message("test",mqtt_hello); assert(events.size()==size);
    auto stale_audio=net.udp->message;
    udp_send_ok=false; assert(!p.SendAudio(audio())); udp_send_ok=true;
    assert(events.back().stage==Stage::Media && events.back().outcome==Outcome::Failed && events.back().attempt_id==first);
    crypto_ok=false; assert(!p.SendAudio(audio())); crypto_ok=true;
    assert(events.back().stage==Stage::Media && events.back().outcome==Outcome::Failed && events.back().attempt_id==first);
    net.mqtt->connected(); // control reconnect cannot produce media recovery
    assert(count(Stage::Media,Outcome::Succeeded)==0 && p.channel_token()==first);
    const auto before_close=count(Stage::Media,Outcome::Failed);
    p.CloseAudioChannel(false);
    assert(count(Stage::Media,Outcome::Failed)==before_close);
    assert(events.back().outcome==Outcome::Retired && events.back().attempt_id==first);

    mqtt_notify_connected=true;
    assert(p.Start());
    const auto current_control=events.back().attempt_id;
    assert(current_control!=control);
    size=events.size(); old_connected(); old_disconnected(); old_control_message("test",mqtt_hello);
    assert(events.size()==size);
    assert(p.OpenAudioChannel());
    const auto second=p.channel_token();
    // The send returns an error only after a new channel is established.
    int legacy_errors=0; p.OnNetworkError([&](const auto&) { ++legacy_errors; });
    net.udp->on_send=[&](const auto&) {
        p.CloseAudioChannel(false); assert(p.OpenAudioChannel()); udp_send_ok=false;
    };
    assert(!p.SendAudio(audio())); udp_send_ok=true;
    assert(events.back().outcome==Outcome::Failed && events.back().attempt_id==second);
    assert(events.back().source_channel_token!=p.channel_token() && legacy_errors==0);
    const auto no_probe_success=count(Stage::Media,Outcome::Succeeded);
    std::string bytes(19,'\0'); bytes[0]=1; bytes[3]=3; bytes[15]=1;
    stale_audio(bytes); net.udp->message(bytes);
    assert(count(Stage::Media,Outcome::Succeeded)==no_probe_success);
    p.CloseAudioChannel(false);

    for (bool creation : {true,false}) {
        create_udp_ok=creation; udp_connect_ok=false;
        const auto failures=count(Stage::Media,Outcome::Failed);
        assert(!p.OpenAudioChannel());
        assert(count(Stage::Media,Outcome::Failed)==failures+1);
        assert(events.back().attempt_id==p.channel_token());
    }
    create_udp_ok=udp_connect_ok=true;
    auto valid=mqtt_hello;
    mqtt_hello=R"({"type":"hello","transport":"udp","features":{"udp_probe":"bad"}})";
    const auto handshakes=count(Stage::Handshake,Outcome::Failed);
    assert(!p.OpenAudioChannel());
    assert(count(Stage::Handshake,Outcome::Failed)==handshakes+1);
    mqtt_hello=""; assert(!p.OpenAudioChannel()); // actual hello timeout
    const auto mqtt_timed_out_session=p.session_id();
    const auto mqtt_timed_out_rate=p.server_sample_rate();
    net.mqtt->message("test",valid);
    assert(p.session_id()==mqtt_timed_out_session && p.server_sample_rate()==mqtt_timed_out_rate);
    const auto canceled_failures=count(Stage::Handshake,Outcome::Failed);
    wait_hook=[&] { p.CloseAudioChannel(false); };
    assert(!p.OpenAudioChannel());
    assert(count(Stage::Handshake,Outcome::Failed)==canceled_failures);
    mqtt_hello=valid;
    mqtt_connect_ok=false; assert(!p.Start());
    assert(events.back().stage==Stage::Control && events.back().outcome==Outcome::Failed);
    assert(!p.OpenAudioChannel());
    assert(events.back().stage==Stage::Handshake && events.back().outcome==Outcome::Failed);
    mqtt_connect_ok=true;

    mqtt_hello=valid;
    mqtt_hello.insert(mqtt_hello.size()-1, R"(,"features":{"udp_probe":true})");
    clock_us=0;
    assert(p.OpenAudioChannel());
    const auto probed=p.channel_token();
    assert(events.back().stage==Stage::Media && events.back().outcome==Outcome::Started && events.back().udp_probe);
    assert(net.udp->sent.size()==1); // first PING never confirms media
    auto pong=authenticated_pong(net.udp->sent.back());
    auto old_pong_receiver=net.udp->message;
    auto bad=pong; bad[31]^=1; net.udp->message(bad);
    assert(count(Stage::Media,Outcome::Succeeded)==0);
    clock_us=50; net.udp->message(pong);
    assert(events.back().stage==Stage::Media && events.back().outcome==Outcome::Succeeded && events.back().attempt_id==probed);
    size=events.size(); net.udp->message(pong); assert(events.size()==size);
    p.CloseAudioChannel(false); assert(p.OpenAudioChannel());
    size=events.size(); old_pong_receiver(pong); assert(events.size()==size);
    const auto timeout_id=p.channel_token();
    const auto failed_before=count(Stage::Media,Outcome::Failed);
    clock_us=1000050; p.ProcessPending(); clock_us=3000050; p.ProcessPending();
    clock_us=5000050; p.ProcessPending(); p.ProcessPending();
    assert(count(Stage::Media,Outcome::Failed)==failed_before+1);
    assert(std::any_of(events.begin(),events.end(),[&](auto e) { return e.stage==Stage::Media && e.outcome==Outcome::Failed && e.attempt_id==timeout_id; }));
    assert(!p.IsAudioChannelOpened());
    sign_ok=false; const auto crypto_before=count(Stage::Media,Outcome::Failed);
    assert(!p.OpenAudioChannel()); sign_ok=true;
    assert(count(Stage::Media,Outcome::Failed)==crypto_before+1);
    udp_send_ok=false; const auto ping_before=count(Stage::Media,Outcome::Failed);
    assert(p.OpenAudioChannel()); udp_send_ok=true;
    assert(count(Stage::Media,Outcome::Failed)==ping_before+1);
    p.CloseAudioChannel(false);
    // An authenticated PONG received inside the very first Send cannot precede MediaStarted.
    udp_on_send=[&](const auto& packet) {
        assert(events.back().stage==Stage::Media && events.back().outcome==Outcome::Started);
        net.udp->message(authenticated_pong(packet));
    };
    assert(p.OpenAudioChannel());
    assert(events.back().outcome==Outcome::Succeeded && events.back().attempt_id==p.channel_token());
    udp_on_send=nullptr;
    p.CloseAudioChannel(false);
    assert(p.OpenAudioChannel());
    const auto probe_send_source=p.channel_token();
    const auto probe_legacy_errors=legacy_errors;
    net.udp->on_send=[&](const auto&) {
        p.CloseAudioChannel(false); assert(p.OpenAudioChannel()); udp_send_ok=false;
    };
    clock_us+=1000000; p.ProcessPending(); udp_send_ok=true;
    assert(events.back().outcome==Outcome::Failed && events.back().attempt_id==probe_send_source);
    assert(events.back().attempt_id!=p.channel_token() && legacy_errors==probe_legacy_errors);
    p.CloseAudioChannel(false);

    events.clear();
    WebsocketProtocol ws; watch(ws);
    assert(ws.OpenAudioChannel());
    const auto ws_first=ws.channel_token();
    assert(count(Stage::Handshake,Outcome::Started)==1 && count(Stage::Handshake,Outcome::Succeeded)==1);
    assert(count(Stage::Media,Outcome::Started)==1 && count(Stage::Media,Outcome::Succeeded)==0);
    auto ws_old_disconnected=net.ws->disconnected;
    auto ws_old_data=net.ws->data;
    net.ws->Deliver("audio",true); net.ws->Deliver(R"({"type":"stt","text":"hello"})");
    assert(count(Stage::Media,Outcome::Succeeded)==0);
    ws_send_ok=false; assert(!ws.SendAudio(audio())); ws_send_ok=true;
    assert(events.back().stage==Stage::Media && events.back().outcome==Outcome::Failed && events.back().attempt_id==ws_first);
    const auto ws_failures=count(Stage::Media,Outcome::Failed);
    ws.CloseAudioChannel(false); ws_old_disconnected();
    assert(count(Stage::Media,Outcome::Failed)==ws_failures);
    assert(ws.OpenAudioChannel());
    size=events.size(); ws_old_disconnected(); ws_old_data("audio",5,true); assert(events.size()==size);
    const auto ws_second=ws.channel_token();
    ws.OnNetworkError([&](const auto&) { ++legacy_errors; });
    net.ws->on_send=[&] { ws.CloseAudioChannel(false); assert(ws.OpenAudioChannel()); ws_send_ok=false; };
    assert(!ws.SendAudio(audio())); ws_send_ok=true;
    assert(events.back().outcome==Outcome::Failed && events.back().attempt_id==ws_second);
    const auto text_source=ws.channel_token();
    const auto text_errors=legacy_errors;
    net.ws->on_text_send=[&] { ws.CloseAudioChannel(false); assert(ws.OpenAudioChannel()); ws_send_ok=false; };
    ws.SendMcpMessage("{}"); ws_send_ok=true;
    assert(events.back().outcome==Outcome::Failed && events.back().attempt_id==text_source);
    assert(legacy_errors==text_errors && text_source!=ws.channel_token());
    const auto ws_current=ws.channel_token();
    net.ws->disconnected();
    assert(events.back().stage==Stage::Media && events.back().outcome==Outcome::Failed && events.back().attempt_id==ws_current);
    ws_connect_ok=false; assert(!ws.OpenAudioChannel()); ws_connect_ok=true;
    assert(events.back().stage==Stage::Handshake && events.back().outcome==Outcome::Failed);
    const auto connect_errors=legacy_errors;
    ws_connect_ok=false; ws_disconnect_during_connect=true;
    assert(!ws.OpenAudioChannel()); ws_connect_ok=true; ws_disconnect_during_connect=false;
    assert(legacy_errors==connect_errors+1); // the current failing source retains the legacy error
    auto ws_valid=ws_hello; ws_hello=""; assert(!ws.OpenAudioChannel());
    assert(events.back().stage==Stage::Handshake && events.back().outcome==Outcome::Failed);
    const auto ws_timed_out_session=ws.session_id();
    const auto ws_timed_out_rate=ws.server_sample_rate();
    size=events.size();
    net.ws->Deliver(R"({"type":"hello","transport":"websocket","session_id":"late-after-timeout","audio_params":{"sample_rate":8000}})");
    assert(events.size()==size); // a late hello cannot mutate validity/session/audio state either
    assert(ws.session_id()==ws_timed_out_session && ws.server_sample_rate()==ws_timed_out_rate);
    const auto ws_cancel_failures=count(Stage::Handshake,Outcome::Failed);
    wait_hook=[&] { ws.CloseAudioChannel(false); };
    assert(!ws.OpenAudioChannel());
    assert(count(Stage::Handshake,Outcome::Failed)==ws_cancel_failures);
    ws_hello=R"({"type":"hello","transport":"invalid"})"; assert(!ws.OpenAudioChannel());
    assert(events.back().stage==Stage::Handshake && events.back().outcome==Outcome::Failed);
    ws_hello=ws_valid;
    create_ws_ok=false; assert(!ws.OpenAudioChannel()); create_ws_ok=true;
    assert(events.back().stage==Stage::Handshake && events.back().outcome==Outcome::Failed);
    // Optional API: existing behavior remains usable with no health consumer.
    ws.OnHealthEvent(nullptr); assert(ws.OpenAudioChannel()); ws.CloseAudioChannel(false);
    ws_disconnect_on_destroy=true;
    const auto destruction_failures=count(Stage::Media,Outcome::Failed);
    { WebsocketProtocol disappearing; watch(disappearing); assert(disappearing.OpenAudioChannel()); }
    assert(count(Stage::Media,Outcome::Failed)==destruction_failures);
}
''', ["protocol.cc", "mqtt_protocol.cc", "websocket_protocol.cc"],
            TRANSPORT_STUBS, ["-lcrypto"])


if __name__ == "__main__":
    unittest.main()
