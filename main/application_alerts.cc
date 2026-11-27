#include "application.h"

#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS

#include "assets/lang_config.h"
#include "audio_codec.h"
#include "board.h"
#include "display.h"

#include <esp_log.h>

namespace {
constexpr char kTag[] = "LocalAlerts";
uint64_t NowMs() { return static_cast<uint64_t>(esp_timer_get_time() / 1000); }
}  // namespace

uint32_t Application::AllocatePlaybackId() {
    if (++playback_id_sequence_ == 0) {
        ++playback_id_sequence_;
    }
    return playback_id_sequence_;
}

void Application::RetireConversationHealth() {
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
    connection_health_.Retire(ConnectionHealth::Stage::Handshake);
    connection_health_.Retire(ConnectionHealth::Stage::Media);
    handshake_source_token_ = 0;
    media_attempt_id_ = 0;
    media_source_token_ = 0;
    media_uses_probe_ = false;
    media_speech_token_.store(0);
    media_decode_request_.store(0);
    media_decode_token_.store(0);
    media_stt_sequence_ = 0;
#endif
}

void Application::EndConversationForConnectionFailure() {
    auto state = GetDeviceState();
    if (state != kDeviceStateConnecting && state != kDeviceStateListening &&
        state != kDeviceStateSpeaking && state != kDeviceStateNotifying) {
        return;
    }
    CancelLocalPrompt(false);
    if (state == kDeviceStateNotifying) {
        StopNotification();
    }
    conversation_intent_.fetch_add(1);
    {
        std::lock_guard<std::mutex> lock(audio_receive_mutex_);
        conversation_events_enabled_.store(false);
        audio_service_.ResetDecoder();
    }
    audio_service_.EnableVoiceProcessing(false);
    audio_service_.ResetCapture();
    pending_listening_start_ = false;
    play_popup_on_listening_ = false;
    if (protocol_) {
        protocol_->CloseAudioChannel();
    }
    SetDeviceState(kDeviceStateIdle);
}

void Application::HandleConnectionWifiEvent(bool connected) {
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
    if (connected) {
        connection_health_.OnWifiConnected();
    } else {
        connection_health_.OnWifiDisconnected();
        connection_health_.Retire(ConnectionHealth::Stage::Control);
        RetireConversationHealth();
        EndConversationForConnectionFailure();
    }
    RefreshConnectionStatus();
#endif
}

void Application::HandleProtocolHealthEvent(const Protocol::HealthEvent& event) {
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
    using Stage = ConnectionHealth::Stage;
    using Outcome = Protocol::HealthOutcome;
    Stage stage;
    switch (event.stage) {
        case Protocol::HealthStage::Control:
            stage = Stage::Control;
            if (event.source_channel_token != 0) {
                return;
            }
            break;
        case Protocol::HealthStage::Handshake:
            stage = Stage::Handshake;
            break;
        case Protocol::HealthStage::Media:
            stage = Stage::Media;
            break;
        default:
            return;
    }
    if (event.attempt_id == 0 ||
        (stage != Stage::Control && event.attempt_id != event.source_channel_token)) {
        return;
    }
    if (event.outcome == Outcome::Started) {
        if (stage == Stage::Handshake) {
            RetireConversationHealth();
            handshake_source_token_ = event.source_channel_token;
        } else if (stage == Stage::Media) {
            if (event.source_channel_token != handshake_source_token_ ||
                connection_health_.GetStageState(Stage::Handshake) !=
                    ConnectionHealth::StageState::Healthy) {
                return;
            }
            media_attempt_id_ = event.attempt_id;
            media_source_token_ = event.source_channel_token;
            media_uses_probe_ = event.udp_probe;
            media_decode_request_.store(0);
            media_speech_token_.store(0);
        }
        connection_health_.BeginAttempt(stage, event.attempt_id);
    } else if (event.outcome == Outcome::Retired) {
        if (!connection_health_.Retire(stage, event.attempt_id)) {
            return;
        }
        if (stage == Stage::Handshake) {
            RetireConversationHealth();
        } else if (stage == Stage::Media) {
            media_attempt_id_ = 0;
            media_source_token_ = 0;
            media_decode_request_.store(0);
            media_speech_token_.store(0);
        }
    } else if (event.outcome == Outcome::Failed) {
        // A link outage is not evidence that a voice-service phase failed.
        // Previously confirmed service faults remain until their own success.
        if (!wifi_link_connected_.load() ||
            !connection_health_.ReportFailure(stage, event.attempt_id)) {
            return;
        }
        pending_connection_failure_.store(true);
        EndConversationForConnectionFailure();
    } else if (event.outcome == Outcome::Succeeded) {
        if (!wifi_link_connected_.load() || (stage == Stage::Media && !media_uses_probe_)) {
            return;
        }
        if (!connection_health_.ReportSuccess(stage, event.attempt_id)) {
            return;
        }
    }
    RefreshConnectionStatus();
#endif
}

void Application::RecordConfigurationResult(ConnectionHealth::Stage stage, uint64_t identity,
                                            uint32_t link_epoch, bool success) {
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
    if (!success) {
        pending_connection_failure_.store(true);
    }
    Schedule([this, stage, identity, link_epoch, success]() {
        if (identity != configuration_attempt_sequence_.load() ||
            link_epoch != wifi_link_epoch_.load() || !wifi_link_connected_.load()) {
            return;
        }
        if (stage != ConnectionHealth::Stage::Configuration &&
            stage != ConnectionHealth::Stage::Activation) {
            return;
        }
        connection_health_.BeginAttempt(stage, identity);
        if (success) {
            connection_health_.ReportSuccess(stage, identity);
        } else {
            connection_health_.ReportFailure(stage, identity);
        }
        RefreshConnectionStatus();
    });
#endif
}

bool Application::PrepareConnectionOpen() {
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
    RetireConversationHealth();
    if (!wifi_link_connected_.load()) {
        SetDeviceState(kDeviceStateIdle);
        RefreshConnectionStatus();
        return false;
    }
    RefreshConnectionStatus();
#endif
    return true;
}

void Application::RecordMediaRecognition(Protocol::ChannelToken token, uint64_t speech,
                                         bool nonempty) {
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
    if (media_uses_probe_ || !nonempty || speech == 0 || speech == media_stt_sequence_ ||
        token == 0 || token != media_source_token_ || token != media_speech_token_.load() ||
        speech != media_speech_sequence_.load() ||
        !IsConversationEventCurrent(token, ListeningPauseSequence())) {
        return;
    }
    media_stt_sequence_ = speech;
    media_decode_token_.store(token);
    media_decode_request_.store(speech);
#endif
}

void Application::HandleDecodedMedia(Protocol::ChannelToken token, uint64_t speech) {
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
    if (media_uses_probe_ || media_attempt_id_ == 0 || token != media_source_token_ ||
        speech != media_stt_sequence_ || speech != media_speech_sequence_.load() ||
        !IsConversationEventCurrent(token, ListeningPauseSequence())) {
        return;
    }
    if (connection_health_.ReportSuccess(ConnectionHealth::Stage::Media, media_attempt_id_)) {
        RefreshConnectionStatus();
    }
#endif
}

void Application::RefreshConnectionStatus() {
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
    const auto service = connection_health_.GetServiceState();
    connection_service_state_.store(service);
    const char* message = nullptr;
    if (connection_health_.GetWifiState() == ConnectionHealth::WifiState::Disconnected) {
        message = Lang::Strings::WIFI_UNAVAILABLE;
    } else if (service == ConnectionHealth::ServiceState::Unavailable) {
        message = Lang::Strings::SERVICE_UNAVAILABLE;
    }
    connection_alert_active_.store(message != nullptr);
    pending_connection_failure_.store(message != nullptr);
    if (local_prompt_.id != 0 && local_prompt_.kind == LocalPromptKind::Network &&
        !local_prompt_.started && !local_prompt_.cancelled) {
        const auto pending = connection_health_.GetPendingPrompt();
        if (pending.kind != local_prompt_.network_ticket.kind ||
            pending.episode != local_prompt_.network_ticket.episode) {
            CancelLocalPrompt(true);
        }
    }
    const auto state = GetDeviceState();
    auto* display = Board::GetInstance().GetDisplay();
    if (message != nullptr) {
        last_error_message_ = message;
        connection_display_message_ = message;
        if (state == kDeviceStateIdle || state == kDeviceStateActivating || IsListeningPaused()) {
            display->SetStatus(IsListeningPaused() ? Lang::Strings::LISTENING_PAUSED : message);
            if (state != kDeviceStateActivating || !activation_guidance_pending_.load()) {
                display->SetChatMessage("system", message);
            }
        }
    } else if (!connection_display_message_.empty()) {
        display->RemoveChatMessage("system", connection_display_message_.c_str());
        if (last_error_message_ == connection_display_message_) {
            last_error_message_.clear();
        }
        connection_display_message_.clear();
        if (state == kDeviceStateIdle) {
            display->SetStatus(IsListeningPaused() ? Lang::Strings::LISTENING_PAUSED
                                                   : Lang::Strings::STANDBY);
        } else if (state == kDeviceStateActivating) {
            display->SetStatus(Lang::Strings::ACTIVATION);
        }
    }
#endif
}

bool Application::TryStartPendingLocalPrompt(bool before_listening) {
    if (local_prompt_.id != 0) {
        return before_listening && !local_prompt_.cancelled;
    }
    const auto state = GetDeviceState();
    const bool listening_gap = before_listening && state == kDeviceStateListening &&
                               listening_mode_ != kListeningModeRealtime && !IsListeningPaused();
    const bool idle_gap =
        !before_listening && (state == kDeviceStateIdle || (state == kDeviceStateActivating &&
                                                            !activation_guidance_pending_.load()));
    if ((!listening_gap && !idle_gap) || !audio_service_.IsPlaybackIdle() ||
        audio_service_.IsLocalSoundBusy() || audio_service_.IsVoiceDetected() ||
        notify_player_.IsBusy() || NowMs() < local_prompt_retry_after_ms_) {
        return false;
    }
    auto& board = Board::GetInstance();
    auto* codec = board.GetAudioCodec();
    if (codec == nullptr || codec->GetOutputVolumeStatus().volume == 0) {
        return false;
    }
    LocalPromptContext request;
    std::string_view sound;
#if CONFIG_WAVESHARE_1_85C_HOME_LOW_BATTERY_ALERTS
    low_battery_reminder_.Observe(board.GetBatteryStatus());
    if (low_battery_reminder_.HasPending()) {
        if (!listening_gap && state != kDeviceStateIdle) {
            return false;
        }
        request.low_episode = low_battery_reminder_.PendingEpisode();
        if (request.low_episode == 0) {
            return false;
        }
        request.kind = LocalPromptKind::LowBattery;
        sound = Lang::Sounds::OGG_LOW_BATTERY;
    }
#endif
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
    if (request.kind == LocalPromptKind::None) {
        request.network_ticket = connection_health_.GetPendingPrompt();
        if (request.network_ticket.kind != ConnectionHealth::PromptKind::None) {
            const bool link_connected = wifi_link_connected_.load();
            if ((request.network_ticket.kind == ConnectionHealth::PromptKind::WifiUnavailable &&
                 link_connected) ||
                (request.network_ticket.kind == ConnectionHealth::PromptKind::ServiceUnavailable &&
                 !link_connected)) {
                return false;
            }
            request.kind = LocalPromptKind::Network;
            sound = Lang::Sounds::OGG_EXCLAMATION;
        }
    }
#endif
    if (request.kind == LocalPromptKind::None) {
        return false;
    }
    request.id = AllocatePlaybackId();
    request.resume_listening = listening_gap;
    request.intent = ConversationIntentSequence();
    request.pause_sequence = ListeningPauseSequence();
    request.channel = accepted_channel_token_.load();
    if (!audio_service_.TryPlayLocalSound(sound, request.id)) {
        local_prompt_retry_after_ms_ = NowMs() + 5000;
        return false;
    }
    // Worker callbacks only Schedule; this main-task record is installed before
    // their handlers can execute, even if the worker completed immediately.
    local_prompt_ = request;
    return true;
}

void Application::CancelLocalPrompt(bool resume_after_cancel) {
    if (!resume_after_cancel) {
        local_prompt_gap_allowed_ = false;
    }
    if (local_prompt_.id == 0) {
        return;
    }
    local_prompt_.cancelled = true;
    local_prompt_.resume_listening = resume_after_cancel && local_prompt_.resume_listening;
    audio_service_.CancelLocalSound(local_prompt_.id);
    // Retain the identity until finished: a delayed successful-write receipt
    // still consumes this episode but can never reopen a cancelled microphone.
}

void Application::HandleLocalSoundStarted(uint32_t id) {
    if (id == 0 || id != local_prompt_.id || local_prompt_.started) {
        return;
    }
    local_prompt_.started = true;
    if (local_prompt_.kind == LocalPromptKind::LowBattery) {
        low_battery_reminder_.MarkStarted(local_prompt_.low_episode);
    } else if (local_prompt_.kind == LocalPromptKind::Network) {
        connection_health_.MarkPromptStarted(local_prompt_.network_ticket);
    }
    ESP_LOGI(kTag, "Local prompt started (id=%lu)", static_cast<unsigned long>(id));
}

void Application::HandleLocalSoundFinished(uint32_t id, bool completed) {
    if (id == 0 || id != local_prompt_.id) {
        return;
    }
    const auto request = local_prompt_;
    local_prompt_ = {};
    ESP_LOGI(kTag, "Local prompt finished (id=%lu, completed=%d, started=%d)",
             static_cast<unsigned long>(id), completed, request.started);
    if (!completed && !request.started && !request.cancelled) {
        local_prompt_retry_after_ms_ = NowMs() + 5000;
    }
    const bool resume = request.resume_listening && !IsListeningPaused() &&
                        request.pause_sequence == ListeningPauseSequence() &&
                        request.intent == ConversationIntentSequence() &&
                        GetDeviceState() == kDeviceStateListening &&
                        IsConversationEventCurrent(request.channel, request.pause_sequence);
    if (resume) {
        if (audio_service_.IsPlaybackIdle()) {
            StartListeningAudio();
        } else {
            pending_listening_start_ = true;
        }
    }
}

void Application::TickLocalPrompts() {
#if CONFIG_WAVESHARE_1_85C_HOME_LOW_BATTERY_ALERTS
    low_battery_reminder_.Observe(Board::GetInstance().GetBatteryStatus());
#endif
    RefreshConnectionStatus();
    if (local_prompt_.id != 0 && !local_prompt_.started && !local_prompt_.cancelled) {
        bool still_pending = true;
        if (local_prompt_.kind == LocalPromptKind::LowBattery) {
            still_pending = low_battery_reminder_.PendingEpisode() == local_prompt_.low_episode;
        } else if (local_prompt_.kind == LocalPromptKind::Network) {
            const auto current = connection_health_.GetPendingPrompt();
            still_pending = current.kind == local_prompt_.network_ticket.kind &&
                            current.episode == local_prompt_.network_ticket.episode;
        }
        if (!still_pending) {
            CancelLocalPrompt(true);
        }
    }
    if (local_prompt_.id == 0) {
        TryStartPendingLocalPrompt(false);
    }
}

#endif  // CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS
