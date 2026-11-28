#ifndef PROTOCOL_H
#define PROTOCOL_H

#include <cJSON.h>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

struct AudioStreamPacket {
    int sample_rate = 0;
    int frame_duration = 0;
    uint32_t timestamp = 0;
    uint32_t playback_id = 0;
    uint32_t media_position_ms = 0;
    // Device-local provenance for successful decoding; never serialized.
    uint64_t source_channel_token = 0;
    // Consumer-owned request identity captured with the received packet.
    uint64_t source_request_id = 0;
    std::vector<uint8_t> payload;
};

struct BinaryProtocol2 {
    uint16_t version;
    uint16_t type;          // Message type (0: OPUS, 1: JSON)
    uint32_t reserved;      // Reserved for future use
    uint32_t timestamp;     // Timestamp in milliseconds (used for server-side AEC)
    uint32_t payload_size;  // Payload size in bytes
    uint8_t payload[];      // Payload data
} __attribute__((packed));

struct BinaryProtocol3 {
    uint8_t type;
    uint8_t reserved;
    uint16_t payload_size;
    uint8_t payload[];
} __attribute__((packed));

enum AbortReason { kAbortReasonNone, kAbortReasonWakeWordDetected };

enum ListeningMode {
    kListeningModeAutoStop,
    kListeningModeManualStop,
    kListeningModeRealtime  // 需要 AEC 支持
};

class Protocol {
public:
    using ChannelToken = uint64_t;

    // Device-local evidence only; never serialized or inferred from socket readiness.
    enum class HealthStage { Control, Handshake, Media };
    enum class HealthOutcome { Started, Failed, Succeeded, Retired };
    struct HealthEvent {
        HealthStage stage;
        HealthOutcome outcome;
        uint64_t attempt_id;                // Nonzero and stable for this stage's source.
        ChannelToken source_channel_token;  // Zero for the independent MQTT control source.
        bool udp_probe = false;             // Negotiated mode, meaningful for Media Started.
    };

    virtual ~Protocol() = default;

    // Shared device capability source for transport hello and OTA protocol.features.
    static void AddClientCapabilities(cJSON* root, bool include_text_font = true);

    inline int server_sample_rate() const { return server_sample_rate_; }
    inline int server_frame_duration() const { return server_frame_duration_; }
    std::string session_id() const;
    ChannelToken channel_token() const { return channel_token_.load(); }

    void OnIncomingAudio(
        std::function<void(std::unique_ptr<AudioStreamPacket> packet, ChannelToken)> callback);
    void OnIncomingJson(std::function<void(const cJSON* root, ChannelToken)> callback);
    void OnAudioChannelOpened(std::function<void(ChannelToken)> callback);
    void OnAudioChannelClosed(std::function<void(ChannelToken)> callback);
    void OnNetworkError(std::function<void(const std::string& message)> callback);
    void OnConnected(std::function<void()> callback);
    void OnDisconnected(std::function<void()> callback);
    // Register before Start/Open. Callbacks run on the source task, outside transport
    // locks; application consumers must schedule mutations on their own task.
    void OnHealthEvent(std::function<void(const HealthEvent&)> callback);

    // Set before Start/Open on a dedicated recovery instance. No application
    // work or automatic reconnect may escape this instance's worker lifetime.
    void SetRecoveryProbe(bool enabled = true) { recovery_probe_ = enabled; }

    virtual bool Start() = 0;
    virtual bool OpenAudioChannel() = 0;
    virtual void CloseAudioChannel(bool send_goodbye = true) = 0;
    virtual bool IsAudioChannelOpened() const = 0;
    virtual bool SendAudio(std::unique_ptr<AudioStreamPacket> packet) = 0;
    // Called in the existing application/network-send context, never a timer callback.
    virtual void ProcessPending() {}
    virtual void SendWakeWordDetected(const std::string& wake_word);
    virtual void SendStartListening(ListeningMode mode);
    virtual void SendStopListening();
    virtual void SendAbortSpeaking(AbortReason reason);
    virtual void SendMcpMessage(const std::string& message);

protected:
    bool recovery_probe_ = false;
    std::function<void(const cJSON* root, ChannelToken)> on_incoming_json_;
    std::function<void(std::unique_ptr<AudioStreamPacket> packet, ChannelToken)> on_incoming_audio_;
    std::function<void(ChannelToken)> on_audio_channel_opened_;
    std::function<void(ChannelToken)> on_audio_channel_closed_;
    std::function<void(const std::string& message)> on_network_error_;
    std::function<void()> on_connected_;
    std::function<void()> on_disconnected_;
    std::function<void(const HealthEvent&)> on_health_event_;

    void EmitHealthEvent(const HealthEvent& event);

    int server_sample_rate_ = 24000;
    int server_frame_duration_ = 60;
    bool error_occurred_ = false;
    std::chrono::time_point<std::chrono::steady_clock> last_incoming_time_;

    // Transports serialize advancement with their channel state. Tokens remain
    // unique across protocol replacement so old application work cannot match a
    // new instance. A close publishes its token before tearing down the source.
    ChannelToken AdvanceChannelToken() {
        const auto token = next_channel_token_.fetch_add(1) + 1;
        channel_token_.store(token);
        return token;
    }
    void SetSessionId(const std::string& session_id);
    virtual bool SendText(const std::string& text) = 0;
    virtual void SetError(const std::string& message);
    void SetError(const std::string& message, const std::string& detail);
    virtual bool IsTimeout() const;

private:
    inline static std::atomic<ChannelToken> next_channel_token_{0};
    std::atomic<ChannelToken> channel_token_{0};
    mutable std::mutex session_mutex_;
    std::string session_id_;
};

#endif  // PROTOCOL_H
