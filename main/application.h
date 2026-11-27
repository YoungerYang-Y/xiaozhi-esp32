#ifndef _APPLICATION_H_
#define _APPLICATION_H_

#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <freertos/task.h>

#include <atomic>
#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "audio_service.h"
#include "connection_health.h"
#include "device_state.h"
#include "device_state_machine.h"
#include "low_battery_reminder.h"
#include "notify/notify_player.h"
#include "ota.h"
#include "protocol.h"
#include "waiting_response.h"

// Main event bits
#define MAIN_EVENT_SCHEDULE (1 << 0)
#define MAIN_EVENT_SEND_AUDIO (1 << 1)
#define MAIN_EVENT_WAKE_WORD_DETECTED (1 << 2)
#define MAIN_EVENT_VAD_CHANGE (1 << 3)
#define MAIN_EVENT_ERROR (1 << 4)
#define MAIN_EVENT_ACTIVATION_DONE (1 << 5)
#define MAIN_EVENT_CLOCK_TICK (1 << 6)
#define MAIN_EVENT_NETWORK_CONNECTED (1 << 7)
#define MAIN_EVENT_NETWORK_DISCONNECTED (1 << 8)
#define MAIN_EVENT_TOGGLE_CHAT (1 << 9)
#define MAIN_EVENT_START_LISTENING (1 << 10)
#define MAIN_EVENT_STOP_LISTENING (1 << 11)
#define MAIN_EVENT_STATE_CHANGED (1 << 12)
#define MAIN_EVENT_PLAYBACK_DRAINED (1 << 13)
#define MAIN_EVENT_STARTUP_HINT_TIMEOUT (1 << 14)
#define MAIN_EVENT_PROTOCOL_PENDING (1 << 15)
#define MAIN_EVENT_PRIMARY_BUTTON (1 << 16)
#define MAIN_EVENT_LISTENING_PAUSE (1 << 17)

enum AecMode {
    kAecOff,
    kAecOnDeviceSide,
    kAecOnServerSide,
};

class Application {
public:
    static Application& GetInstance() {
        static Application instance;
        return instance;
    }
    // Delete copy constructor and assignment operator
    Application(const Application&) = delete;
    Application& operator=(const Application&) = delete;

    /**
     * Initialize the application
     * This sets up display, audio, network callbacks, etc.
     * Network connection starts asynchronously.
     */
    void Initialize();

    /**
     * Run the main event loop
     * This function runs in the main task and never returns.
     * It handles all events including network, state changes, and user interactions.
     */
    void Run();

    DeviceState GetDeviceState() const { return state_machine_.GetState(); }
    bool IsVoiceDetected() const { return audio_service_.IsVoiceDetected(); }

    /**
     * Request state transition
     * Returns true if transition was successful
     */
    bool SetDeviceState(DeviceState state);

    /**
     * Schedule a callback to be executed in the main task
     */
    void Schedule(std::function<void()>&& callback);
    void NotifyProtocolPending() { xEventGroupSetBits(event_group_, MAIN_EVENT_PROTOCOL_PENDING); }

    /**
     * Alert with status, message, emotion and optional sound
     */
    void Alert(const char* status, const char* message, const char* emotion = "",
               const std::string_view& sound = "");
    void DismissAlert();

    void AbortSpeaking(AbortReason reason);

    /**
     * Toggle chat state (event-based, thread-safe)
     * Sends MAIN_EVENT_TOGGLE_CHAT to be handled in Run()
     */
    void ToggleChatState();
    void PrimaryButtonPressed();
    void ToggleListeningPause();
    bool IsListeningPaused() const;
    bool HasConnectionAlert() const {
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
        return connection_alert_active_.load();
#else
        return false;
#endif
    }
    const char* GetServiceStateName() const {
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
        if (!wifi_link_connected_.load()) {
            return "unknown";
        }
        switch (connection_service_state_.load()) {
            case ConnectionHealth::ServiceState::Available:
                return "available";
            case ConnectionHealth::ServiceState::Unavailable:
                return "unavailable";
            default:
                return "unknown";
        }
#else
        return "unknown";
#endif
    }

    /**
     * Start listening (event-based, thread-safe)
     * Sends MAIN_EVENT_START_LISTENING to be handled in Run()
     */
    void StartListening();

    /**
     * Stop listening (event-based, thread-safe)
     * Sends MAIN_EVENT_STOP_LISTENING to be handled in Run()
     */
    void StopListening();

    void Reboot();
    void WakeWordInvoke(const std::string& wake_word);
    bool UpgradeFirmware(const std::string& url, const std::string& version = "");
    bool CanEnterSleepMode();
    void SendMcpMessage(const std::string& payload);
    void RegisterMcpBroadcastCallback(std::function<void(const std::string&)> callback);
    void SetAecMode(AecMode mode);
    AecMode GetAecMode() const { return aec_mode_; }
    void PlaySound(const std::string_view& sound);
    AudioService& GetAudioService() { return audio_service_; }

    /**
     * Reset protocol resources (thread-safe)
     * Can be called from any task to release resources allocated after network connected
     * This includes closing audio channel, resetting protocol and ota objects
     */
    void ResetProtocol();

private:
    Application();
    ~Application();

    std::mutex mutex_;
    std::deque<std::function<void()>> main_tasks_;
    std::unique_ptr<Protocol> protocol_;
    EventGroupHandle_t event_group_ = nullptr;
    esp_timer_handle_t clock_timer_handle_ = nullptr;
    esp_timer_handle_t startup_wake_word_hint_timer_ = nullptr;
    DeviceStateMachine state_machine_;
    ListeningMode listening_mode_ = kListeningModeAutoStop;
    AecMode aec_mode_ = kAecOff;
    std::string last_error_message_;
    std::string startup_wake_word_hint_message_;
    AudioService audio_service_;
    NotifyPlayer notify_player_;
    uint32_t notification_playback_id_ = 0;
    std::unique_ptr<Ota> ota_;

    std::function<void(const std::string&)> mcp_broadcast_callback_;

    bool has_server_time_ = false;
    bool startup_completed_ = false;
    bool pending_startup_wake_word_hint_ = false;
    bool aborted_ = false;
    bool assets_version_checked_ = false;
    bool play_popup_on_listening_ =
        false;  // Flag to play popup sound after state changes to listening
    bool pending_listening_start_ =
        false;  // Waiting for playback to drain before starting listening (auto mode)
    int clock_ticks_ = 0;
    TaskHandle_t activation_task_handle_ = nullptr;

#if CONFIG_WAVESHARE_1_85C_HOME
    std::atomic<uint32_t> pause_requests_{0};
    std::atomic<uint32_t> primary_button_sequence_{0};
    std::atomic<bool> listening_paused_{false};
    std::atomic<bool> conversation_events_enabled_{false};
    std::atomic<uint32_t> conversation_intent_{0};
    std::atomic<Protocol::ChannelToken> accepted_channel_token_{0};
    std::atomic<uint32_t> accepted_channel_intent_{0};
    std::atomic<uint32_t> protocol_callback_epoch_{0};
    uint32_t pending_error_intent_ = 0;
    uint32_t pending_error_pause_sequence_ = 0;
    uint32_t pending_error_callback_epoch_ = 0;
    std::atomic<uint32_t> processed_pause_requests_{0};
    std::mutex audio_receive_mutex_;
    std::atomic<uint64_t> waiting_turn_{0};
    std::atomic_bool waiting_turn_open_{false};
    uint64_t waiting_response_turn_ = 0;
    WaitingResponse waiting_response_;
#endif

#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS
    enum class LocalPromptKind { None, LowBattery, Network };
    struct LocalPromptContext {
        uint32_t id = 0;
        LocalPromptKind kind = LocalPromptKind::None;
        ConnectionHealth::PromptTicket network_ticket;
        uint64_t low_episode = 0;
        bool started = false;
        bool cancelled = false;
        bool resume_listening = false;
        uint32_t intent = 0;
        uint32_t pause_sequence = 0;
        Protocol::ChannelToken channel = 0;
    };
    ConnectionHealth connection_health_;
    LowBatteryReminder low_battery_reminder_;
    LocalPromptContext local_prompt_;
    uint32_t playback_id_sequence_ = 0;
    uint64_t local_prompt_retry_after_ms_ = 0;
    bool local_prompt_gap_allowed_ = false;
    std::atomic_bool wifi_link_connected_{false};
    std::atomic<uint32_t> wifi_link_epoch_{0};
    std::atomic<uint32_t> wifi_disconnect_sequence_{0};
    uint32_t processed_wifi_disconnect_sequence_ = 0;
    std::atomic_bool connection_alert_active_{false};
    std::atomic_bool pending_connection_failure_{false};
    std::atomic_bool activation_guidance_pending_{false};
    std::atomic<ConnectionHealth::ServiceState> connection_service_state_{
        ConnectionHealth::ServiceState::Unknown};
    std::atomic<uint64_t> configuration_attempt_sequence_{0};
    Protocol::ChannelToken handshake_source_token_ = 0;
    uint64_t media_attempt_id_ = 0;
    Protocol::ChannelToken media_source_token_ = 0;
    bool media_uses_probe_ = false;
    std::atomic<uint64_t> media_speech_sequence_{0};
    std::atomic<Protocol::ChannelToken> media_speech_token_{0};
    uint64_t media_stt_sequence_ = 0;
    std::atomic<uint64_t> media_decode_request_{0};
    std::atomic<Protocol::ChannelToken> media_decode_token_{0};
    std::string connection_display_message_;

    void HandleConnectionWifiEvent(bool connected);
    void HandleProtocolHealthEvent(const Protocol::HealthEvent& event);
    void RefreshConnectionStatus();
    void RetireConversationHealth();
    void EndConversationForConnectionFailure();
    void RecordConfigurationResult(ConnectionHealth::Stage stage, uint64_t identity,
                                   uint32_t link_epoch, bool success);
    bool PrepareConnectionOpen();
    void RecordMediaRecognition(Protocol::ChannelToken token, uint64_t speech, bool nonempty);
    void HandleDecodedMedia(Protocol::ChannelToken token, uint64_t speech);
    void TickLocalPrompts();
    bool TryStartPendingLocalPrompt(bool before_listening);
    void CancelLocalPrompt(bool resume_after_cancel = false);
    void HandleLocalSoundStarted(uint32_t id);
    void HandleLocalSoundFinished(uint32_t id, bool completed);
    uint32_t AllocatePlaybackId();
#endif

    // Event handlers
    void HandleStateChangedEvent();
    void HandleToggleChatEvent();
    void HandlePrimaryButtonEvent();
    bool ApplyPendingListeningPause();
    bool IsConversationEventCurrent(Protocol::ChannelToken token, uint32_t pause_sequence) const;
    uint32_t ListeningPauseSequence() const;
    uint32_t ConversationIntentSequence() const;
    bool AcceptConversationChannel();
    void HandleStartListeningEvent();
    void HandleStopListeningEvent();
    void HandleNetworkConnectedEvent();
    void HandleNetworkDisconnectedEvent();
    void HandleActivationDoneEvent();
    void HandleWakeWordDetectedEvent();
    void ContinueOpenAudioChannel(ListeningMode mode);
    void BeginWakeWordInvoke(const std::string& wake_word);
    void ContinueWakeWordInvoke(const std::string& wake_word);
    void StartListeningAudio();
    void ConfigureWakeWordForListening();
    void BeginWaitingResponseTurn();
    void EndWaitingResponseTurn();
    void HandleWaitingSpeech(uint64_t turn, Protocol::ChannelToken token, uint32_t pause_sequence);
    void HandleWaitingRecognition(uint64_t turn, bool nonempty);
    void UpdateWaitingResponse();
    void ShowStartupWakeWordHint();
    void HideStartupWakeWordHint();
    void StartNotification(std::string audio_url, std::vector<NotifySubtitle> subtitles);
    void StopNotification();
    void HandleNotificationFinished(uint32_t playback_id, bool success);

    // Activation task (runs in background)
    void ActivationTask();

    // Helper methods
    void CheckAssetsVersion();
    void CheckNewVersion();
    void InitializeProtocol();
    void ShowActivationCode(const std::string& code, const std::string& message);
    void SetListeningMode(ListeningMode mode);
    ListeningMode GetDefaultListeningMode() const;

    // State change handler called by state machine
    void OnStateChanged(DeviceState old_state, DeviceState new_state);
};

class TaskPriorityReset {
public:
    TaskPriorityReset(BaseType_t priority) {
        original_priority_ = uxTaskPriorityGet(NULL);
        vTaskPrioritySet(NULL, priority);
    }
    ~TaskPriorityReset() { vTaskPrioritySet(NULL, original_priority_); }

private:
    BaseType_t original_priority_;
};

#endif  // _APPLICATION_H_
