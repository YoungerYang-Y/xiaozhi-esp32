"""Exercise production channel callbacks with host transport/platform adapters."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ProtocolChannelTokenTest(unittest.TestCase):
    def test_websocket_rejects_callbacks_from_closed_connection(self):
        compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not compiler or not shutil.which(compiler[0]):
            self.skipTest("requires a host C++ compiler")
        flags = shlex.split(os.environ.get("MCP_CJSON_CFLAGS", "-I/usr/include/cjson"))
        libs = shlex.split(os.environ.get("MCP_CJSON_LIBS", "-lcjson"))
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory)
            stubs = {
                "esp_log.h": "#define ESP_LOGE(...)\n#define ESP_LOGI(...)\n#define ESP_LOGW(...)\n",
                "freertos/FreeRTOS.h": "#pragma once\n#define pdTRUE 1\n#define pdFALSE 0\n#define pdMS_TO_TICKS(x) (x)\n",
                "freertos/event_groups.h": r'''
#pragma once
using EventBits_t = unsigned;
using EventGroupHandle_t = unsigned*;
inline EventGroupHandle_t xEventGroupCreate() { return new unsigned(0); }
inline void vEventGroupDelete(EventGroupHandle_t e) { delete e; }
inline void xEventGroupSetBits(EventGroupHandle_t e, unsigned b) { *e |= b; }
inline void xEventGroupClearBits(EventGroupHandle_t e, unsigned b) { *e &= ~b; }
inline unsigned xEventGroupWaitBits(EventGroupHandle_t e, unsigned b, int clear, int, int) {
    const unsigned result = *e & b;
    if (clear) *e &= ~b;
    return result;
}
''',
                "web_socket.h": r'''
#pragma once
#include <functional>
#include <memory>
#include <string>
struct Result {
    explicit operator bool() const { return true; }
    Result error() const { return {}; }
    std::string ToString() const { return "test"; }
};
struct WebSocket {
    std::function<void(const char*, size_t, bool)> data;
    std::function<void()> disconnected;
    bool connected = true;
    void SetHeader(const char*, const char*) {}
    void OnData(decltype(data) callback) { data = std::move(callback); }
    void OnPing(std::function<void(const char*, size_t)>) {}
    void OnDisconnected(std::function<void()> callback) { disconnected = std::move(callback); }
    bool IsConnected() const { return connected; }
    Result Connect(const char*) { return {}; }
    bool Send(const std::string&) {
        Deliver(R"({"type":"hello","transport":"websocket","session_id":"current"})");
        return true;
    }
    bool Send(const void*, size_t, bool) { return true; }
    void SendPong(const char*, size_t) {}
    void Deliver(const std::string& text, bool binary = false) { data(text.data(), text.size(), binary); }
};
''',
                "application.h": r'''
#pragma once
struct Application {
    static Application& GetInstance() { static Application app; return app; }
    template<class F> void Schedule(F f) { f(); }
};
''',
                "board.h": r'''
#pragma once
#include "web_socket.h"
struct Network {
    WebSocket* last = nullptr;
    std::unique_ptr<WebSocket> CreateWebSocket(int) {
        auto websocket = std::make_unique<WebSocket>();
        last = websocket.get();
        return websocket;
    }
};
struct Board {
    Network network;
    static Board& GetInstance() { static Board board; return board; }
    Network* GetNetwork() { return &network; }
    std::string GetUuid() { return "test"; }
};
''',
                "settings.h": r'''
#pragma once
#include <string>
inline int websocket_version = 1;
struct Settings {
    Settings(const char*, bool) {}
    std::string GetString(const char*) { return "test"; }
    int GetInt(const char*) { return websocket_version; }
};
''',
                "system_info.h": r'''
#pragma once
struct SystemInfo { static std::string GetMacAddress() { return "test"; } };
''',
                "assets.h": r'''
#pragma once
#include <string>
struct Assets {
    struct Capability { bool glyph_push = false; std::string bundle, charset; int size = 0, bpp = 0; };
    static Assets& GetInstance() { static Assets assets; return assets; }
    Capability text_font_capability() { return {}; }
};
''',
                "assets/lang_config.h": r'''
#pragma once
#define OPUS_FRAME_DURATION_MS 60
namespace Lang { namespace Strings {
constexpr const char* SERVER_ERROR = "error";
constexpr const char* SERVER_NOT_CONNECTED = "not connected";
constexpr const char* SERVER_TIMEOUT = "timeout";
} }
''',
            }
            for name, content in stubs.items():
                path = build / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            source = r'''
#include <cassert>
#include <arpa/inet.h>
#include <cstring>
#include <memory>
#include "main/protocols/websocket_protocol.h"
#include "board.h"
#include "settings.h"
int main() {
    WebsocketProtocol protocol;
    int json = 0, audio = 0, closed = 0;
    Protocol::ChannelToken event_token = 0, closed_token = 0, opened_token = 0;
    std::vector<uint8_t> audio_payload;
    uint32_t audio_timestamp = 0;
    protocol.OnIncomingJson([&](const cJSON*, auto token) {
        assert(protocol.IsAudioChannelOpened()); // callbacks must not hold the channel lock
        event_token = token;
        ++json;
    });
    protocol.OnIncomingAudio([&](auto packet, auto token) {
        assert(protocol.IsAudioChannelOpened());
        event_token = token;
        audio_payload = packet->payload;
        audio_timestamp = packet->timestamp;
        ++audio;
    });
    protocol.OnAudioChannelClosed([&](auto token) {
        (void)protocol.IsAudioChannelOpened();
        closed_token = token;
        ++closed;
    });
    protocol.OnAudioChannelOpened([&](auto token) { opened_token = token; });
    assert(protocol.OpenAudioChannel());
    const auto first_token = protocol.channel_token();
    assert(opened_token == first_token && first_token != 0);
    auto old = Board::GetInstance().network.last;
    old->Deliver(R"({"type":"tts","state":"start"})");
    auto old_data = old->data;
    auto old_disconnected = old->disconnected;
    auto deliver_old = [&](const std::string& data, bool binary = false) {
        old_data(data.data(), data.size(), binary);
    };
    assert(json == 1 && event_token == first_token);
    protocol.CloseAudioChannel(false);
    assert(closed_token == protocol.channel_token() && closed_token != first_token && closed == 1);
    assert(protocol.OpenAudioChannel());
    assert(opened_token == protocol.channel_token() && opened_token != first_token);
    assert(closed_token != protocol.channel_token()); // queued old close is now stale
    auto current = Board::GetInstance().network.last;
    const int closed_before_old_callback = closed;
    deliver_old(R"({"type":"hello","transport":"websocket","session_id":"old"})");
    deliver_old(R"({"type":"tts","state":"stop"})");
    deliver_old("audio", true);
    old_disconnected();
    assert(protocol.session_id() == "current");
    assert(json == 1 && audio == 0 && closed == closed_before_old_callback);
    current->Deliver(R"({"type":"tts","state":"start"})");
    current->Deliver("audio", true);
    assert(json == 2 && audio == 1 && event_token == opened_token);
    current->connected = false;
    current->disconnected();
    assert(closed == 2 && closed_token == protocol.channel_token());
    current->disconnected(); // duplicate disconnect from the now-invalid source is ignored
    assert(closed == 2);

    for (const int version : {2, 3}) {
        websocket_version = version;
        assert(protocol.OpenAudioChannel());
        current = Board::GetInstance().network.last;
        std::string bytes;
        if (version == 2) {
            BinaryProtocol2 header{};
            header.version = htons(2);
            header.timestamp = htonl(123);
            header.payload_size = htonl(3);
            bytes.assign(reinterpret_cast<const char*>(&header), sizeof(header));
        } else {
            BinaryProtocol3 header{};
            header.payload_size = htons(3);
            bytes.assign(reinterpret_cast<const char*>(&header), sizeof(header));
        }
        bytes += "abc";
        const auto original = bytes;
        current->Deliver(bytes, true);
        assert(bytes == original && audio_payload == std::vector<uint8_t>({'a', 'b', 'c'}));
        assert(audio_timestamp == (version == 2 ? 123 : 0));
        const auto valid_audio = audio;
        current->Deliver(bytes.substr(0, 2), true); // truncated header
        current->Deliver(bytes.substr(0, bytes.size() - 1), true); // truncated payload
        assert(audio == valid_audio);
        protocol.CloseAudioChannel(false);
    }
    WebsocketProtocol replacement;
    assert(replacement.OpenAudioChannel());
    assert(replacement.channel_token() != first_token);
    assert(replacement.channel_token() != protocol.channel_token());
}
'''
            (build / "test.cc").write_text(source)
            result = subprocess.run(
                compiler + ["-std=c++20", "-pthread", "-Wall", "-Wextra", "-Werror",
                            "-Wno-unused-parameter", f"-I{build}", f"-I{ROOT}", *flags,
                            str(build / "test.cc"), str(ROOT / "main/protocols/protocol.cc"),
                            str(ROOT / "main/protocols/websocket_protocol.cc"), *libs,
                            "-o", str(build / "test")], capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(build / "test")], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_mqtt_session_filter_and_queued_goodbye_generation(self):
        compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not compiler or not shutil.which(compiler[0]):
            self.skipTest("requires a host C++ compiler")
        flags = shlex.split(os.environ.get("MCP_CJSON_CFLAGS", "-I/usr/include/cjson"))
        libs = shlex.split(os.environ.get("MCP_CJSON_LIBS", "-lcjson"))
        protocol = (ROOT / "main/protocols/protocol.cc").read_text()
        protocol = protocol[protocol.index("void Protocol::OnIncomingJson"):]
        mqtt = (ROOT / "main/protocols/mqtt_protocol.cc").read_text()
        start = mqtt.index("    mqtt_->OnMessage(")
        message = mqtt[start:mqtt.index("\n    std::string broker_address;", start)]
        start = mqtt.index("void MqttProtocol::CloseAudioChannel(")
        close = mqtt[start:mqtt.index("bool MqttProtocol::OpenAudioChannel()", start)]
        source = r'''
#include "main/protocols/protocol.h"
#include <cassert>
#include <cstring>
#include <optional>
#define ESP_LOGE(...)
#define ESP_LOGI(...)
#define ESP_LOGD(...)
struct Timer {};
void esp_timer_stop(Timer*) {}
struct Udp {};
struct Mqtt {
    std::function<void(const std::string&, const std::string&)> receive;
    void OnMessage(decltype(receive) callback) { receive = std::move(callback); }
};
struct Application {
    std::vector<std::function<void()>> pending;
    static Application& GetInstance() { static Application app; return app; }
    void Schedule(std::function<void()> callback) { pending.push_back(std::move(callback)); }
    void Drain() {
        auto callbacks = std::move(pending);
        pending.clear();
        for (auto& callback : callbacks) callback();
    }
};
struct MqttProtocol : Protocol {
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    mutable std::mutex channel_mutex_;
    std::atomic<bool> awaiting_hello_{false}, hello_valid_{false};
    uint64_t channel_generation_ = 0;
    std::atomic<uint64_t> control_attempt_{1};
    ChannelToken health_channel_token_ = 0;
    bool media_started_ = false;
    std::shared_ptr<Udp> udp_;
    std::shared_ptr<int> probe_channel_;
    Timer* probe_timer_ = nullptr;
    std::unique_ptr<Mqtt> mqtt_ = std::make_unique<Mqtt>();
    bool Start() override { return true; }
    bool OpenAudioChannel() override {
        std::lock_guard<std::mutex> lock(channel_mutex_);
        AdvanceChannelToken();
        ++channel_generation_;
        SetSessionId("current");
        udp_ = std::make_shared<Udp>();
        return true;
    }
    bool IsAudioChannelOpened() const override {
        std::lock_guard<std::mutex> lock(channel_mutex_);
        return udp_ != nullptr;
    }
    bool SendAudio(std::unique_ptr<AudioStreamPacket>) override { return true; }
    bool SendText(const std::string&) override { return true; }
    void ParseServerHello(const cJSON*, ChannelToken, uint64_t) {}
    void CloseAudioChannel(bool send_goodbye = true) override;
    void CloseAudioChannelForGeneration(bool, std::optional<uint64_t>);
    void InstallReceiver();
};
''' + protocol + close + "\nvoid MqttProtocol::InstallReceiver() {\nconst auto attempt = control_attempt_.load();\n" + message + r'''
}
int main() {
    MqttProtocol protocol;
    protocol.InstallReceiver();
    protocol.OpenAudioChannel();
    const auto first = protocol.channel_token();
    std::vector<Protocol::ChannelToken> received;
    int closed = 0;
    protocol.OnIncomingJson([&](const cJSON*, auto token) {
        assert(protocol.IsAudioChannelOpened());
        received.push_back(token);
    });
    protocol.OnAudioChannelClosed([&](auto token) {
        assert(token == protocol.channel_token() && !protocol.IsAudioChannelOpened());
        ++closed;
    });
    const auto deliver = [&](const char* json) { protocol.mqtt_->receive("test", json); };
    deliver(R"({"type":"tts","session_id":"stale","state":"start"})");
    deliver(R"({"type":"stt","session_id":"stale","text":"old"})");
    deliver(R"({"type":"llm","session_id":"stale","emotion":"sad"})");
    const size_t legacy_messages = CONFIG_WAVESHARE_1_85C_HOME ? 0 : 3;
    assert(received.size() == legacy_messages);
    deliver(R"({"type":"tts","session_id":"current","state":"start"})");
    deliver(R"({"type":"tts","state":"start"})");
    deliver(R"({"type":"tts","session_id":"","state":"start"})");
    deliver(R"({"type":"mcp","payload":{}})");
    deliver(R"({"type":"system","command":"reboot"})");
    deliver(R"({"type":"notify","audio_url":"test"})");
    deliver(R"({"type":"mcp","session_id":"stale","payload":{}})");
    assert(received.size() == legacy_messages + 7);
    for (const auto token : received) assert(token == first);

    deliver(R"({"type":"goodbye","session_id":"current"})");
    assert(Application::GetInstance().pending.size() == 1);
    protocol.CloseAudioChannel(false);
    protocol.OpenAudioChannel();
    const auto reopened = protocol.channel_token();
    Application::GetInstance().Drain();
    assert(protocol.IsAudioChannelOpened() && protocol.channel_token() == reopened && closed == 1);
    deliver(R"({"type":"goodbye","session_id":"stale"})");
    deliver(R"({"type":"goodbye"})");
    assert(Application::GetInstance().pending.empty());
    deliver(R"({"type":"tts","session_id":"current","state":"stop"})");
    assert(received.back() == reopened);
    deliver(R"({"type":"goodbye","session_id":"current"})");
    Application::GetInstance().Drain();
    assert(!protocol.IsAudioChannelOpened() && closed == 2 && protocol.channel_token() != reopened);
}
'''
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory)
            (build / "test.cc").write_text(source)
            for home in (False, True):
                with self.subTest(home=home):
                    binary = build / ("home" if home else "legacy")
                    result = subprocess.run(
                        compiler + ["-std=c++20", "-pthread", "-Wall", "-Wextra", "-Werror",
                                    "-Wno-unused-parameter", f"-I{ROOT}", *flags,
                                    f"-DCONFIG_WAVESHARE_1_85C_HOME={int(home)}",
                                    str(build / "test.cc"), *libs, "-o", str(binary)],
                        capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    result = subprocess.run([str(binary)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
