#include "application.h"

#if CONFIG_WAVESHARE_1_85C_HOME_OFFLINE
#include <esp_log.h>
#include <esp_ota_ops.h>
#include <esp_timer.h>

#include "assets.h"
#include "assets/lang_config.h"
#include "board.h"
#include "display.h"
#include "settings.h"

namespace {
uint64_t HomeNow() { return esp_timer_get_time() / 1000; }
struct HomeWork {
    std::function<void()> run;
};
bool RunHomeWork(std::function<void()> run) {
    auto* work = new (std::nothrow) HomeWork{std::move(run)};
    if (!work)
        return false;
    if (xTaskCreate(
            [](void* arg) {
                std::unique_ptr<HomeWork> task(static_cast<HomeWork*>(arg));
                task->run();
                task.reset();
                vTaskDelete(nullptr);
            },
            "home_work", 8192, work, 2, nullptr) != pdPASS) {
        delete work;
        return false;
    }
    return true;
}
}  // namespace

bool Application::HomeAllowsConversation() const {
    return !home_assets_busy_ && !home_menu_open_ && !home_disconnect_pending_ && home_online_ &&
           !home_configuring_;
}

void Application::HomeProtocolFailure(uint32_t epoch, const Protocol::HealthEvent& event) {
    if (epoch != protocol_callback_epoch_.load() || !HomeAllowsConversation() || !protocol_)
        return;
    if (event.stage == Protocol::HealthStage::Handshake &&
        event.outcome == Protocol::HealthOutcome::Started) {
        home_health_channel_ = event.source_channel_token;
        return;
    }
    if (event.outcome != Protocol::HealthOutcome::Failed)
        return;
    // Close advances the transport token before publishing failure for the retired source.
    // Track the most recent handshake identity, not the transport's post-close token.
    if (event.stage != Protocol::HealthStage::Control &&
        (event.source_channel_token == 0 || event.source_channel_token != home_health_channel_))
        return;
    home_online_ = false;
    home_reason_ = "语音服务不可用";
    home_policy_.RecoveryFailed(HomeNow());
    HomeCancelInteraction();
    SetDeviceState(kDeviceStateOffline);
    HomeRestoreStandby();
}

void Application::InitializeHomeOffline() {
    Settings settings("home", false);
    int32_t offline = 0;
    auto err = settings.TryGetInt("offline", offline);
    bool manual = offline == 1;
    if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) {
        manual = true;
        home_reason_ = "离线设置读取失败";
    }
    home_policy_.Start(manual, HomeNow());
    home_front_deadline_ = HomeNow() + 15000;
    esp_ota_mark_app_valid_cancel_rollback();
    esp_timer_create_args_t timer = {};
    timer.callback = [](void* arg) {
        auto* app = static_cast<Application*>(arg);
        app->Schedule([app]() { app->TickHomeOffline(); });
    };
    timer.arg = this;
    timer.name = "home_command";
    esp_timer_create(&timer, &home_command_timer_);
    auto& board = Board::GetInstance();
    board.SetNetworkEnabled(!manual);
    if (!manual && !board.HasNetworkConfiguration()) {
        home_configuring_ = true;
        SetDeviceState(kDeviceStateWifiConfiguring);
        board.RequestWifiConfiguration();
    } else if (manual) {
        SetDeviceState(kDeviceStateOffline);
    }
    HomeRestoreStandby();
}

void Application::HomeRetireProtocol() {
    protocol_callback_epoch_.fetch_add(1);
    auto old = std::move(protocol_);
    if (!old)
        return;
    if (home_async_busy_)
        return;  // In-flight opener owns and closes its retired instance.
    home_async_busy_ = true;
    if (!RunHomeWork([this, old]() {
            old->CloseAudioChannel();
            Schedule([this]() { home_async_busy_ = false; });
        })) {
        protocol_ = std::move(old);
        home_async_busy_ = false;
    }
}

void Application::HomeCancelInteraction() {
    home_pending_conversation_ = false;
    conversation_intent_.fetch_add(1);
    {
        std::lock_guard<std::mutex> lock(audio_receive_mutex_);
        conversation_events_enabled_ = false;
        accepted_channel_token_ = 0;
    }
    pending_listening_start_ = false;
    play_popup_on_listening_ = false;
    EndWaitingResponseTurn();
    home_policy_.CancelCommand();
    if (home_command_timer_)
        esp_timer_stop(home_command_timer_);
    if (!home_assets_busy_) {
        audio_service_.StopLocalCommands();
        audio_service_.SetInputPaused(true);
        audio_service_.ResetDecoder();
        StopNotification();
    }
    HomeRetireProtocol();
}

void Application::HomeRestoreStandby() {
    if (home_assets_busy_)
        return;
    auto* display = Board::GetInstance().GetDisplay();
    if (home_policy_.MenuOpen()) {
        audio_service_.SetInputPaused(true);
        HomeShowMenu();
        return;
    }
    if (home_configuring_) {
        audio_service_.SetInputPaused(true);
        display->SetStatus("配网中，单击 BOOT 离线使用");
        return;
    }
    if (home_policy_.CommandActive()) {
        display->SetStatus("请说指令");
        return;
    }
    const bool paused = IsListeningPaused();
    audio_service_.SetInputPaused(paused);
    audio_service_.EnableVoiceProcessing(false);
    audio_service_.EnableWakeWordDetection(!paused);
    if (paused)
        display->SetStatus(Lang::Strings::LISTENING_PAUSED);
    else if (home_policy_.mode() == HomeOfflinePolicy::Mode::ManualOffline)
        display->SetStatus("手动离线");
    else if (home_policy_.mode() == HomeOfflinePolicy::Mode::AutomaticOffline)
        display->SetStatus(home_reason_.empty() ? "离线待机" : home_reason_.c_str());
    else
        display->SetStatus(Lang::Strings::STANDBY);
}

void Application::HomeSetManualOffline() {
    home_online_ = false;
    home_policy_.ManualOffline();
    home_configuring_ = false;
    home_network_.Invalidate(++home_network_epoch_);
    HomeCancelInteraction();
    Board::GetInstance().SetNetworkEnabled(false);
    Settings settings("home", true);
    if (settings.TrySetIntAndCommit("offline", 1) != ESP_OK) {
        Board::GetInstance().GetDisplay()->ShowNotification("本次已离线，设置保存失败");
    }
    SetDeviceState(kDeviceStateOffline);
    HomeRestoreStandby();
}

bool Application::HandleHomePrimaryButton() {
    if (home_assets_busy_ || GetDeviceState() == kDeviceStateUpgrading)
        return true;
    if (home_policy_.MenuOpen()) {
        home_policy_.NextMenuItem();
        HomeShowMenu();
        return true;
    }
    if (home_configuring_) {
        HomeSetManualOffline();
        return true;
    }
    if (IsListeningPaused()) {
        HomeRestoreStandby();
        return true;
    }
    if (home_policy_.CommandActive()) {
        HomeCancelInteraction();
        SetDeviceState(kDeviceStateOffline);
        HomeRestoreStandby();
    } else if (GetDeviceState() == kDeviceStateListening ||
               GetDeviceState() == kDeviceStateConnecting) {
        HomeCancelInteraction();
        SetDeviceState(kDeviceStateIdle);
        HomeRestoreStandby();
    } else if (home_policy_.mode() == HomeOfflinePolicy::Mode::Online) {
        HomeStartConversation();
    } else {
        HomeStartCommand();
    }
    return true;
}

void Application::NetworkMenuPressed() {
    if (home_assets_busy_ || GetDeviceState() == kDeviceStateUpgrading || home_configuring_)
        return;
    if (home_policy_.MenuOpen()) {
        HomeExecuteMenu();
        return;
    }
    HomeCancelInteraction();
    home_policy_.OpenMenu();
    home_menu_open_ = true;
    if (auto* backlight = Board::GetInstance().GetBacklight())
        backlight->SetDisplayOff(false);
    SetDeviceState(home_policy_.mode() == HomeOfflinePolicy::Mode::Online ? kDeviceStateIdle
                                                                          : kDeviceStateOffline);
    HomeShowMenu();
}

void Application::HomeShowMenu() {
    std::vector<std::string> items;
    switch (home_policy_.mode()) {
        case HomeOfflinePolicy::Mode::Online:
            items = {"离线使用", "进入配网", "返回"};
            break;
        case HomeOfflinePolicy::Mode::AutomaticOffline:
            items = {"立即重试", "保持离线", "进入配网", "返回"};
            break;
        case HomeOfflinePolicy::Mode::ManualOffline:
            items = {"恢复联网", "进入配网", "返回"};
            break;
    }
    Board::GetInstance().GetDisplay()->ShowNetworkMenu(items, home_policy_.Selected());
}

bool Application::HandleHomeTouch(int x, int y) {
    if (!home_policy_.MenuOpen())
        return false;
    int item = Board::GetInstance().GetDisplay()->NetworkMenuHitTest(x, y);
    if (item >= 0 && item < home_policy_.MenuSize()) {
        home_policy_.Select(item);
        HomeExecuteMenu();
    }
    return true;
}

void Application::HomeExecuteMenu() {
    const auto mode = home_policy_.mode();
    int index = home_policy_.Selected();
    const bool back = index == home_policy_.MenuSize() - 1;
    const bool config = index == home_policy_.MenuSize() - 2;
    const bool offline = (mode == HomeOfflinePolicy::Mode::Online && index == 0) ||
                         (mode == HomeOfflinePolicy::Mode::AutomaticOffline && index == 1);
    home_policy_.CloseMenu();
    home_menu_open_ = false;
    Board::GetInstance().GetDisplay()->ShowNetworkMenu({}, 0);
    if (back) {
        HomeRestoreStandby();
        TickHomeOffline();
        return;
    }
    if (offline) {
        HomeSetManualOffline();
        return;
    }
    Settings settings("home", true);
    if (settings.TrySetIntAndCommit("offline", 0) != ESP_OK) {
        Board::GetInstance().GetDisplay()->ShowNotification("联网设置保存失败");
        HomeRestoreStandby();
        return;
    }
    home_network_.Invalidate(++home_network_epoch_);
    home_policy_.AllowNetwork(HomeNow());
    home_reason_ = "正在重试联网";
    if (config) {
        home_configuring_ = true;
        SetDeviceState(kDeviceStateWifiConfiguring);
        if (!Board::GetInstance().RequestWifiConfiguration()) {
            home_configuring_ = false;
            home_reason_ = "配网不可用";
            SetDeviceState(kDeviceStateOffline);
        }
    } else {
        home_configuring_ = false;
        Board::GetInstance().SetNetworkEnabled(true);
        SetDeviceState(kDeviceStateOffline);
    }
    HomeRestoreStandby();
    TickHomeOffline();
}

void Application::HomeStartCommand() {
    if (home_assets_busy_ || home_configuring_ || home_policy_.MenuOpen() || IsListeningPaused())
        return;
    if (!audio_service_.HasLocalCommands()) {
        Board::GetInstance().GetDisplay()->ShowNotification("本地语音不可用");
        HomeRestoreStandby();
        return;
    }
    HomeCancelInteraction();
    SetDeviceState(kDeviceStateOffline);
    const auto generation = home_policy_.BeginCommand(HomeNow());
    audio_service_.SetInputPaused(false);
    if (!audio_service_.StartLocalCommands(generation)) {
        home_policy_.CancelCommand();
        Board::GetInstance().GetDisplay()->ShowNotification("本地语音不可用");
        HomeRestoreStandby();
        return;
    }
    SetDeviceState(kDeviceStateLocalCommand);
    audio_service_.PlaySound(Lang::Sounds::OGG_POPUP);
    if (home_command_timer_)
        esp_timer_start_once(home_command_timer_, 8000000);
    HomeRestoreStandby();
}

void Application::HomeHandleCommand(const std::string& action, uint64_t generation) {
    if (home_assets_busy_ || IsListeningPaused() || home_policy_.MenuOpen() ||
        !home_policy_.AcceptCommand(generation, HomeNow()))
        return;
    home_policy_.CancelCommand();
    audio_service_.StopLocalCommands();
    if (home_command_timer_)
        esp_timer_stop(home_command_timer_);
    auto& board = Board::GetInstance();
    auto* display = board.GetDisplay();
    auto* codec = board.GetAudioCodec();
    auto* backlight = board.GetBacklight();
    std::string feedback = "操作失败";
    if (action == "cancel")
        feedback = "已取消";
    else if ((action == "screen_on" || action == "screen_off") && backlight) {
        if (backlight->SetDisplayOff(action == "screen_off") == ESP_OK)
            feedback = action == "screen_on" ? "已打开屏幕" : "已关闭屏幕";
    } else if ((action == "volume_up" || action == "volume_down") && codec) {
        const int value =
            std::clamp(codec->output_volume() + (action == "volume_up" ? 10 : -10), 0, 100);
        const auto status = value == codec->output_volume() ? codec->GetOutputVolumeStatus()
                                                            : codec->SetOutputVolumeChecked(value);
        if (status.persisted && status.error == ESP_OK)
            feedback = "音量 " + std::to_string(status.volume) +
                       (status.applied ? "" : "（已保存，尚未应用）");
    } else if ((action == "brightness_up" || action == "brightness_down") && backlight) {
        const int value = std::clamp(
            backlight->GetStatus().brightness + (action == "brightness_up" ? 10 : -10), 1, 100);
        const auto previous = backlight->GetStatus();
        const auto status =
            value == previous.brightness ? previous : backlight->SetBrightnessChecked(value);
        if (status.persisted && status.error == ESP_OK)
            feedback = "亮度 " + std::to_string(status.brightness) +
                       (status.display_off ? "（息屏）" : "");
    } else if (action == "device_status") {
        if (backlight)
            backlight->SetDisplayOff(false);
        const auto battery = board.GetBatteryStatus();
        const char* bars = BatteryBandName(battery.band);
        feedback = std::string("电池 ") + (bars ? bars : "未知") + "\n音量 " +
                   (codec ? std::to_string(codec->output_volume()) : "未知") + " 亮度 " +
                   (backlight ? std::to_string(backlight->GetStatus().brightness) : "未知") + "\n" +
                   (home_policy_.mode() == HomeOfflinePolicy::Mode::ManualOffline
                        ? "手动离线"
                        : (home_reason_.empty() ? "服务不可用" : home_reason_));
    }
    SetDeviceState(kDeviceStateOffline);
    display->SetChatMessage("assistant", feedback.c_str());
    if (feedback != "操作失败" && action != "cancel")
        audio_service_.PlaySound(Lang::Sounds::OGG_SUCCESS);
    HomeRestoreStandby();
}

void Application::HomeStartConversation(const std::string& wake_word) {
    if (!HomeAllowsConversation() || IsListeningPaused())
        return;
    if (home_async_busy_) {
        home_pending_conversation_ = true;
        home_pending_wake_ = wake_word;
        SetDeviceState(kDeviceStateIdle);
        SetDeviceState(kDeviceStateConnecting);
        return;
    }
    HomeCancelInteraction();
    if (home_async_busy_) {
        home_pending_conversation_ = true;
        home_pending_wake_ = wake_word;
        SetDeviceState(kDeviceStateIdle);
        SetDeviceState(kDeviceStateConnecting);
        return;
    }
    home_pending_conversation_ = false;
    InitializeProtocol();
    auto protocol = protocol_;
    auto intent = ConversationIntentSequence();
    home_async_busy_ = true;
    SetDeviceState(kDeviceStateIdle);
    SetDeviceState(kDeviceStateConnecting);
    if (!RunHomeWork([this, protocol, intent, wake_word]() {
            bool opened = protocol->Start() && protocol->OpenAudioChannel();
            if (intent != ConversationIntentSequence())
                protocol->CloseAudioChannel();
            Schedule([this, protocol, intent, opened, wake_word]() {
                home_async_busy_ = false;
                if (intent != ConversationIntentSequence() || protocol_ != protocol ||
                    !HomeAllowsConversation() || IsListeningPaused())
                    return;
                if (!opened) {
                    home_online_ = false;
                    home_reason_ = "语音服务不可用";
                    home_policy_.RecoveryFailed(HomeNow());
                    HomeCancelInteraction();
                    SetDeviceState(kDeviceStateOffline);
                    HomeRestoreStandby();
                    return;
                }
                if (!AcceptConversationChannel())
                    return;
                audio_service_.SetInputPaused(false);
                if (!wake_word.empty())
                    protocol->SendWakeWordDetected(wake_word);
                SetListeningMode(GetDefaultListeningMode());
            });
        })) {
        home_async_busy_ = false;
        SetDeviceState(kDeviceStateIdle);
        HomeRestoreStandby();
    }
}

void Application::TickHomeOffline() {
    if (home_assets_busy_)
        return;
    auto& board = Board::GetInstance();
    if (home_policy_.Expired(HomeNow())) {
        HomeCancelInteraction();
        SetDeviceState(kDeviceStateOffline);
        HomeRestoreStandby();
    }
    HomeNetworkService::Result result;
    if (home_network_.TakeResult(result) && result.generation == home_network_epoch_) {
        has_server_time_ = has_server_time_ || result.has_server_time;
        if (result.ready && board.IsNetworkConnected() && !home_configuring_) {
            home_use_mqtt_ = result.use_mqtt;
            home_policy_.RecoverySucceeded();
        } else {
            home_policy_.RecoveryFailed(HomeNow());
            home_reason_ = result.activation_message.empty() ? "语音服务不可用" : "等待设备激活";
            if (!result.activation_message.empty())
                board.GetDisplay()->SetChatMessage("system", result.activation_message.c_str());
        }
    }
    if (home_policy_.mode() == HomeOfflinePolicy::Mode::AutomaticOffline &&
        GetDeviceState() == kDeviceStateStarting && HomeNow() >= home_front_deadline_) {
        SetDeviceState(kDeviceStateOffline);
        HomeRestoreStandby();
    }
    if (!home_configuring_ && audio_service_.IsPlaybackIdle() && home_policy_.ApplyRecovery()) {
        home_online_ = true;
        home_reason_.clear();
        SetDeviceState(kDeviceStateOffline);
        SetDeviceState(kDeviceStateIdle);
        HomeRestoreStandby();
    }
    if (!home_configuring_ && !home_async_busy_ && board.IsNetworkConnected() &&
        home_policy_.CanRecover(HomeNow()) && !home_network_.Busy()) {
        home_network_.StartRecovery(home_network_epoch_);
    }
    if (home_pending_conversation_ && !home_async_busy_ && HomeAllowsConversation() &&
        !IsListeningPaused()) {
        auto wake = home_pending_wake_;
        home_pending_conversation_ = false;
        HomeStartConversation(wake);
    }
    if (home_policy_.mode() == HomeOfflinePolicy::Mode::Online && !home_policy_.MenuOpen() &&
        GetDeviceState() == kDeviceStateIdle && !home_async_busy_ &&
        audio_service_.IsPlaybackIdle())
        HomeStartAssetUpdate();
}

void Application::HomeStartAssetUpdate() {
    if (home_assets_attempted_ || home_assets_busy_)
        return;
    Settings settings("assets", false);
    const auto url = settings.GetString("download_url");
    if (url.empty())
        return;
    home_assets_attempted_ = true;
    HomeCancelInteraction();
    if (home_async_busy_) {
        home_assets_attempted_ = false;
        return;
    }
    SetDeviceState(kDeviceStateUpgrading);
    home_assets_busy_ = true;
    if (!RunHomeWork([this, url]() {
            auto& assets = Assets::GetInstance();
            auto* display = Board::GetInstance().GetDisplay();
            bool prepared = audio_service_.PrepareForAssetUpdate();
            bool success = false;
            if (prepared && display->PrepareForAssetUpdate()) {
                success = assets.Download(url, [](int, size_t) {});
                const bool applied = assets.Apply();
                success = success && applied;
                if (success) {
                    Settings saved("assets", true);
                    if (saved.GetString("download_url") == url)
                        saved.EraseKey("download_url");
                }
            }
            audio_service_.ResumeAfterAssetUpdate();
            Schedule([this, success]() {
                home_assets_busy_ = false;
                SetDeviceState(home_online_ ? kDeviceStateIdle : kDeviceStateOffline);
                HomeRestoreStandby();
                Board::GetInstance().GetDisplay()->ShowNotification(success ? "资源更新完成"
                                                                            : "资源更新失败");
            });
        })) {
        home_assets_busy_ = false;
        SetDeviceState(kDeviceStateIdle);
        HomeRestoreStandby();
    }
}
#endif
