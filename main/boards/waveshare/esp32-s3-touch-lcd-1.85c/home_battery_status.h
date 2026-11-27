#ifndef HOME_BATTERY_STATUS_H
#define HOME_BATTERY_STATUS_H

#include "sdkconfig.h"

#if defined(CONFIG_WAVESHARE_1_85C_HOME) && CONFIG_WAVESHARE_1_85C_HOME

#include <driver/gpio.h>

#include <atomic>

#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_oneshot.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "home_battery_filter.h"

// A resident board member. Start/destruction are serialized by the owner;
// GetStatus may be called concurrently while the object is alive. Destruction
// waits for the sampling task to relinquish this object before freeing ADCs.
class HomeBatteryStatus {
public:
    explicit HomeBatteryStatus(HomeBatteryCalibration calibration = kHomeBatteryCalibration)
        : filter_(calibration,
#if defined(CONFIG_WAVESHARE_1_85C_HOME_LOW_BATTERY_ALERTS) && \
    CONFIG_WAVESHARE_1_85C_HOME_LOW_BATTERY_ALERTS
                  true
#else
                  false
#endif
          ) {
    }
    ~HomeBatteryStatus();
    HomeBatteryStatus(const HomeBatteryStatus&) = delete;
    HomeBatteryStatus& operator=(const HomeBatteryStatus&) = delete;
    HomeBatteryStatus(HomeBatteryStatus&&) = delete;
    HomeBatteryStatus& operator=(HomeBatteryStatus&&) = delete;

    bool Start(gpio_num_t pin);
    BatteryStatus GetStatus() const;

private:
    adc_oneshot_unit_handle_t adc_handle_ = nullptr;
    adc_cali_handle_t calibration_ = nullptr;
    adc_channel_t channel_ = ADC_CHANNEL_7;
    TaskHandle_t task_ = nullptr;
    SemaphoreHandle_t exited_ = nullptr;
    std::atomic<bool> stop_requested_{false};
    mutable portMUX_TYPE state_lock_ = portMUX_INITIALIZER_UNLOCKED;
    mutable HomeBatteryFilter filter_;
    bool running_ = false;           // Protected by state_lock_, like filter_.
    bool failure_reported_ = false;  // Sampling task only.
    BatteryBand logged_band_ = BatteryBand::Unknown;

    static void TaskEntry(void* context);
    SemaphoreHandle_t Run();
    void SampleOnce();
    void CleanupAdc();
};

#endif  // CONFIG_WAVESHARE_1_85C_HOME
#endif  // HOME_BATTERY_STATUS_H
