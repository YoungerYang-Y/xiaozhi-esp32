#ifndef WIFI_BOARD_H
#define WIFI_BOARD_H

#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <atomic>
#include "board.h"

class WifiBoard : public Board {
protected:
    esp_timer_handle_t connect_timer_ = nullptr;
    bool in_config_mode_ = false;
    bool InitializeWifi();
#if CONFIG_WAVESHARE_1_85C_HOME_OFFLINE
    std::atomic<bool> network_enabled_{false};
    std::atomic<bool> suppress_config_reconnect_{false};
#endif
    NetworkEventCallback network_event_callback_ = nullptr;

    virtual std::string GetBoardJson() override;

    /**
     * Handle network event (called from WiFi manager callbacks)
     * @param event The network event type
     * @param data Additional data (e.g., SSID for Connecting/Connected events)
     */
    void OnNetworkEvent(NetworkEvent event, const std::string& data = "");

    /**
     * Start WiFi connection attempt
     */
    void TryWifiConnect();

    /**
     * Enter WiFi configuration mode
     */
    void StartWifiConfigMode();

    /**
     * WiFi connection timeout callback
     */
    static void OnWifiConnectTimeout(void* arg);

public:
    WifiBoard();
    virtual ~WifiBoard();

    virtual std::string GetBoardType() override;

    /**
     * Start network connection asynchronously
     * This function returns immediately. Network events are notified through the callback set by
     * SetNetworkEventCallback().
     */
    virtual void StartNetwork() override;
    bool SetNetworkEnabled(bool enabled) override;
    bool RequestWifiConfiguration() override;
    bool IsNetworkConnected() const override;
    bool IsWifiConfigurationActive() const override;
    bool HasNetworkConfiguration() const override;

    virtual NetworkInterface* GetNetwork() override;
    virtual void SetNetworkEventCallback(NetworkEventCallback callback) override;
    virtual const char* GetNetworkStateIcon() override;
    virtual void SetPowerSaveLevel(PowerSaveLevel level) override;
    virtual AudioCodec* GetAudioCodec() override { return nullptr; }
    virtual std::string GetDeviceStatusJson() override;

    /**
     * Enter WiFi configuration mode (thread-safe, can be called from any task)
     */
    void EnterWifiConfigMode();

    /**
     * Check if in WiFi config mode
     */
    bool IsInWifiConfigMode() const;
};

#endif  // WIFI_BOARD_H
