#include "application.h"
#include "assets.h"
#include "assets/lang_config.h"
#include "audio_codec.h"
#include "board.h"
#include "cjson_utils.h"
#include "display.h"
#include "mcp_server.h"
#include "mqtt_protocol.h"
#include "settings.h"
#include "system_info.h"
#include "text_glyph_payload.h"
#include "websocket_protocol.h"

#include <driver/gpio.h>
#include <esp_log.h>
#include <arpa/inet.h>
#include <cJSON.h>
#include <algorithm>
#include <cstring>
#include <limits>

#define TAG "Application"

Application::Application() : notify_player_(audio_service_) {
    event_group_ = xEventGroupCreate();

#if CONFIG_USE_DEVICE_AEC && CONFIG_USE_SERVER_AEC
#error "CONFIG_USE_DEVICE_AEC and CONFIG_USE_SERVER_AEC cannot be enabled at the same time"
#elif CONFIG_USE_DEVICE_AEC
    aec_mode_ = kAecOnDeviceSide;
#elif CONFIG_USE_SERVER_AEC
    aec_mode_ = kAecOnServerSide;
#else
    aec_mode_ = kAecOff;
#endif

    esp_timer_create_args_t clock_timer_args = {.callback =
                                                    [](void* arg) {
                                                        Application* app = (Application*)arg;
                                                        xEventGroupSetBits(app->event_group_,
                                                                           MAIN_EVENT_CLOCK_TICK);
                                                    },
                                                .arg = this,
                                                .dispatch_method = ESP_TIMER_TASK,
                                                .name = "clock_timer",
                                                .skip_unhandled_events = true};
    esp_timer_create(&clock_timer_args, &clock_timer_handle_);
}

Application::~Application() {
    notify_player_.Stop();
    if (clock_timer_handle_ != nullptr) {
        esp_timer_stop(clock_timer_handle_);
        esp_timer_delete(clock_timer_handle_);
    }
    if (startup_wake_word_hint_timer_ != nullptr) {
        esp_timer_stop(startup_wake_word_hint_timer_);
        esp_timer_delete(startup_wake_word_hint_timer_);
    }
    vEventGroupDelete(event_group_);
}

bool Application::SetDeviceState(DeviceState state) {
    [[maybe_unused]] const auto previous = GetDeviceState();
    if (!state_machine_.TransitionTo(state)) {
        return false;
    }
#if CONFIG_WAVESHARE_1_85C_HOME
    if (previous != state) {
        // Only atomics here: startup/activation can also request transitions.
        // The main task owns the display state and discards queued old turns.
        waiting_turn_open_.store(false);
        waiting_turn_.fetch_add(1);
    }
#endif
    return true;
}

void Application::Initialize() {
    auto& board = Board::GetInstance();
    SetDeviceState(kDeviceStateStarting);

    // Setup the display
    auto display = board.GetDisplay();
    display->SetupUI();
    // Print board name/version info
    display->SetChatMessage("system", SystemInfo::GetUserAgent().c_str());

    // Setup the audio service
    auto codec = board.GetAudioCodec();
    audio_service_.Initialize(codec);
    audio_service_.Start();
    ESP_LOGI(TAG, "After board/audio init");
    SystemInfo::PrintHeapStats();

    AudioServiceCallbacks callbacks;
    callbacks.on_send_queue_available = [this]() {
        xEventGroupSetBits(event_group_, MAIN_EVENT_SEND_AUDIO);
    };
    callbacks.on_wake_word_detected = [this](const std::string& wake_word) {
        if (IsListeningPaused()) {
            return;
        }
        xEventGroupSetBits(event_group_, MAIN_EVENT_WAKE_WORD_DETECTED);
    };
    callbacks.on_vad_change = [this](bool speaking) {
#if CONFIG_WAVESHARE_1_85C_HOME
        // Capture identity before eligibility: a preempted old callback must
        // not acquire the next listening turn after an answer starts/stops.
        const auto turn = waiting_turn_.load();
        const auto token = accepted_channel_token_.load();
        const auto pause_sequence = ListeningPauseSequence();
        if (speaking && waiting_turn_open_.load() && GetDeviceState() == kDeviceStateListening &&
            !IsListeningPaused() && turn == waiting_turn_.load()) {
            Schedule([this, turn, token, pause_sequence]() {
                HandleWaitingSpeech(turn, token, pause_sequence);
            });
        }
#endif
        xEventGroupSetBits(event_group_, MAIN_EVENT_VAD_CHANGE);
    };
    callbacks.on_playback_drained = [this]() {
        xEventGroupSetBits(event_group_, MAIN_EVENT_PLAYBACK_DRAINED);
    };
    callbacks.on_playback_progress = [this](uint32_t playback_id, uint32_t media_position_ms) {
        notify_player_.OnPlaybackProgress(playback_id, media_position_ms);
    };
#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS
    callbacks.on_local_sound_started = [this](uint32_t id) {
        Schedule([this, id]() { HandleLocalSoundStarted(id); });
    };
    callbacks.on_local_sound_finished = [this](uint32_t id, bool completed) {
        Schedule([this, id, completed]() { HandleLocalSoundFinished(id, completed); });
    };
#endif
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
    callbacks.on_incoming_audio_decoded = [this](uint64_t token, uint64_t request) {
        auto speech = request;
        if (speech == 0) {
            return;
        }
        const auto pause = ListeningPauseSequence();
        if (token == 0 || token != media_decode_token_.load() ||
            !IsConversationEventCurrent(token, pause)) {
            return;
        }
        const auto intent = ConversationIntentSequence();
        if (!media_decode_request_.compare_exchange_strong(speech, 0)) {
            return;
        }
        Schedule([this, token, speech, pause, intent]() {
            if (pause == ListeningPauseSequence() && intent == ConversationIntentSequence() &&
                IsConversationEventCurrent(token, pause)) {
                HandleDecodedMedia(token, speech);
            }
        });
    };
#endif
    audio_service_.SetCallbacks(callbacks);

    // Load local fonts, images and speech models before starting the network. A pending
    // update must finish first: these resources can reference the partition being rewritten.
    {
        Settings settings("assets", false);
        if (settings.GetString("download_url").empty()) {
            CheckAssetsVersion();
        }
    }

    // Add state change listeners
    state_machine_.AddStateChangeListener([this](DeviceState old_state, DeviceState new_state) {
        xEventGroupSetBits(event_group_, MAIN_EVENT_STATE_CHANGED);
    });

    // Start the clock timer to update the status bar
    esp_timer_start_periodic(clock_timer_handle_, 1000000);

    // Add MCP common tools (only once during initialization)
    auto& mcp_server = McpServer::GetInstance();
    mcp_server.AddCommonTools();
    mcp_server.AddUserOnlyTools();
    // Registration is complete here, so this is the only place the whole inventory exists.
    // It is logged because a server that accepts a bounded number of tools drops the rest
    // quietly: the only sign on this device was an alert at boot and a sad face, which is
    // easy to see and hard to connect to a tool list.
    mcp_server.LogToolInventory();

    // Set network event callback for UI updates and network state handling
    board.SetNetworkEventCallback([this](NetworkEvent event, const std::string& data) {
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
        if (event == NetworkEvent::Connected || event == NetworkEvent::Disconnected) {
            const bool connected = event == NetworkEvent::Connected;
            if (wifi_link_connected_.exchange(connected) != connected) {
                wifi_link_epoch_.fetch_add(1);
            }
            if (!connected) {
                wifi_disconnect_sequence_.fetch_add(1);
            }
        }
#endif
        auto display = Board::GetInstance().GetDisplay();

        switch (event) {
            case NetworkEvent::Scanning:
                display->ShowNotification(Lang::Strings::SCANNING_WIFI, 30000);
                xEventGroupSetBits(event_group_, MAIN_EVENT_NETWORK_DISCONNECTED);
                break;
            case NetworkEvent::Connecting: {
                if (data.empty()) {
                    // Cellular network - registering without carrier info yet
                    display->SetStatus(Lang::Strings::REGISTERING_NETWORK);
                } else {
                    // WiFi or cellular with carrier info
                    std::string msg = Lang::Strings::CONNECT_TO;
                    msg += data;
                    msg += "...";
                    display->ShowNotification(msg.c_str(), 30000);
                }
                break;
            }
            case NetworkEvent::Connected: {
                std::string msg = Lang::Strings::CONNECTED_TO;
                msg += data;
                display->ShowNotification(msg.c_str(), 30000);
                xEventGroupSetBits(event_group_, MAIN_EVENT_NETWORK_CONNECTED);
                break;
            }
            case NetworkEvent::Disconnected:
                xEventGroupSetBits(event_group_, MAIN_EVENT_NETWORK_DISCONNECTED);
                break;
            case NetworkEvent::WifiConfigModeEnter:
                // WiFi config mode enter is handled by WifiBoard internally
                break;
            case NetworkEvent::WifiConfigModeExit:
                // WiFi config mode exit is handled by WifiBoard internally
                break;
            // Cellular modem specific events
            case NetworkEvent::ModemDetecting:
                display->SetStatus(Lang::Strings::DETECTING_MODULE);
                break;
            case NetworkEvent::ModemErrorNoSim:
                Alert(Lang::Strings::ERROR, Lang::Strings::PIN_ERROR, "warning",
                      Lang::Sounds::OGG_ERR_PIN);
                break;
            case NetworkEvent::ModemErrorRegDenied:
                Alert(Lang::Strings::ERROR, Lang::Strings::REG_ERROR, "warning",
                      Lang::Sounds::OGG_ERR_REG);
                break;
            case NetworkEvent::ModemErrorInitFailed:
                Alert(Lang::Strings::ERROR, Lang::Strings::MODEM_INIT_ERROR, "warning",
                      Lang::Sounds::OGG_EXCLAMATION);
                break;
            case NetworkEvent::ModemErrorTimeout:
                display->SetStatus(Lang::Strings::REGISTERING_NETWORK);
                break;
        }
    });

    // Start network asynchronously
    board.StartNetwork();

    // Update the status bar immediately to show the network state
    display->UpdateStatusBar(true);
}

void Application::Run() {
    // Set the priority of the main task to 10
    vTaskPrioritySet(nullptr, 10);

    const EventBits_t ALL_EVENTS =
        MAIN_EVENT_SCHEDULE | MAIN_EVENT_SEND_AUDIO | MAIN_EVENT_WAKE_WORD_DETECTED |
        MAIN_EVENT_VAD_CHANGE | MAIN_EVENT_CLOCK_TICK | MAIN_EVENT_ERROR |
        MAIN_EVENT_NETWORK_CONNECTED | MAIN_EVENT_NETWORK_DISCONNECTED | MAIN_EVENT_TOGGLE_CHAT |
        MAIN_EVENT_START_LISTENING | MAIN_EVENT_STOP_LISTENING | MAIN_EVENT_ACTIVATION_DONE |
        MAIN_EVENT_STATE_CHANGED | MAIN_EVENT_PLAYBACK_DRAINED | MAIN_EVENT_STARTUP_HINT_TIMEOUT |
        MAIN_EVENT_PROTOCOL_PENDING | MAIN_EVENT_PRIMARY_BUTTON | MAIN_EVENT_LISTENING_PAUSE;

    while (true) {
        auto bits = xEventGroupWaitBits(event_group_, ALL_EVENTS, pdTRUE, pdFALSE, portMAX_DELAY);

        if (ApplyPendingListeningPause()) {
            bits &= ~(MAIN_EVENT_PRIMARY_BUTTON | MAIN_EVENT_TOGGLE_CHAT |
                      MAIN_EVENT_START_LISTENING | MAIN_EVENT_WAKE_WORD_DETECTED);
        }

#if CONFIG_WAVESHARE_1_85C_HOME
        if ((bits & MAIN_EVENT_ERROR) &&
            (pending_error_intent_ != ConversationIntentSequence() ||
             pending_error_pause_sequence_ != ListeningPauseSequence() ||
             pending_error_callback_epoch_ != protocol_callback_epoch_.load())) {
            bits &= ~MAIN_EVENT_ERROR;
            last_error_message_.clear();
        }
#endif

        if ((bits & MAIN_EVENT_ERROR) && !last_error_message_.empty()) {
#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS
            CancelLocalPrompt(false);
#endif
            if (GetDeviceState() == kDeviceStateNotifying) {
                StopNotification();
            }
            SetDeviceState(kDeviceStateIdle);
            Alert(Lang::Strings::ERROR, last_error_message_.c_str(), "cancel",
                  Lang::Sounds::OGG_EXCLAMATION);
        }

        if (bits & MAIN_EVENT_NETWORK_CONNECTED) {
            HandleNetworkConnectedEvent();
        }

        if (bits & MAIN_EVENT_NETWORK_DISCONNECTED) {
            HandleNetworkDisconnectedEvent();
        }

        if (bits & MAIN_EVENT_ACTIVATION_DONE) {
            HandleActivationDoneEvent();
        }

        if (bits & MAIN_EVENT_STATE_CHANGED) {
            HandleStateChangedEvent();
        }

        if (bits & MAIN_EVENT_PLAYBACK_DRAINED) {
            if (audio_service_.IsPlaybackIdle()) {
                notify_player_.OnPlaybackDrained();
            }
            // Deferred listening start (auto mode): the playback queue has
            // drained, so it is now safe to enable voice processing.
            if (pending_listening_start_ && GetDeviceState() == kDeviceStateListening &&
                audio_service_.IsPlaybackIdle()) {
                pending_listening_start_ = false;
                StartListeningAudio();
            }
        }

        if (bits & MAIN_EVENT_TOGGLE_CHAT) {
            HandleToggleChatEvent();
        }

        if (bits & MAIN_EVENT_PRIMARY_BUTTON) {
            HandlePrimaryButtonEvent();
        }

        if (bits & MAIN_EVENT_START_LISTENING) {
            HandleStartListeningEvent();
        }

        if (bits & MAIN_EVENT_STOP_LISTENING) {
            HandleStopListeningEvent();
        }

        if ((bits & MAIN_EVENT_PROTOCOL_PENDING) && protocol_) {
            protocol_->ProcessPending();
        }

        if (bits & MAIN_EVENT_SEND_AUDIO) {
            while (auto packet = audio_service_.PopPacketFromSendQueue()) {
                if (IsListeningPaused()) {
                    continue;
                }
                if (protocol_ && !protocol_->SendAudio(std::move(packet))) {
                    // Drop the remaining packets. Leaving them in the queue would
                    // stall the Opus codec task (it waits for queue space), which in
                    // turn deadlocks the whole audio input pipeline, as no new
                    // MAIN_EVENT_SEND_AUDIO event would ever be triggered again.
                    while (audio_service_.PopPacketFromSendQueue())
                        ;
                    break;
                }
            }
        }

        if (bits & MAIN_EVENT_WAKE_WORD_DETECTED) {
            HandleWakeWordDetectedEvent();
        }

        if (bits & MAIN_EVENT_VAD_CHANGE) {
            if (GetDeviceState() == kDeviceStateListening) {
                auto led = Board::GetInstance().GetLed();
                led->OnStateChanged();
            }
        }

        if (bits & MAIN_EVENT_SCHEDULE) {
            std::unique_lock<std::mutex> lock(mutex_);
            auto tasks = std::move(main_tasks_);
            lock.unlock();
            for (auto& task : tasks) {
                task();
            }
        }

        if (bits & MAIN_EVENT_CLOCK_TICK) {
            clock_ticks_++;
            UpdateWaitingResponse();
#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS
            TickLocalPrompts();
#endif
            auto display = Board::GetInstance().GetDisplay();
            display->UpdateStatusBar();

            // Print debug info every 10 seconds
            if (clock_ticks_ % 10 == 0) {
                SystemInfo::PrintHeapStats();
                // SystemInfo::PrintTaskList();
                // SystemInfo::PrintTaskCpuUsage(pdMS_TO_TICKS(1000));
            }
        }
        if (bits & MAIN_EVENT_STARTUP_HINT_TIMEOUT) {
            HideStartupWakeWordHint();
        }
        ShowStartupWakeWordHint();
    }
}

void Application::HandleNetworkConnectedEvent() {
    ESP_LOGI(TAG, "Network connected");
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
    if (!wifi_link_connected_.load()) {
        return;
    }
    HandleConnectionWifiEvent(true);
#endif
    auto state = GetDeviceState();

    if (state == kDeviceStateStarting || state == kDeviceStateWifiConfiguring) {
        // Network is ready, start activation
        SetDeviceState(kDeviceStateActivating);
        if (activation_task_handle_ != nullptr) {
            ESP_LOGW(TAG, "Activation task already running");
            return;
        }

        xTaskCreate(
            [](void* arg) {
                Application* app = static_cast<Application*>(arg);
                app->ActivationTask();
                app->activation_task_handle_ = nullptr;
                vTaskDelete(NULL);
            },
            "activation", 4096 * 2, this, 2, &activation_task_handle_);
    }

    // Update the status bar immediately to show the network state
    auto display = Board::GetInstance().GetDisplay();
    display->UpdateStatusBar(true);
}

void Application::HandleNetworkDisconnectedEvent() {
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
    const auto disconnected = wifi_disconnect_sequence_.load();
    if (disconnected != processed_wifi_disconnect_sequence_ || !wifi_link_connected_.load()) {
        processed_wifi_disconnect_sequence_ = disconnected;
        HandleConnectionWifiEvent(false);
        // Even a short outage must retire the old conversation. The link may
        // already be back by the time the main task processes this event.
        if (wifi_link_connected_.load()) {
            HandleConnectionWifiEvent(true);
        }
    }
    Board::GetInstance().GetDisplay()->UpdateStatusBar(true);
    return;
#endif
    // Close current conversation when network disconnected
    auto state = GetDeviceState();
    if (state == kDeviceStateNotifying) {
        StopNotification();
    }
    if (state == kDeviceStateConnecting || state == kDeviceStateListening ||
        state == kDeviceStateSpeaking) {
        ESP_LOGI(TAG, "Closing audio channel due to network disconnection");
        if (protocol_) {
            protocol_->CloseAudioChannel();
        }
    }

    // Update the status bar immediately to show the network state
    auto display = Board::GetInstance().GetDisplay();
    display->UpdateStatusBar(true);
}

void Application::HandleActivationDoneEvent() {
    ESP_LOGI(TAG, "Activation done");

    SystemInfo::PrintHeapStats();
    SetDeviceState(kDeviceStateIdle);

    has_server_time_ = ota_->HasServerTime();

    // Protocol start may have already raised MAIN_EVENT_ERROR. Do not replace
    // that alert with the "ready" UI/sound — the main loop can process both
    // events back-to-back because the activation task is lower priority.
    const bool has_error = !last_error_message_.empty()
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
                           || pending_connection_failure_.load() || !wifi_link_connected_.load()
#endif
        ;
    if (!startup_completed_) {
        startup_completed_ = true;
        pending_startup_wake_word_hint_ = !has_error;
    }
    if (!has_error) {
        auto display = Board::GetInstance().GetDisplay();
        std::string message = std::string(Lang::Strings::VERSION) + ota_->GetCurrentVersion();
        display->ShowNotification(message.c_str());
        display->SetChatMessage("system", "");
    }

    // Release OTA object after activation is complete
    ota_.reset();
    auto& board = Board::GetInstance();
    board.SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);

    if (!has_error) {
        Schedule([this]() {
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
            if (HasConnectionAlert() || pending_connection_failure_.load() ||
                !wifi_link_connected_.load()) {
                return;
            }
#endif
            // Play the success sound to indicate the device is ready
            audio_service_.PlaySound(Lang::Sounds::OGG_SUCCESS);
        });
    }
}

void Application::ActivationTask() {
    // Create OTA object for activation process
    ota_ = std::make_unique<Ota>();

    // Check for new assets version
    CheckAssetsVersion();

    // Check for new firmware version
    CheckNewVersion();

    // Initialize the protocol
    InitializeProtocol();

    // Signal completion to main loop
    xEventGroupSetBits(event_group_, MAIN_EVENT_ACTIVATION_DONE);
}

void Application::CheckAssetsVersion() {
    // Only allow CheckAssetsVersion to be called once
    if (assets_version_checked_) {
        return;
    }
    assets_version_checked_ = true;

    auto& board = Board::GetInstance();
    auto display = board.GetDisplay();
    auto& assets = Assets::GetInstance();

    if (!assets.partition_valid()) {
        ESP_LOGW(TAG, "Assets partition is disabled for board %s", BOARD_NAME);
        return;
    }

    Settings settings("assets", true);
    // Check if there is a new assets need to be downloaded
    std::string download_url = settings.GetString("download_url");

    if (!download_url.empty()) {
        settings.EraseKey("download_url");

        char message[256];
        snprintf(message, sizeof(message), Lang::Strings::FOUND_NEW_ASSETS, download_url.c_str());
        Alert(Lang::Strings::LOADING_ASSETS, message, "cloud_download", Lang::Sounds::OGG_UPGRADE);

        // Wait for the audio service to be idle for 3 seconds
        vTaskDelay(pdMS_TO_TICKS(3000));
        SetDeviceState(kDeviceStateUpgrading);
        board.SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);
        display->SetChatMessage("system", Lang::Strings::PLEASE_WAIT);

        bool success =
            assets.Download(download_url, [this, display](int progress, size_t speed) -> void {
                char buffer[32];
                snprintf(buffer, sizeof(buffer), "%d%% %uKB/s", progress, speed / 1024);
                Schedule([display, message = std::string(buffer)]() {
                    display->SetChatMessage("system", message.c_str());
                });
            });

        board.SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);
        vTaskDelay(pdMS_TO_TICKS(1000));

        if (!success) {
            Alert(Lang::Strings::ERROR, Lang::Strings::DOWNLOAD_ASSETS_FAILED, "cancel",
                  Lang::Sounds::OGG_EXCLAMATION);
            vTaskDelay(pdMS_TO_TICKS(2000));
            SetDeviceState(kDeviceStateActivating);
            return;
        }
    }

    // Apply assets
    assets.Apply();
    display->SetChatMessage("system", "");
    // "robot_2" is declared only by a board shipping its own pack: esp-vocat maps it to
    // neutral.eaf, and the rgb-matrix display special-cases the string. None of the shared
    // esp_emote_assets packs (320_240, 360_360, 1024_600) declares it, so on every board that
    // uses one this call fails, logs two errors and leaves whatever face was already up.
    // Fall back to an emotion all three packs do declare.
    if (!display->SetEmotion("robot_2")) {
        display->SetEmotion("neutral");
    }
}

void Application::CheckNewVersion() {
    const int MAX_RETRY = 10;
    int retry_count = 0;
    int retry_delay = 10;  // Initial retry delay in seconds

    auto& board = Board::GetInstance();
    while (true) {
        auto display = board.GetDisplay();
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
        const auto configuration_attempt = configuration_attempt_sequence_.fetch_add(1) + 1;
        const auto configuration_link = wifi_link_epoch_.load();
        Schedule([this, configuration_attempt, configuration_link]() {
            if (configuration_attempt == configuration_attempt_sequence_.load() &&
                configuration_link == wifi_link_epoch_.load()) {
                connection_health_.BeginAttempt(ConnectionHealth::Stage::Configuration,
                                                configuration_attempt);
                RefreshConnectionStatus();
            }
        });
        if (!HasConnectionAlert()) {
            display->SetStatus(Lang::Strings::CHECKING_NEW_VERSION);
        }
#else
        display->SetStatus(Lang::Strings::CHECKING_NEW_VERSION);
#endif

        auto check = ota_->CheckVersion();
        if (!check) {
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
            RecordConfigurationResult(ConnectionHealth::Stage::Configuration, configuration_attempt,
                                      configuration_link, false);
#endif
            retry_count++;
            if (retry_count >= MAX_RETRY) {
                ESP_LOGE(TAG, "Too many retries, exit version check");
                return;
            }

            const auto& err = check.error();
            char error_message[160];
            int error_message_length =
                snprintf(error_message, sizeof(error_message), "%s", err.ToString().c_str());
            if (error_message_length < 0 ||
                error_message_length >= static_cast<int>(sizeof(error_message))) {
                snprintf(error_message, sizeof(error_message), "%s", err.Message());
            }

            char buffer[320];
            int alert_message_length =
                snprintf(buffer, sizeof(buffer), Lang::Strings::CHECK_NEW_VERSION_FAILED,
                         retry_delay, error_message);
            if (alert_message_length < 0 ||
                alert_message_length >= static_cast<int>(sizeof(buffer))) {
                snprintf(buffer, sizeof(buffer), "%s", err.Message());
            }
#if !CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
            Alert(Lang::Strings::ERROR, buffer, "cloud_off", Lang::Sounds::OGG_EXCLAMATION);
#endif

            ESP_LOGW(TAG, "Check new version failed, retry in %d seconds (%d/%d)", retry_delay,
                     retry_count, MAX_RETRY);
            for (int i = 0; i < retry_delay; i++) {
                vTaskDelay(pdMS_TO_TICKS(1000));
                if (GetDeviceState() == kDeviceStateIdle) {
                    break;
                }
            }
            retry_delay *= 2;  // Double the retry delay
            continue;
        }
        retry_count = 0;
        retry_delay = 10;  // Reset retry delay
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
        RecordConfigurationResult(ConnectionHealth::Stage::Configuration, configuration_attempt,
                                  configuration_link, true);
        activation_guidance_pending_.store(ota_->HasActivationCode() ||
                                           ota_->HasActivationChallenge());
#endif

        if (ota_->HasNewVersion()) {
#if CONFIG_WAVESHARE_1_85C_HOME
            ESP_LOGI(TAG, "Home firmware keeps the current image; offered version: %s",
                     ota_->GetFirmwareVersion().c_str());
#else
            if (UpgradeFirmware(ota_->GetFirmwareUrl(), ota_->GetFirmwareVersion())) {
                return;  // This line will never be reached after reboot
            }
            // If upgrade failed, continue to normal operation
#endif
        }

        // Keep the running image valid before continuing activation.
        ota_->MarkCurrentVersionValid();
        if (!ota_->HasActivationCode() && !ota_->HasActivationChallenge()) {
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
            RecordConfigurationResult(ConnectionHealth::Stage::Activation, configuration_attempt,
                                      configuration_link, true);
#endif
            // Exit the loop if done checking new version
            break;
        }

        display->SetStatus(Lang::Strings::ACTIVATION);
        // Activation code is shown to the user and waiting for the user to input
        if (ota_->HasActivationCode()) {
            ShowActivationCode(ota_->GetActivationCode(), ota_->GetActivationMessage());
        }

        // This will block the loop until the activation is done or timeout
        for (int i = 0; i < 10; ++i) {
            ESP_LOGI(TAG, "Activating... %d/%d", i + 1, 10);
            esp_err_t err = ota_->Activate();
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
            if (ota_->HasActivationChallenge()) {
                // HTTP 202 is normal "waiting for binding", not a timeout fault.
                RecordConfigurationResult(ConnectionHealth::Stage::Activation,
                                          configuration_attempt, configuration_link,
                                          err == ESP_OK || err == ESP_ERR_TIMEOUT);
            }
#endif
            if (err == ESP_OK) {
                break;
            } else if (err == ESP_ERR_TIMEOUT) {
                vTaskDelay(pdMS_TO_TICKS(3000));
            } else {
                vTaskDelay(pdMS_TO_TICKS(10000));
            }
            if (GetDeviceState() == kDeviceStateIdle) {
                break;
            }
        }
    }
}

void Application::InitializeProtocol() {
#if CONFIG_WAVESHARE_1_85C_HOME
    const auto callback_epoch = protocol_callback_epoch_.fetch_add(1) + 1;
    {
        std::lock_guard<std::mutex> lock(audio_receive_mutex_);
        conversation_events_enabled_.store(false);
        accepted_channel_token_.store(0);
    }
#else
    const uint32_t callback_epoch = 0;
#endif
    auto& board = Board::GetInstance();
    auto display = board.GetDisplay();
    auto codec = board.GetAudioCodec();

    display->SetStatus(Lang::Strings::LOADING_PROTOCOL);

    if (ota_->HasMqttConfig()) {
        protocol_ = std::make_unique<MqttProtocol>();
    } else if (ota_->HasWebsocketConfig()) {
        protocol_ = std::make_unique<WebsocketProtocol>();
    } else {
        ESP_LOGW(TAG, "No protocol specified in the OTA config, using MQTT");
        protocol_ = std::make_unique<MqttProtocol>();
    }

#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
    const bool uses_mqtt = ota_->HasMqttConfig() || !ota_->HasWebsocketConfig();
    Schedule([this, callback_epoch, uses_mqtt]() {
        if (callback_epoch != protocol_callback_epoch_.load()) {
            return;
        }
        using Stage = ConnectionHealth::Stage;
        using ConnectionHealthTypes::StageMask;
        auto required = StageMask(Stage::Configuration) | StageMask(Stage::Activation) |
                        StageMask(Stage::Handshake) | StageMask(Stage::Media);
        if (uses_mqtt) {
            required |= StageMask(Stage::Control);
        }
        connection_health_.Retire(Stage::Control);
        RetireConversationHealth();
        connection_health_.SetRequiredStages(required);
        RefreshConnectionStatus();
    });
    protocol_->OnHealthEvent([this, callback_epoch](const Protocol::HealthEvent& event) {
        if (callback_epoch != protocol_callback_epoch_.load()) {
            return;
        }
        const auto link = wifi_link_epoch_.load();
        const auto intent = ConversationIntentSequence();
        const auto pause = ListeningPauseSequence();
        if (event.outcome == Protocol::HealthOutcome::Failed && wifi_link_connected_.load()) {
            pending_connection_failure_.store(true);
        }
        Schedule([this, callback_epoch, event, link, intent, pause]() {
            if (callback_epoch != protocol_callback_epoch_.load() ||
                link != wifi_link_epoch_.load()) {
                return;
            }
            if (event.stage != Protocol::HealthStage::Control &&
                (intent != ConversationIntentSequence() || pause != ListeningPauseSequence())) {
                return;
            }
            HandleProtocolHealthEvent(event);
        });
    });
#endif

    protocol_->OnConnected([this, callback_epoch]() {
        Schedule([this, callback_epoch]() {
#if CONFIG_WAVESHARE_1_85C_HOME
            if (callback_epoch != protocol_callback_epoch_.load()) {
                return;
            }
#endif
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
            RefreshConnectionStatus();
#else
            DismissAlert();
#endif
        });
    });

    protocol_->OnNetworkError([this, callback_epoch](const std::string& message) {
        const auto intent = ConversationIntentSequence();
        const auto pause_sequence = ListeningPauseSequence();
        Schedule([this, callback_epoch, intent, pause_sequence, message]() {
#if CONFIG_WAVESHARE_1_85C_HOME
            if (callback_epoch != protocol_callback_epoch_.load() ||
                intent != ConversationIntentSequence() ||
                pause_sequence != ListeningPauseSequence()) {
                return;
            }
            pending_error_callback_epoch_ = callback_epoch;
            pending_error_intent_ = intent;
            pending_error_pause_sequence_ = pause_sequence;
#endif
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
            // Typed stage events own recovery and the one-shot local prompt.
            // A string-only callback cannot safely attribute a late failure.
            ESP_LOGW(TAG, "Protocol reported an error: %s", message.c_str());
#else
            last_error_message_ = message;
            xEventGroupSetBits(event_group_, MAIN_EVENT_ERROR);
#endif
        });
    });

    protocol_->OnIncomingAudio([this, callback_epoch](std::unique_ptr<AudioStreamPacket> packet,
                                                      Protocol::ChannelToken token) {
#if CONFIG_WAVESHARE_1_85C_HOME
        std::lock_guard<std::mutex> lock(audio_receive_mutex_);
        if (callback_epoch != protocol_callback_epoch_.load() ||
            !IsConversationEventCurrent(token, ListeningPauseSequence())) {
            return;
        }
#endif
        if (GetDeviceState() == kDeviceStateSpeaking) {
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
            packet->source_channel_token = token;
            packet->source_request_id = media_decode_request_.load();
#endif
            audio_service_.PushPacketToDecodeQueue(std::move(packet));
        }
    });

    protocol_->OnAudioChannelOpened([this, codec, &board](Protocol::ChannelToken token) {
#if CONFIG_WAVESHARE_1_85C_HOME
        if (!protocol_ || token != protocol_->channel_token() || IsListeningPaused()) {
            return;
        }
#endif
        board.SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);
        if (protocol_->server_sample_rate() != codec->output_sample_rate()) {
            ESP_LOGW(TAG,
                     "Server sample rate %d does not match device output sample rate %d, "
                     "resampling may cause distortion",
                     protocol_->server_sample_rate(), codec->output_sample_rate());
        }
    });

    protocol_->OnAudioChannelClosed([this, &board, callback_epoch](Protocol::ChannelToken token) {
        uint32_t intent = 0;
#if CONFIG_WAVESHARE_1_85C_HOME
        {
            std::lock_guard<std::mutex> lock(audio_receive_mutex_);
            if (callback_epoch != protocol_callback_epoch_.load() ||
                token <= accepted_channel_token_.load()) {
                return;
            }
            intent = accepted_channel_intent_.load();
            accepted_channel_token_.store(token);
            conversation_events_enabled_.store(false);
        }
#endif
#if !CONFIG_WAVESHARE_1_85C_HOME
        board.SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);
#endif
        const auto pause_sequence = ListeningPauseSequence();
        Schedule([this, &board, token, pause_sequence, intent, callback_epoch]() {
#if CONFIG_WAVESHARE_1_85C_HOME
            if (callback_epoch != protocol_callback_epoch_.load() ||
                token != accepted_channel_token_.load() || IsListeningPaused() ||
                pause_sequence != ListeningPauseSequence() ||
                intent != ConversationIntentSequence()) {
                return;
            }
            conversation_events_enabled_.store(false);
            board.SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);
#endif
            auto display = Board::GetInstance().GetDisplay();
            display->SetChatMessage("system", "");
            SetDeviceState(kDeviceStateIdle);
        });
    });

    protocol_->OnIncomingJson([this, display, callback_epoch](const cJSON* root,
                                                              Protocol::ChannelToken token) {
#if CONFIG_WAVESHARE_1_85C_HOME
        if (callback_epoch != protocol_callback_epoch_.load()) {
            return;
        }
#endif
        // Parse JSON data
        auto type = cJSON_GetObjectItem(root, "type");
        if (!cJSON_IsString(type)) {
            ESP_LOGW(TAG, "Incoming JSON message has no type");
            return;
        }
        const auto pause_sequence = ListeningPauseSequence();
        const bool conversation_message = strcmp(type->valuestring, "tts") == 0 ||
                                          strcmp(type->valuestring, "stt") == 0 ||
                                          strcmp(type->valuestring, "llm") == 0;
        if (conversation_message && !IsConversationEventCurrent(token, pause_sequence)) {
            return;
        }
        if (strcmp(type->valuestring, "notify") == 0) {
            if (IsListeningPaused()) {
                return;
            }
            auto audio_url = cJSON_GetObjectItem(root, "audio_url");
            if (!cJSON_IsString(audio_url) || audio_url->valuestring[0] == '\0') {
                ESP_LOGW(TAG, "Notify message requires audio_url");
                return;
            }

            std::vector<NotifySubtitle> subtitles;
            auto subtitles_json = cJSON_GetObjectItem(root, "subtitles");
            if (subtitles_json != nullptr && !cJSON_IsArray(subtitles_json)) {
                ESP_LOGW(TAG, "Notify subtitles must be an array");
                return;
            }
            if (cJSON_IsArray(subtitles_json)) {
                cJSON* item = nullptr;
                cJSON_ArrayForEach (item, subtitles_json) {
                    auto start_ms = cJSON_GetObjectItem(item, "start_ms");
                    auto text = cJSON_GetObjectItem(item, "text");
                    if (!cJSON_IsNumber(start_ms) || start_ms->valuedouble < 0 ||
                        start_ms->valuedouble > std::numeric_limits<uint32_t>::max() ||
                        !cJSON_IsString(text)) {
                        ESP_LOGW(TAG, "Ignoring invalid notify subtitle");
                        continue;
                    }
                    subtitles.push_back({.start_ms = static_cast<uint32_t>(start_ms->valuedouble),
                                         .text = text->valuestring});
                }
            }

            Schedule([this, pause_sequence, callback_epoch,
                      url = std::string(audio_url->valuestring),
                      subtitles = std::move(subtitles)]() mutable {
#if CONFIG_WAVESHARE_1_85C_HOME
                if (callback_epoch != protocol_callback_epoch_.load()) {
                    return;
                }
#endif
                if (IsListeningPaused() || pause_sequence != ListeningPauseSequence()) {
                    return;
                }
                StartNotification(std::move(url), std::move(subtitles));
            });
        } else if (strcmp(type->valuestring, "tts") == 0) {
            auto state = cJSON_GetObjectItem(root, "state");
            if (!cJSON_IsString(state)) {
                return;
            }
            if (strcmp(state->valuestring, "start") == 0) {
                Schedule([this, token, pause_sequence]() {
                    if (!IsConversationEventCurrent(token, pause_sequence)) {
                        return;
                    }
                    aborted_ = false;
                    SetDeviceState(kDeviceStateSpeaking);
                });
            } else if (strcmp(state->valuestring, "stop") == 0) {
                Schedule([this, token, pause_sequence]() {
                    if (!IsConversationEventCurrent(token, pause_sequence)) {
                        return;
                    }
                    if (GetDeviceState() == kDeviceStateSpeaking) {
                        if (listening_mode_ == kListeningModeManualStop) {
                            SetDeviceState(kDeviceStateIdle);
                        } else {
#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS
                            local_prompt_gap_allowed_ = listening_mode_ != kListeningModeRealtime;
#endif
                            SetDeviceState(kDeviceStateListening);
                        }
                    }
                });
            } else if (strcmp(state->valuestring, "sentence_start") == 0) {
                auto text = cJSON_GetObjectItem(root, "text");
                if (cJSON_IsString(text)) {
                    std::vector<TextGlyph> glyphs;
                    uint8_t bpp = 0;
                    if (!TextGlyphPayload::Parse(root, glyphs, bpp)) {
                        glyphs.clear();
                    }
                    ESP_LOGI(TAG, "<< %s", text->valuestring);
                    Schedule([this, display, token, pause_sequence,
                              message = std::string(text->valuestring), glyphs = std::move(glyphs),
                              bpp]() {
                        if (!IsConversationEventCurrent(token, pause_sequence)) {
                            return;
                        }
                        EndWaitingResponseTurn();
                        display->AddTextGlyphs(glyphs, bpp);
                        display->SetChatMessage("assistant", message.c_str());
                    });
                }
            }
        } else if (strcmp(type->valuestring, "stt") == 0) {
            auto text = cJSON_GetObjectItem(root, "text");
            if (cJSON_IsString(text)) {
                std::vector<TextGlyph> glyphs;
                uint8_t bpp = 0;
                if (!TextGlyphPayload::Parse(root, glyphs, bpp)) {
                    glyphs.clear();
                }
                ESP_LOGI(TAG, ">> %s", text->valuestring);
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
                const auto media_speech = media_speech_sequence_.load();
#else
                constexpr uint64_t media_speech = 0;
#endif
#if CONFIG_WAVESHARE_1_85C_HOME
                const auto waiting_turn = waiting_turn_.load();
#else
                constexpr uint64_t waiting_turn = 0;
#endif
                Schedule([this, display, token, pause_sequence,
                          message = std::string(text->valuestring), glyphs = std::move(glyphs), bpp,
                          waiting_turn, media_speech]() {
                    if (!IsConversationEventCurrent(token, pause_sequence)) {
                        return;
                    }
                    display->AddTextGlyphs(glyphs, bpp);
                    display->SetChatMessage("user", message.c_str());
                    HandleWaitingRecognition(waiting_turn, !message.empty());
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
                    RecordMediaRecognition(token, media_speech, !message.empty());
#endif
                });
            }
        } else if (strcmp(type->valuestring, "llm") == 0) {
            auto emotion = cJSON_GetObjectItem(root, "emotion");
            if (cJSON_IsString(emotion)) {
                Schedule([this, display, token, pause_sequence,
                          emotion_str = std::string(emotion->valuestring)]() {
                    if (!IsConversationEventCurrent(token, pause_sequence)) {
                        return;
                    }
                    display->SetEmotion(emotion_str.c_str());
                });
            }
        } else if (strcmp(type->valuestring, "mcp") == 0) {
            auto payload = cJSON_GetObjectItem(root, "payload");
            if (cJSON_IsObject(payload)) {
                McpServer::GetInstance().ParseMessage(payload);
            }
        } else if (strcmp(type->valuestring, "system") == 0) {
            auto command = cJSON_GetObjectItem(root, "command");
            if (cJSON_IsString(command)) {
                ESP_LOGI(TAG, "System command: %s", command->valuestring);
                if (strcmp(command->valuestring, "reboot") == 0) {
                    // Do a reboot if user requests a OTA update
                    Schedule([this]() { Reboot(); });
                } else {
                    ESP_LOGW(TAG, "Unknown system command: %s", command->valuestring);
                }
            }
        } else if (strcmp(type->valuestring, "alert") == 0) {
            auto status = cJSON_GetObjectItem(root, "status");
            auto message = cJSON_GetObjectItem(root, "message");
            auto emotion = cJSON_GetObjectItem(root, "emotion");
            if (cJSON_IsString(status) && cJSON_IsString(message) && cJSON_IsString(emotion)) {
                Alert(status->valuestring, message->valuestring, emotion->valuestring,
                      Lang::Sounds::OGG_VIBRATION);
            } else {
                ESP_LOGW(TAG, "Alert command requires status, message and emotion");
            }
#if CONFIG_RECEIVE_CUSTOM_MESSAGE
        } else if (strcmp(type->valuestring, "custom") == 0) {
            auto payload = cJSON_GetObjectItem(root, "payload");
            CJsonStringUniquePtr root_json(cJSON_PrintUnformatted(root));
            ESP_LOGI(TAG, "Received custom message: %s", root_json ? root_json.get() : "");
            if (cJSON_IsObject(payload)) {
                CJsonStringUniquePtr payload_json(cJSON_PrintUnformatted(payload));
                if (payload_json) {
                    Schedule([this, display, payload_str = std::string(payload_json.get())]() {
                        display->SetChatMessage("system", payload_str.c_str());
                    });
                }
            } else {
                ESP_LOGW(TAG, "Invalid custom message format: missing payload");
            }
#endif
        } else {
            ESP_LOGW(TAG, "Unknown message type: %s", type->valuestring);
        }
    });

    protocol_->Start();
}

void Application::ShowActivationCode(const std::string& code, const std::string& message) {
    struct digit_sound {
        char digit;
        const std::string_view& sound;
    };
    static const std::array<digit_sound, 10> digit_sounds{
        {digit_sound{'0', Lang::Sounds::OGG_0}, digit_sound{'1', Lang::Sounds::OGG_1},
         digit_sound{'2', Lang::Sounds::OGG_2}, digit_sound{'3', Lang::Sounds::OGG_3},
         digit_sound{'4', Lang::Sounds::OGG_4}, digit_sound{'5', Lang::Sounds::OGG_5},
         digit_sound{'6', Lang::Sounds::OGG_6}, digit_sound{'7', Lang::Sounds::OGG_7},
         digit_sound{'8', Lang::Sounds::OGG_8}, digit_sound{'9', Lang::Sounds::OGG_9}}};

    // This sentence uses 9KB of SRAM, so we need to wait for it to finish
    Alert(Lang::Strings::ACTIVATION, message.c_str(), "link", Lang::Sounds::OGG_ACTIVATION);

    for (const auto& digit : code) {
        auto it = std::find_if(digit_sounds.begin(), digit_sounds.end(),
                               [digit](const digit_sound& ds) { return ds.digit == digit; });
        if (it != digit_sounds.end()) {
            audio_service_.PlaySound(it->sound);
        }
    }
}

void Application::Alert(const char* status, const char* message, const char* emotion,
                        const std::string_view& sound) {
    ESP_LOGW(TAG, "Alert [%s] %s: %s", emotion, status, message);
    auto display = Board::GetInstance().GetDisplay();
    display->SetStatus(IsListeningPaused() ? Lang::Strings::LISTENING_PAUSED : status);
    display->SetEmotion(emotion);
    display->SetChatMessage("system", message);
    if (!sound.empty()) {
        audio_service_.PlaySound(sound);
    }
}

void Application::DismissAlert() {
    last_error_message_.clear();
    if (GetDeviceState() == kDeviceStateIdle) {
        auto display = Board::GetInstance().GetDisplay();
        display->SetStatus(IsListeningPaused() ? Lang::Strings::LISTENING_PAUSED
                                               : Lang::Strings::STANDBY);
        display->SetEmotion("neutral");
        display->SetChatMessage("system", "");
    }
}

void Application::ToggleChatState() {
    if (!IsListeningPaused()) {
        xEventGroupSetBits(event_group_, MAIN_EVENT_TOGGLE_CHAT);
    }
}

void Application::PrimaryButtonPressed() {
#if CONFIG_WAVESHARE_1_85C_HOME
    if (IsListeningPaused()) {
        return;
    }
    primary_button_sequence_.store(ListeningPauseSequence());
    ESP_LOGI(TAG, "Primary button requested");
    xEventGroupSetBits(event_group_, MAIN_EVENT_PRIMARY_BUTTON);
#endif
}

void Application::ToggleListeningPause() {
#if CONFIG_WAVESHARE_1_85C_HOME
    auto state = GetDeviceState();
    if (state != kDeviceStateIdle && state != kDeviceStateConnecting &&
        state != kDeviceStateListening && state != kDeviceStateSpeaking &&
        state != kDeviceStateNotifying) {
        return;
    }
    [[maybe_unused]] const auto request = pause_requests_.fetch_add(1) + 1;
    ESP_LOGI(TAG, "Listening pause requested (sequence %lu)", static_cast<unsigned long>(request));
    xEventGroupSetBits(event_group_, MAIN_EVENT_LISTENING_PAUSE);
#endif
}

bool Application::IsListeningPaused() const {
#if CONFIG_WAVESHARE_1_85C_HOME
    const auto requests = pause_requests_.load();
    return requests != processed_pause_requests_.load() || (requests & 1U) != 0 ||
           listening_paused_.load();
#else
    return false;
#endif
}

uint32_t Application::ListeningPauseSequence() const {
#if CONFIG_WAVESHARE_1_85C_HOME
    return pause_requests_.load();
#else
    return 0;
#endif
}

uint32_t Application::ConversationIntentSequence() const {
#if CONFIG_WAVESHARE_1_85C_HOME
    return conversation_intent_.load();
#else
    return 0;
#endif
}

bool Application::IsConversationEventCurrent(Protocol::ChannelToken token,
                                             uint32_t pause_sequence) const {
#if CONFIG_WAVESHARE_1_85C_HOME
    return conversation_events_enabled_.load() && !IsListeningPaused() &&
           pause_sequence == ListeningPauseSequence() && token != 0 &&
           token == accepted_channel_token_.load();
#else
    return true;
#endif
}

bool Application::AcceptConversationChannel() {
#if CONFIG_WAVESHARE_1_85C_HOME
    std::lock_guard<std::mutex> lock(audio_receive_mutex_);
    if (IsListeningPaused() || !protocol_ || !protocol_->IsAudioChannelOpened()) {
        return false;
    }
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
    if (!wifi_link_connected_.load()) {
        return false;
    }
    // OpenAudioChannel returns only after a valid hello. Apply that synchronous
    // evidence before deciding whether a pending prompt may delay this listen.
    const auto token = protocol_->channel_token();
    handshake_source_token_ = token;
    connection_health_.BeginAttempt(ConnectionHealth::Stage::Handshake, token);
    connection_health_.ReportSuccess(ConnectionHealth::Stage::Handshake, token);
    connection_service_state_.store(connection_health_.GetServiceState());
#endif
    accepted_channel_token_.store(protocol_->channel_token());
    accepted_channel_intent_.store(ConversationIntentSequence());
    conversation_events_enabled_.store(true);
#endif
    return true;
}

bool Application::ApplyPendingListeningPause() {
#if CONFIG_WAVESHARE_1_85C_HOME
    const auto requests = pause_requests_.load();
    if (requests == processed_pause_requests_) {
        return false;
    }
    const bool paused = (requests & 1U) != 0;
    listening_paused_.store(paused);
#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS
    CancelLocalPrompt(false);
#endif
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
    RetireConversationHealth();
#endif
    conversation_intent_.fetch_add(1);
    {
        std::lock_guard<std::mutex> lock(audio_receive_mutex_);
        conversation_events_enabled_.store(false);
        audio_service_.ResetDecoder();
    }
    audio_service_.SetInputPaused(true);
    xEventGroupClearBits(event_group_, MAIN_EVENT_WAKE_WORD_DETECTED | MAIN_EVENT_START_LISTENING |
                                           MAIN_EVENT_TOGGLE_CHAT | MAIN_EVENT_PRIMARY_BUTTON |
                                           MAIN_EVENT_VAD_CHANGE);
    pending_listening_start_ = false;
    play_popup_on_listening_ = false;
    if (GetDeviceState() == kDeviceStateNotifying) {
        StopNotification();
    }
    if (protocol_) {
        protocol_->CloseAudioChannel();
    }
    Board::GetInstance().SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);
    SetDeviceState(kDeviceStateIdle);
    if (!paused) {
        audio_service_.SetInputPaused(false);
    }
    processed_pause_requests_.store(requests);
    audio_service_.EnableWakeWordDetection(!paused);
    Board::GetInstance().GetDisplay()->SetStatus(paused ? Lang::Strings::LISTENING_PAUSED
                                                        : Lang::Strings::STANDBY);
    ESP_LOGI(TAG, "Listening pause %s (request %lu)", paused ? "enabled" : "disabled",
             static_cast<unsigned long>(requests));
    return true;
#else
    return false;
#endif
}

void Application::HandlePrimaryButtonEvent() {
#if CONFIG_WAVESHARE_1_85C_HOME
    if (IsListeningPaused() || primary_button_sequence_.load() != ListeningPauseSequence()) {
        return;
    }
    const auto state = GetDeviceState();
    ESP_LOGI(TAG, "Primary button handled (state %d)", static_cast<int>(state));
#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS
    if (state == kDeviceStateIdle || state == kDeviceStateSpeaking ||
        state == kDeviceStateNotifying) {
        CancelLocalPrompt(false);
    }
#endif
    if (state == kDeviceStateSpeaking || state == kDeviceStateNotifying) {
        {
            std::lock_guard<std::mutex> lock(audio_receive_mutex_);
            conversation_events_enabled_.store(false);
            audio_service_.ResetDecoder();
        }
        audio_service_.SetInputPaused(true);
        pending_listening_start_ = false;
        if (state == kDeviceStateNotifying) {
            StopNotification();
        }
        if (protocol_) {
            AbortSpeaking(kAbortReasonNone);
            protocol_->CloseAudioChannel();
        }
        SetDeviceState(kDeviceStateIdle);
        audio_service_.SetInputPaused(false);
        HandleToggleChatEvent();
    } else if (state == kDeviceStateIdle) {
        HandleToggleChatEvent();
    }
#endif
}

void Application::StartListening() {
    if (!IsListeningPaused()) {
        xEventGroupSetBits(event_group_, MAIN_EVENT_START_LISTENING);
    }
}

void Application::StopListening() { xEventGroupSetBits(event_group_, MAIN_EVENT_STOP_LISTENING); }

void Application::HandleToggleChatEvent() {
    if (IsListeningPaused()) {
        return;
    }
    auto state = GetDeviceState();
#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS
    if (state == kDeviceStateIdle || state == kDeviceStateSpeaking ||
        state == kDeviceStateNotifying) {
        CancelLocalPrompt(false);
    }
#endif

    if (state == kDeviceStateNotifying) {
        StopNotification();
        state = kDeviceStateIdle;
    }

    if (state == kDeviceStateActivating) {
        SetDeviceState(kDeviceStateIdle);
        return;
    } else if (state == kDeviceStateWifiConfiguring) {
        audio_service_.EnableAudioTesting(true);
        SetDeviceState(kDeviceStateAudioTesting);
        return;
    } else if (state == kDeviceStateAudioTesting) {
        audio_service_.EnableAudioTesting(false);
        SetDeviceState(kDeviceStateWifiConfiguring);
        return;
    }

    if (!protocol_) {
        ESP_LOGE(TAG, "Protocol not initialized");
        return;
    }

    if (state == kDeviceStateIdle) {
#if CONFIG_WAVESHARE_1_85C_HOME
        conversation_intent_.fetch_add(1);
#endif
        ListeningMode mode = GetDefaultListeningMode();
        if (!protocol_->IsAudioChannelOpened()) {
            SetDeviceState(kDeviceStateConnecting);
            // Schedule to let the state change be processed first (UI update)
            Schedule([this, mode]() { ContinueOpenAudioChannel(mode); });
            return;
        }
        SetListeningMode(mode);
    } else if (state == kDeviceStateSpeaking) {
        AbortSpeaking(kAbortReasonNone);
    } else if (state == kDeviceStateListening) {
        protocol_->CloseAudioChannel();
    }
}

void Application::ContinueOpenAudioChannel(ListeningMode mode) {
    if (ApplyPendingListeningPause() || IsListeningPaused()) {
        return;
    }
    // Check state again in case it was changed during scheduling
    if (GetDeviceState() != kDeviceStateConnecting) {
        return;
    }

    // Switch to performance mode before connecting to reduce latency
    auto& board = Board::GetInstance();
    board.SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);

    if (!protocol_->IsAudioChannelOpened()) {
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
        if (!PrepareConnectionOpen()) {
            return;
        }
#endif
        if (!protocol_->OpenAudioChannel()) {
            // Return to idle so the device is not stuck in the connecting
            // state (not every failure path reports a network error)
            SetDeviceState(kDeviceStateIdle);
            return;
        }
    }

    if (ApplyPendingListeningPause() || IsListeningPaused() ||
        GetDeviceState() != kDeviceStateConnecting) {
        return;
    }
    SetListeningMode(mode);
}

void Application::HandleStartListeningEvent() {
    if (IsListeningPaused()) {
        return;
    }
    auto state = GetDeviceState();
#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS
    if (state == kDeviceStateIdle || state == kDeviceStateSpeaking ||
        state == kDeviceStateNotifying) {
        CancelLocalPrompt(false);
    }
#endif

    if (state == kDeviceStateNotifying) {
        StopNotification();
        state = kDeviceStateIdle;
    }

    if (state == kDeviceStateActivating) {
        SetDeviceState(kDeviceStateIdle);
        return;
    } else if (state == kDeviceStateWifiConfiguring) {
        audio_service_.EnableAudioTesting(true);
        SetDeviceState(kDeviceStateAudioTesting);
        return;
    }

    if (!protocol_) {
        ESP_LOGE(TAG, "Protocol not initialized");
        return;
    }

    if (state == kDeviceStateIdle) {
#if CONFIG_WAVESHARE_1_85C_HOME
        conversation_intent_.fetch_add(1);
#endif
        if (!protocol_->IsAudioChannelOpened()) {
            SetDeviceState(kDeviceStateConnecting);
            // Schedule to let the state change be processed first (UI update)
            Schedule([this]() { ContinueOpenAudioChannel(kListeningModeManualStop); });
            return;
        }
        SetListeningMode(kListeningModeManualStop);
    } else if (state == kDeviceStateSpeaking) {
        AbortSpeaking(kAbortReasonNone);
        SetListeningMode(kListeningModeManualStop);
    }
}

void Application::HandleStopListeningEvent() {
    auto state = GetDeviceState();

    if (state == kDeviceStateNotifying) {
        StopNotification();
    } else if (state == kDeviceStateAudioTesting) {
        audio_service_.EnableAudioTesting(false);
        SetDeviceState(kDeviceStateWifiConfiguring);
        return;
    } else if (state == kDeviceStateListening) {
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
        media_decode_request_.store(0);
        media_speech_token_.store(0);
        media_stt_sequence_ = 0;
#endif
        if (protocol_) {
            protocol_->SendStopListening();
        }
        SetDeviceState(kDeviceStateIdle);
    }
}

void Application::HandleWakeWordDetectedEvent() {
    if (IsListeningPaused()) {
        return;
    }
    if (!protocol_) {
        return;
    }

    auto state = GetDeviceState();
    auto wake_word = audio_service_.GetLastWakeWord();
    ESP_LOGI(TAG, "Wake word detected: %s (state: %d)", wake_word.c_str(), (int)state);
#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS
    if (state == kDeviceStateListening && local_prompt_.id != 0) {
        CancelLocalPrompt(false);
        pending_listening_start_ = false;
        EndWaitingResponseTurn();
        SetListeningMode(GetDefaultListeningMode());
        StartListeningAudio();
        return;
    }
#endif

    if (state == kDeviceStateIdle) {
        BeginWakeWordInvoke(wake_word);
    } else if (state == kDeviceStateNotifying) {
        StopNotification();
        BeginWakeWordInvoke(wake_word);
    } else if (state == kDeviceStateSpeaking || state == kDeviceStateListening) {
        AbortSpeaking(kAbortReasonWakeWordDetected);
        // Clear send queue to avoid sending residues to server
        while (audio_service_.PopPacketFromSendQueue())
            ;

        if (state == kDeviceStateListening) {
            EndWaitingResponseTurn();
            protocol_->SendStartListening(GetDefaultListeningMode());
            audio_service_.ResetDecoder();
            audio_service_.PlaySound(Lang::Sounds::OGG_POPUP);
            // Re-enable wake word detection as it was stopped by the detection itself
            audio_service_.EnableWakeWordDetection(true);
            BeginWaitingResponseTurn();
        } else {
            // Play popup sound and start listening again
            play_popup_on_listening_ = true;
            SetListeningMode(GetDefaultListeningMode());
        }
    } else if (state == kDeviceStateActivating) {
        // Restart the activation check if the wake word is detected during activation
        SetDeviceState(kDeviceStateIdle);
    }
}

void Application::BeginWakeWordInvoke(const std::string& wake_word) {
    if (IsListeningPaused()) {
        return;
    }
#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS
    CancelLocalPrompt(false);
#endif
#if CONFIG_WAVESHARE_1_85C_HOME
    conversation_intent_.fetch_add(1);
#endif
    // Must run in the main task with the device in idle state
    audio_service_.EncodeWakeWord();

    // Always pass through the connecting state, even if the audio channel is
    // already opened. ContinueWakeWordInvoke() rejects any other state, so
    // skipping this transition would silently drop the wake word invocation.
    if (!SetDeviceState(kDeviceStateConnecting)) {
        // Wake word detection was stopped by the detection itself; restore it
        // so the device does not become unresponsive to wake words.
        audio_service_.EnableWakeWordDetection(true);
        return;
    }

    if (!protocol_->IsAudioChannelOpened()) {
        // Schedule to let the state change be processed first (UI update),
        // then continue with OpenAudioChannel which may block for ~1 second
        Schedule([this, wake_word]() { ContinueWakeWordInvoke(wake_word); });
        return;
    }
    // Channel already opened, continue directly
    ContinueWakeWordInvoke(wake_word);
}

void Application::ContinueWakeWordInvoke(const std::string& wake_word) {
    if (ApplyPendingListeningPause() || IsListeningPaused()) {
        return;
    }
    // Check state again in case it was changed during scheduling
    if (GetDeviceState() != kDeviceStateConnecting) {
        return;
    }

    // Switch to performance mode before connecting to reduce latency
    auto& board = Board::GetInstance();
    board.SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);

    if (!protocol_->IsAudioChannelOpened()) {
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
        if (!PrepareConnectionOpen()) {
            return;
        }
#endif
        if (!protocol_->OpenAudioChannel()) {
            // Return to idle so the device is not stuck in the connecting
            // state (not every failure path reports a network error), and
            // wake word detection is re-enabled by the idle state handler.
            SetDeviceState(kDeviceStateIdle);
            return;
        }
    }

    if (ApplyPendingListeningPause() || IsListeningPaused() ||
        GetDeviceState() != kDeviceStateConnecting) {
        return;
    }
    if (!AcceptConversationChannel()) {
        SetDeviceState(kDeviceStateIdle);
        return;
    }
    ESP_LOGI(TAG, "Wake word detected: %s", wake_word.c_str());
#if CONFIG_SEND_WAKE_WORD_DATA
    // Encode and send the wake word data to the server
    while (auto packet = audio_service_.PopWakeWordPacket()) {
        if (ApplyPendingListeningPause() || IsListeningPaused() ||
            GetDeviceState() != kDeviceStateConnecting) {
            return;
        }
        protocol_->SendAudio(std::move(packet));
    }
    if (ApplyPendingListeningPause() || IsListeningPaused() ||
        GetDeviceState() != kDeviceStateConnecting) {
        return;
    }
    // Set the chat state to wake word detected
    protocol_->SendWakeWordDetected(wake_word);
    SetListeningMode(GetDefaultListeningMode());
#else
    // Set flag to play popup sound after state changes to listening
    // (PlaySound here would be cleared by ResetDecoder in EnableVoiceProcessing)
    play_popup_on_listening_ = true;
    SetListeningMode(GetDefaultListeningMode());
#endif
}

void Application::HandleStateChangedEvent() {
    DeviceState new_state = state_machine_.GetState();
    clock_ticks_ = 0;
    // Any state change invalidates a pending deferred listening start;
    // the Listening case below re-arms it when needed.
    pending_listening_start_ = false;
#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS
    if (new_state != kDeviceStateIdle && new_state != kDeviceStateListening &&
        new_state != kDeviceStateActivating) {
        CancelLocalPrompt(false);
    }
#endif
#if CONFIG_WAVESHARE_1_85C_HOME
    waiting_response_.Cancel();
    waiting_turn_open_.store(false);
#endif

    auto& board = Board::GetInstance();
    auto display = board.GetDisplay();
    auto led = board.GetLed();
    led->OnStateChanged();

    switch (new_state) {
        case kDeviceStateUnknown:
        case kDeviceStateIdle:
            // Keep a just-raised network error visible. SetDeviceState(idle)
            // queues STATE_CHANGED after Alert(), and the idle handler would
            // otherwise wipe the status, emotion, and chat message.
            if (last_error_message_.empty()) {
                display->SetStatus(Lang::Strings::STANDBY);
                display->ClearChatMessages();  // Clear messages first
                display->SetEmotion(
                    "neutral");  // Then set emotion (wechat mode checks child count)
            }
            audio_service_.EnableVoiceProcessing(false);
            audio_service_.EnableWakeWordDetection(true);
            break;
        case kDeviceStateConnecting:
            display->SetStatus(Lang::Strings::CONNECTING);
            display->SetEmotion("neutral");
            display->SetChatMessage("system", "");
            break;
        case kDeviceStateListening:
            display->SetStatus(Lang::Strings::LISTENING);
            display->SetEmotion("neutral");

            // Make sure the audio processor is running
            if (play_popup_on_listening_ || !audio_service_.IsAudioProcessorRunning()) {
                // For auto mode, wait for the playback queue to drain before enabling
                // voice processing. This prevents audio truncation when STOP arrives
                // late due to network jitter. Instead of blocking the main loop here,
                // defer the start until MAIN_EVENT_PLAYBACK_DRAINED arrives.
                if (listening_mode_ == kListeningModeAutoStop && !audio_service_.IsPlaybackIdle()) {
                    pending_listening_start_ = true;
                } else {
                    StartListeningAudio();
                }
            } else {
                ConfigureWakeWordForListening();
                BeginWaitingResponseTurn();
            }
            break;
        case kDeviceStateSpeaking:
            display->SetStatus(Lang::Strings::SPEAKING);

            if (listening_mode_ != kListeningModeRealtime) {
                audio_service_.EnableVoiceProcessing(false);
                // Only AFE wake word can be detected in speaking mode
                audio_service_.EnableWakeWordDetection(audio_service_.IsAfeWakeWord());
            }
            audio_service_.ResetDecoder();
            break;
        case kDeviceStateNotifying:
            display->SetStatus(Lang::Strings::SPEAKING);
            audio_service_.EnableVoiceProcessing(false);
            audio_service_.EnableWakeWordDetection(audio_service_.IsAfeWakeWord());
            break;
        case kDeviceStateWifiConfiguring:
            audio_service_.EnableVoiceProcessing(false);
            audio_service_.EnableWakeWordDetection(false);
            break;
        default:
            // Do nothing
            break;
    }
    if (IsListeningPaused() && new_state == kDeviceStateIdle) {
        display->SetStatus(Lang::Strings::LISTENING_PAUSED);
    }
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
    RefreshConnectionStatus();
#endif
}

void Application::ShowStartupWakeWordHint() {
    // A coalesced activation/state event can queue another idle UI cleanup.
    // Wait for it before adding the hint, or ClearChatMessages() would erase it.
    if (!pending_startup_wake_word_hint_ ||
        (xEventGroupGetBits(event_group_) & MAIN_EVENT_STATE_CHANGED)) {
        return;
    }
    pending_startup_wake_word_hint_ = false;
    const auto state = GetDeviceState();
    const bool wake_word_running = audio_service_.IsWakeWordRunning();
    if (state != kDeviceStateIdle || !last_error_message_.empty() || !wake_word_running) {
        ESP_LOGI(TAG, "Startup wake word hint skipped (state=%d, wake word running=%d, error=%d)",
                 static_cast<int>(state), wake_word_running, !last_error_message_.empty());
        return;
    }

    const auto wake_words = audio_service_.GetWakeWords();
    std::string words;
    for (auto it = wake_words.begin(); it != wake_words.end(); ++it) {
        if (it->empty() || std::find(wake_words.begin(), it, *it) != it) {
            continue;
        }
        if (!words.empty()) {
            words += " / ";
        }
        words += *it;
    }
    if (words.empty()) {
        ESP_LOGW(TAG, "Startup wake word hint skipped: no available wake words");
        return;
    }
    std::string message = Lang::Strings::WAKE_WORD_HINT;
    const auto placeholder = message.find("%s");
    if (placeholder == std::string::npos) {
        ESP_LOGE(TAG, "Startup wake word hint skipped: invalid message template");
        return;
    }
    message.replace(placeholder, 2, words);
    if (startup_wake_word_hint_timer_ == nullptr) {
        esp_timer_create_args_t timer_args = {
            .callback =
                [](void* arg) {
                    auto app = static_cast<Application*>(arg);
                    xEventGroupSetBits(app->event_group_, MAIN_EVENT_STARTUP_HINT_TIMEOUT);
                },
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "startup_hint",
            .skip_unhandled_events = false,
        };
        ESP_ERROR_CHECK(esp_timer_create(&timer_args, &startup_wake_word_hint_timer_));
    }
    ESP_LOGI(TAG, "Startup wake word hint: %s", message.c_str());
    Board::GetInstance().GetDisplay()->SetChatMessage("assistant", message.c_str());
    startup_wake_word_hint_message_ = std::move(message);
    ESP_ERROR_CHECK(esp_timer_start_once(startup_wake_word_hint_timer_, 5000 * 1000));
}

void Application::HideStartupWakeWordHint() {
    if (startup_wake_word_hint_message_.empty()) {
        return;
    }
    // A conversation or alert may have replaced the hint while its timer was running.
    Board::GetInstance().GetDisplay()->RemoveChatMessage("assistant",
                                                         startup_wake_word_hint_message_.c_str());
    startup_wake_word_hint_message_.clear();
}

void Application::StartListeningAudio() {
    // Runs in the main loop, either directly from HandleStateChangedEvent or
    // deferred via MAIN_EVENT_PLAYBACK_DRAINED once the playback queue drains.
    if (IsListeningPaused() || GetDeviceState() != kDeviceStateListening) {
        return;
    }
#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS
    if (local_prompt_gap_allowed_ && TryStartPendingLocalPrompt(true)) {
        return;
    }
    local_prompt_gap_allowed_ = false;
#endif

    // Send the start listening command
    protocol_->SendStartListening(listening_mode_);
    audio_service_.EnableVoiceProcessing(true);
    BeginWaitingResponseTurn();

    ConfigureWakeWordForListening();

    // Play popup sound after ResetDecoder (in EnableVoiceProcessing) has been called
    if (play_popup_on_listening_) {
        play_popup_on_listening_ = false;
        audio_service_.PlaySound(Lang::Sounds::OGG_POPUP);
    }
}

void Application::BeginWaitingResponseTurn() {
#if CONFIG_WAVESHARE_1_85C_HOME
    if (GetDeviceState() != kDeviceStateListening || IsListeningPaused() ||
        !conversation_events_enabled_.load()) {
        return;
    }
    waiting_response_turn_ = waiting_turn_.load();
    waiting_response_.Begin(waiting_response_turn_);
    waiting_turn_open_.store(true);
#endif
}

void Application::EndWaitingResponseTurn() {
#if CONFIG_WAVESHARE_1_85C_HOME
    const bool was_waiting = waiting_response_.waiting();
    waiting_turn_open_.store(false);
    waiting_turn_.fetch_add(1);
    waiting_response_.Cancel();
    if (was_waiting && GetDeviceState() == kDeviceStateListening && !IsListeningPaused()) {
        Board::GetInstance().GetDisplay()->SetStatus(Lang::Strings::LISTENING);
    }
#endif
}

void Application::HandleWaitingSpeech(uint64_t turn, Protocol::ChannelToken token,
                                      uint32_t pause_sequence) {
#if CONFIG_WAVESHARE_1_85C_HOME
    if (waiting_turn_open_.load() && turn == waiting_turn_.load() &&
        GetDeviceState() == kDeviceStateListening &&
        IsConversationEventCurrent(token, pause_sequence)) {
        waiting_response_.Speech(turn);
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
        if (token == media_source_token_) {
            media_speech_token_.store(token);
            media_speech_sequence_.fetch_add(1);
        }
#endif
    }
#endif
}

void Application::HandleWaitingRecognition(uint64_t turn, bool nonempty) {
#if CONFIG_WAVESHARE_1_85C_HOME
    if (waiting_turn_open_.load() && turn == waiting_turn_.load() &&
        GetDeviceState() == kDeviceStateListening && !IsListeningPaused() &&
        waiting_response_.Recognized(turn, esp_timer_get_time() / 1000, nonempty)) {
        Board::GetInstance().GetDisplay()->SetStatus(Lang::Strings::WAITING_RESPONSE);
        ESP_LOGI(TAG, "Waiting response shown");
    }
#endif
}

void Application::UpdateWaitingResponse() {
#if CONFIG_WAVESHARE_1_85C_HOME
    if (!waiting_response_.waiting()) {
        return;
    }
    if (GetDeviceState() != kDeviceStateListening || IsListeningPaused() ||
        !conversation_events_enabled_.load()) {
        EndWaitingResponseTurn();
    } else if (waiting_response_.Expire(esp_timer_get_time() / 1000)) {
        Board::GetInstance().GetDisplay()->SetStatus(Lang::Strings::LISTENING);
        ESP_LOGI(TAG, "Waiting response display expired");
    }
#endif
}

void Application::ConfigureWakeWordForListening() {
#ifdef CONFIG_WAKE_WORD_DETECTION_IN_LISTENING
    // Enable wake word detection in listening mode (configured via Kconfig)
    audio_service_.EnableWakeWordDetection(audio_service_.IsAfeWakeWord());
#else
    // Disable wake word detection in listening mode
    audio_service_.EnableWakeWordDetection(false);
#endif
}

void Application::StartNotification(std::string audio_url, std::vector<NotifySubtitle> subtitles) {
#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS
    if (local_prompt_.id != 0) {
        ESP_LOGW(TAG, "Ignoring notify message during a local prompt");
        return;
    }
#endif
    if (IsListeningPaused() || GetDeviceState() != kDeviceStateIdle || notify_player_.IsBusy()) {
        ESP_LOGW(TAG, "Ignoring notify message while device is busy");
        return;
    }

    auto& board = Board::GetInstance();
    board.SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);
    audio_service_.EnableVoiceProcessing(false);
    audio_service_.EnableWakeWordDetection(audio_service_.IsAfeWakeWord());
    audio_service_.ReleaseWakeWordResources();
    while (audio_service_.PopPacketFromSendQueue()) {
        // Discard microphone audio left over from a previous conversation.
    }

    if (!SetDeviceState(kDeviceStateNotifying)) {
        board.SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);
        return;
    }

    audio_service_.ResetDecoder();
#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS
    uint32_t playback_id = AllocatePlaybackId();
    notification_playback_id_ = playback_id;
#else
    uint32_t playback_id = ++notification_playback_id_;
    if (playback_id == 0) {
        playback_id = ++notification_playback_id_;
    }
#endif
    audio_service_.PlaySound(Lang::Sounds::OGG_POPUP);

    bool started = notify_player_.Start(
        std::move(audio_url), std::move(subtitles), playback_id,
        [this](uint32_t id, const std::string& text) {
            Schedule([this, id, text]() {
                if (GetDeviceState() == kDeviceStateNotifying && notification_playback_id_ == id) {
                    Board::GetInstance().GetDisplay()->SetChatMessage("assistant", text.c_str());
                }
            });
        },
        [this](uint32_t id, bool success) {
            Schedule([this, id, success]() { HandleNotificationFinished(id, success); });
        });

    if (!started) {
        ESP_LOGE(TAG, "Failed to start notification playback");
        StopNotification();
    }
}

void Application::StopNotification() {
    notify_player_.Stop();
    audio_service_.ResetDecoder();
    auto& board = Board::GetInstance();
    board.GetDisplay()->SetChatMessage("assistant", "");
    board.SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);
    if (GetDeviceState() == kDeviceStateNotifying) {
        SetDeviceState(kDeviceStateIdle);
    }
}

void Application::HandleNotificationFinished(uint32_t playback_id, bool success) {
    if (GetDeviceState() != kDeviceStateNotifying || notification_playback_id_ != playback_id) {
        return;
    }
    ESP_LOGI(TAG, "Notification playback %lu %s", static_cast<unsigned long>(playback_id),
             success ? "completed" : "failed");
    StopNotification();
}

void Application::Schedule(std::function<void()>&& callback) {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        main_tasks_.push_back(std::move(callback));
    }
    xEventGroupSetBits(event_group_, MAIN_EVENT_SCHEDULE);
}

void Application::AbortSpeaking(AbortReason reason) {
    ESP_LOGI(TAG, "Abort speaking");
    aborted_ = true;
    if (protocol_) {
        protocol_->SendAbortSpeaking(reason);
    }
}

void Application::SetListeningMode(ListeningMode mode) {
    if (IsListeningPaused()) {
        return;
    }
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
    media_decode_request_.store(0);
    media_speech_token_.store(0);
    media_stt_sequence_ = 0;
#endif
    if (!AcceptConversationChannel()) {
        SetDeviceState(kDeviceStateIdle);
        return;
    }
#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS
    // This path is an explicit user start/restart. Only a natural TTS stop
    // opens the optional reminder gap before the next automatic listen.
    local_prompt_gap_allowed_ = false;
#endif
    listening_mode_ = mode;
    SetDeviceState(kDeviceStateListening);
}

ListeningMode Application::GetDefaultListeningMode() const {
    return aec_mode_ == kAecOff ? kListeningModeAutoStop : kListeningModeRealtime;
}

void Application::Reboot() {
    ESP_LOGI(TAG, "Rebooting...");
#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS
    CancelLocalPrompt(false);
#endif
    if (GetDeviceState() == kDeviceStateNotifying) {
        StopNotification();
    }
    // Disconnect the audio channel
    if (protocol_ && protocol_->IsAudioChannelOpened()) {
        protocol_->CloseAudioChannel();
    }
    protocol_.reset();
    audio_service_.Stop();

    vTaskDelay(pdMS_TO_TICKS(1000));
    esp_restart();
}

bool Application::UpgradeFirmware(const std::string& url, const std::string& version) {
#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS
    CancelLocalPrompt(false);
#endif
    auto& board = Board::GetInstance();
    auto display = board.GetDisplay();

    std::string upgrade_url = url;
    std::string version_info = version.empty() ? "(Manual upgrade)" : version;

    if (GetDeviceState() == kDeviceStateNotifying) {
        StopNotification();
    }

    // Close audio channel if it's open
    if (protocol_ && protocol_->IsAudioChannelOpened()) {
        ESP_LOGI(TAG, "Closing audio channel before firmware upgrade");
        protocol_->CloseAudioChannel();
    }
    ESP_LOGI(TAG, "Starting firmware upgrade from URL: %s", upgrade_url.c_str());

    Alert(Lang::Strings::OTA_UPGRADE, Lang::Strings::UPGRADING, "download",
          Lang::Sounds::OGG_UPGRADE);
    vTaskDelay(pdMS_TO_TICKS(3000));

    SetDeviceState(kDeviceStateUpgrading);

    std::string message = std::string(Lang::Strings::NEW_VERSION) + version_info;
    display->SetChatMessage("system", message.c_str());

    board.SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);
    audio_service_.Stop();
    vTaskDelay(pdMS_TO_TICKS(1000));

    bool upgrade_success = Ota::Upgrade(upgrade_url, [this, display](int progress, size_t speed) {
        char buffer[32];
        snprintf(buffer, sizeof(buffer), "%d%% %uKB/s", progress, speed / 1024);
        Schedule([display, message = std::string(buffer)]() {
            display->SetChatMessage("system", message.c_str());
        });
    });

    if (!upgrade_success) {
        // Upgrade failed, restart audio service and continue running
        ESP_LOGE(TAG,
                 "Firmware upgrade failed, restarting audio service and continuing operation...");
        audio_service_.Start();                              // Restart audio service
        board.SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);  // Restore power save level
        Alert(Lang::Strings::ERROR, Lang::Strings::UPGRADE_FAILED, "cancel",
              Lang::Sounds::OGG_EXCLAMATION);
        vTaskDelay(pdMS_TO_TICKS(3000));
        return false;
    } else {
        // Upgrade success, reboot immediately
        ESP_LOGI(TAG, "Firmware upgrade successful, rebooting...");
        display->SetChatMessage("system", "Upgrade successful, rebooting...");
        vTaskDelay(pdMS_TO_TICKS(1000));  // Brief pause to show message
        Reboot();
        return true;
    }
}

void Application::WakeWordInvoke(const std::string& wake_word) {
    if (IsListeningPaused() || !protocol_) {
        return;
    }

    const auto pause_sequence = ListeningPauseSequence();
    auto state = GetDeviceState();

    if (state == kDeviceStateIdle) {
        // May be called from outside the main task (e.g. board button
        // callbacks), so schedule the invocation instead of running it here
        Schedule([this, wake_word, pause_sequence]() {
            if (!IsListeningPaused() && pause_sequence == ListeningPauseSequence() &&
                GetDeviceState() == kDeviceStateIdle) {
                BeginWakeWordInvoke(wake_word);
            }
        });
    } else if (state == kDeviceStateNotifying) {
        Schedule([this, wake_word, pause_sequence]() {
            if (!IsListeningPaused() && pause_sequence == ListeningPauseSequence() &&
                GetDeviceState() == kDeviceStateNotifying) {
                StopNotification();
                BeginWakeWordInvoke(wake_word);
            }
        });
    } else if (state == kDeviceStateSpeaking) {
        Schedule([this, pause_sequence]() {
            if (!IsListeningPaused() && pause_sequence == ListeningPauseSequence()) {
                AbortSpeaking(kAbortReasonNone);
            }
        });
    } else if (state == kDeviceStateListening) {
        Schedule([this, pause_sequence]() {
            if (!IsListeningPaused() && pause_sequence == ListeningPauseSequence() && protocol_) {
                protocol_->CloseAudioChannel();
            }
        });
    }
}

bool Application::CanEnterSleepMode() {
    if (GetDeviceState() != kDeviceStateIdle) {
        return false;
    }

    if (protocol_ && protocol_->IsAudioChannelOpened()) {
        return false;
    }

    if (!audio_service_.IsIdle()) {
        return false;
    }

    // Now it is safe to enter sleep mode
    return true;
}

void Application::RegisterMcpBroadcastCallback(std::function<void(const std::string&)> callback) {
    mcp_broadcast_callback_ = std::move(callback);
}

void Application::SendMcpMessage(const std::string& payload) {
    // Always schedule to run in main task for thread safety
    Schedule([this, payload]() {
        if (protocol_) {
            protocol_->SendMcpMessage(payload);
        }
        if (mcp_broadcast_callback_) {
            mcp_broadcast_callback_(payload);
        }
    });
}

void Application::SetAecMode(AecMode mode) {
    aec_mode_ = mode;
    Schedule([this]() {
        auto& board = Board::GetInstance();
        auto display = board.GetDisplay();
        switch (aec_mode_) {
            case kAecOff:
                audio_service_.EnableDeviceAec(false);
                display->ShowNotification(Lang::Strings::RTC_MODE_OFF);
                break;
            case kAecOnServerSide:
                audio_service_.EnableDeviceAec(false);
                display->ShowNotification(Lang::Strings::RTC_MODE_ON);
                break;
            case kAecOnDeviceSide:
                audio_service_.EnableDeviceAec(true);
                display->ShowNotification(Lang::Strings::RTC_MODE_ON);
                break;
        }

        // If the AEC mode is changed, close the audio channel
        if (protocol_ && protocol_->IsAudioChannelOpened()) {
            protocol_->CloseAudioChannel();
        }
    });
}

void Application::PlaySound(const std::string_view& sound) { audio_service_.PlaySound(sound); }

void Application::ResetProtocol() {
    Schedule([this]() {
#if CONFIG_WAVESHARE_1_85C_HOME_LOCAL_ALERTS
        CancelLocalPrompt(false);
#endif
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
        connection_health_.Retire(ConnectionHealth::Stage::Control);
        RetireConversationHealth();
#endif
#if CONFIG_WAVESHARE_1_85C_HOME
        {
            std::lock_guard<std::mutex> lock(audio_receive_mutex_);
            protocol_callback_epoch_.fetch_add(1);
            conversation_events_enabled_.store(false);
            accepted_channel_token_.store(0);
        }
        audio_service_.ResetCapture();
#endif
        if (GetDeviceState() == kDeviceStateNotifying) {
            StopNotification();
        }
        // Close audio channel if opened
        if (protocol_ && protocol_->IsAudioChannelOpened()) {
            protocol_->CloseAudioChannel();
        }
        // Reset protocol
        protocol_.reset();
#if CONFIG_WAVESHARE_1_85C_HOME_NETWORK_ALERTS
        RefreshConnectionStatus();
#endif
#if CONFIG_WAVESHARE_1_85C_HOME
        const auto state = GetDeviceState();
        if (state == kDeviceStateConnecting || state == kDeviceStateListening ||
            state == kDeviceStateSpeaking) {
            SetDeviceState(kDeviceStateIdle);
        }
        if (GetDeviceState() == kDeviceStateIdle) {
            audio_service_.EnableWakeWordDetection(!IsListeningPaused());
        }
#endif
    });
}
