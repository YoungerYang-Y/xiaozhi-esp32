#include "home_battery_status.h"

#if defined(CONFIG_WAVESHARE_1_85C_HOME) && CONFIG_WAVESHARE_1_85C_HOME

#include <esp_log.h>
#include <esp_timer.h>

#include "esp_adc/adc_cali_scheme.h"

namespace {
constexpr char kTag[] = "HomeBattery";
constexpr gpio_num_t kBatteryPin = GPIO_NUM_8;
constexpr int kDividerMultiplier = 3;  // Rev2.0: R2=200k, R11=100k.
constexpr adc_atten_t kAttenuation = ADC_ATTEN_DB_12;
constexpr uint32_t kSamplePeriodMs = 1000;
}  // namespace

HomeBatteryStatus::~HomeBatteryStatus() {
    if (task_) {
        stop_requested_.store(true);
        xTaskNotifyGive(task_);
        // No forced deletion: the ADC operation must finish before cleanup.
        xSemaphoreTake(exited_, portMAX_DELAY);
        task_ = nullptr;
    }
    CleanupAdc();
    if (exited_) {
        vSemaphoreDelete(exited_);
    }
}

bool HomeBatteryStatus::Start(gpio_num_t pin) {
    if (task_) {
        return false;
    }
    portENTER_CRITICAL(&state_lock_);
    filter_.Invalidate();
    running_ = false;
    portEXIT_CRITICAL(&state_lock_);

    adc_unit_t unit;
    adc_channel_t channel;
    esp_err_t error = adc_oneshot_io_to_channel(pin, &unit, &channel);
    // Resolve through the SDK, then reject a pin/unit/channel inconsistent
    // with the HOME Rev2.0 wiring and ESP32-S3 ADC1 GPIO8 mapping.
    if (pin != kBatteryPin || error != ESP_OK || unit != ADC_UNIT_1 || channel != ADC_CHANNEL_7) {
        ESP_LOGW(kTag, "Battery GPIO/ADC mapping unavailable or unexpected");
        return false;
    }
    channel_ = channel;

    adc_oneshot_unit_init_cfg_t unit_config{};
    unit_config.unit_id = unit;
    unit_config.ulp_mode = ADC_ULP_MODE_DISABLE;
    error = adc_oneshot_new_unit(&unit_config, &adc_handle_);
    if (error == ESP_OK) {
        adc_oneshot_chan_cfg_t channel_config{};
        channel_config.atten = kAttenuation;
        channel_config.bitwidth = ADC_BITWIDTH_DEFAULT;
        error = adc_oneshot_config_channel(adc_handle_, channel_, &channel_config);
    }
    if (error == ESP_OK) {
        adc_cali_curve_fitting_config_t calibration_config{};
        calibration_config.unit_id = unit;
        calibration_config.chan = channel_;
        calibration_config.atten = kAttenuation;
        calibration_config.bitwidth = ADC_BITWIDTH_DEFAULT;
        error = adc_cali_create_scheme_curve_fitting(&calibration_config, &calibration_);
    }
    if (error != ESP_OK) {
        ESP_LOGW(kTag, "Battery ADC initialization unavailable: %s", esp_err_to_name(error));
        CleanupAdc();
        return false;
    }

    exited_ = xSemaphoreCreateBinary();
    if (!exited_) {
        ESP_LOGW(kTag, "Battery task exit semaphore unavailable");
        CleanupAdc();
        return false;
    }
    stop_requested_.store(false);
    failure_reported_ = false;
    logged_band_ = BatteryBand::Unknown;
    portENTER_CRITICAL(&state_lock_);
    running_ = true;
    portEXIT_CRITICAL(&state_lock_);
    if (xTaskCreate(TaskEntry, "home_battery", 3072, this, tskIDLE_PRIORITY + 1, &task_) !=
        pdPASS) {
        task_ = nullptr;
        portENTER_CRITICAL(&state_lock_);
        running_ = false;
        portEXIT_CRITICAL(&state_lock_);
        vSemaphoreDelete(exited_);
        exited_ = nullptr;
        CleanupAdc();
        ESP_LOGW(kTag, "Battery sampling task unavailable");
        return false;
    }
    return true;
}

BatteryStatus HomeBatteryStatus::GetStatus() const {
    portENTER_CRITICAL(&state_lock_);
    if (!running_) {
        filter_.Invalidate();
    }
    const auto status = filter_.GetStatus(esp_timer_get_time());
    portEXIT_CRITICAL(&state_lock_);
    return status;
}

void HomeBatteryStatus::TaskEntry(void* context) {
    // Run returns the exit semaphore by value. After that return, this task
    // never dereferences context again, including after signaling the owner.
    const auto exited = static_cast<HomeBatteryStatus*>(context)->Run();
    xSemaphoreGive(exited);
    vTaskDelete(nullptr);
}

SemaphoreHandle_t HomeBatteryStatus::Run() {
    while (!stop_requested_.load()) {
        SampleOnce();
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(kSamplePeriodMs));
    }
    portENTER_CRITICAL(&state_lock_);
    running_ = false;
    filter_.Invalidate();
    portEXIT_CRITICAL(&state_lock_);
    return exited_;
}

void HomeBatteryStatus::SampleOnce() {
    int raw = 0;
    int pin_mv = 0;
    esp_err_t error = adc_oneshot_read(adc_handle_, channel_, &raw);
    if (error == ESP_OK) {
        error = adc_cali_raw_to_voltage(calibration_, raw, &pin_mv);
    }
    // Scale in 64 bits so an invalid calibration result cannot overflow into
    // the valid range. Never replace failed calibration with uncalibrated ADC.
    const int64_t battery_mv = static_cast<int64_t>(pin_mv) * kDividerMultiplier;
    const bool valid = error == ESP_OK && battery_mv >= HomeBatteryFilter::kMinimumMv &&
                       battery_mv <= HomeBatteryFilter::kMaximumMv;
    portENTER_CRITICAL(&state_lock_);
    if (valid) {
        filter_.AddSample(static_cast<int>(battery_mv), esp_timer_get_time());
    } else {
        filter_.Invalidate();
    }
    const auto status = filter_.GetStatus(esp_timer_get_time());
    const int median_mv = filter_.GetMedianMillivolts();
    portEXIT_CRITICAL(&state_lock_);

    if (!valid && !failure_reported_) {
        ESP_LOGW(kTag,
                 "Battery sample unavailable (%s, raw=%d, pin=%d mV, rail=%lld mV); "
                 "rebuilding five-sample window",
                 error == ESP_OK ? "voltage out of range" : esp_err_to_name(error), raw, pin_mv,
                 static_cast<long long>(battery_mv));
        failure_reported_ = true;
    }
    if (status.band != BatteryBand::Unknown && (status.band != logged_band_ || failure_reported_)) {
        ESP_LOGI(kTag, "Battery median=%d mV, temporary band=%s%s", median_mv,
                 BatteryBandName(status.band), failure_reported_ ? " (recovered)" : "");
        failure_reported_ = false;
    }
    logged_band_ = status.band;
}

void HomeBatteryStatus::CleanupAdc() {
    if (calibration_) {
        const auto error = adc_cali_delete_scheme_curve_fitting(calibration_);
        if (error != ESP_OK) {
            ESP_LOGW(kTag, "Battery calibration cleanup: %s", esp_err_to_name(error));
        }
        calibration_ = nullptr;
    }
    if (adc_handle_) {
        const auto error = adc_oneshot_del_unit(adc_handle_);
        if (error != ESP_OK) {
            ESP_LOGW(kTag, "Battery ADC cleanup: %s", esp_err_to_name(error));
        }
        adc_handle_ = nullptr;
    }
}

#endif  // CONFIG_WAVESHARE_1_85C_HOME
