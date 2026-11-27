#include "backlight.h"
#include "settings.h"

#include <esp_log.h>
#include <driver/ledc.h>

#define TAG "Backlight"


Backlight::Backlight() {
    // 创建背光渐变定时器
    const esp_timer_create_args_t timer_args = {
        .callback = [](void* arg) {
            auto self = static_cast<Backlight*>(arg);
            self->OnTransitionTimer();
        },
        .arg = this,
        .dispatch_method = ESP_TIMER_TASK,
        .name = "backlight_timer",
        .skip_unhandled_events = true,
    };
    ESP_ERROR_CHECK(esp_timer_create(&timer_args, &transition_timer_));
}

Backlight::~Backlight() {
    if (transition_timer_ != nullptr) {
        esp_timer_stop(transition_timer_);
        esp_timer_delete(transition_timer_);
    }
}

void Backlight::RestoreBrightness() {
    std::lock_guard<std::recursive_mutex> lock(mutex_);
    // Load brightness from settings
    Settings settings("display");
    int32_t saved_brightness = 75;
    auto read_error = settings.TryGetInt("brightness", saved_brightness);
    const int32_t stored_brightness = saved_brightness;

    // 检查亮度值是否为0或过小，设置默认值
    if (saved_brightness <= 0) {
        ESP_LOGW(TAG, "Brightness value (%d) is too small, setting to default (10)", saved_brightness);
        saved_brightness = 10;  // 设置一个较低的默认值
    }
    if (saved_brightness > 100) {
        saved_brightness = 100;
    }

    SetBrightness(saved_brightness);
    configured_brightness_ = saved_brightness;
    persisted_ = read_error == ESP_OK && stored_brightness == saved_brightness;
    if (read_error != ESP_OK && read_error != ESP_ERR_NVS_NOT_FOUND) {
        error_ = read_error;
    }
}

void Backlight::SetBrightness(uint8_t brightness, bool permanent) {
    std::lock_guard<std::recursive_mutex> lock(mutex_);
    if (brightness > 100) {
        brightness = 100;
    }

    if (brightness_ == brightness) {
        return;
    }

    if (permanent) {
        Settings settings("display", true);
        settings.SetInt("brightness", brightness);
        error_ = ESP_OK;
        persisted_ = true;
    } else if (configured_brightness_ != brightness) {
        persisted_ = false;
    }
    configured_brightness_ = brightness;

    if (display_off_) {
        StopTransition();
        return;
    }

    target_brightness_ = brightness;
    step_ = (target_brightness_ > brightness_) ? 1 : -1;

    if (transition_timer_ != nullptr) {
        // 启动定时器，每 5ms 更新一次
        esp_timer_start_periodic(transition_timer_, 5 * 1000);
    }
    ESP_LOGI(TAG, "Set brightness to %d", brightness);
}

void Backlight::OnTransitionTimer() {
    std::lock_guard<std::recursive_mutex> lock(mutex_);
    if (display_off_) {
        StopTransition();
        return;
    }
    if (brightness_ == target_brightness_) {
        esp_timer_stop(transition_timer_);
        return;
    }

    const auto previous_brightness = brightness_;
    brightness_ += step_;
    const auto err = ApplyBrightnessChecked(brightness_);
    if (err != ESP_OK) {
        brightness_ = previous_brightness;
        applied_brightness_ = -1;
        error_ = err;
        StopTransition();
        return;
    }
    applied_brightness_ = brightness_;

    if (brightness_ == target_brightness_) {
        esp_timer_stop(transition_timer_);
    }
}

uint8_t Backlight::brightness() const {
    std::lock_guard<std::recursive_mutex> lock(mutex_);
    return brightness_;
}

BacklightStatus Backlight::GetStatus() const {
    std::lock_guard<std::recursive_mutex> lock(mutex_);
    return {configured_brightness_, applied_brightness_, display_off_, persisted_, error_};
}

bool Backlight::display_off() const {
    std::lock_guard<std::recursive_mutex> lock(mutex_);
    return display_off_;
}

esp_err_t Backlight::StopTransition() {
    if (transition_timer_ == nullptr) {
        return ESP_OK;
    }
    const auto err = esp_timer_stop(transition_timer_);
    return err == ESP_ERR_INVALID_STATE ? ESP_OK : err;
}

esp_err_t Backlight::ApplyBrightnessChecked(uint8_t brightness) {
    SetBrightnessImpl(brightness);
    return ESP_OK;
}

esp_err_t Backlight::ApplyBrightness(uint8_t brightness) {
    // Some board implementations read the protected target or brightness() in their override.
    const auto previous_brightness = brightness_;
    target_brightness_ = brightness;
    brightness_ = brightness;
    const auto err = ApplyBrightnessChecked(brightness);
    if (err == ESP_OK) {
        applied_brightness_ = brightness_;
    } else {
        brightness_ = previous_brightness;
        applied_brightness_ = -1;
    }
    return err;
}

void Backlight::RestoreAppliedBrightness(int previous_brightness) {
    const auto restore_brightness = previous_brightness >= 0
                                        ? previous_brightness
                                        : (display_off_ ? 0 : configured_brightness_);
    ApplyBrightness(restore_brightness);
}

BacklightStatus Backlight::SetBrightnessChecked(uint8_t brightness) {
    std::lock_guard<std::recursive_mutex> lock(mutex_);
    if (brightness < 1 || brightness > 100) {
        auto status = GetStatus();
        status.error = ESP_ERR_INVALID_ARG;
        return status;
    }

    Settings settings("display", true);
    int32_t stored_brightness = configured_brightness_;
    const auto read_error = settings.TryGetInt("brightness", stored_brightness);
    if (read_error != ESP_OK && read_error != ESP_ERR_NVS_NOT_FOUND) {
        error_ = read_error;
        persisted_ = false;
        return GetStatus();
    }

    auto err = StopTransition();
    if (err != ESP_OK) {
        error_ = err;
        return GetStatus();
    }
    const auto previous_brightness = applied_brightness_;
    err = ApplyBrightness(display_off_ ? 0 : brightness);
    if (err != ESP_OK) {
        RestoreAppliedBrightness(previous_brightness);
        persisted_ = read_error == ESP_OK && stored_brightness == configured_brightness_;
        error_ = err;
        return GetStatus();
    }

    err = settings.TrySetIntAndCommit("brightness", brightness);
    if (err != ESP_OK) {
        // A commit error may leave storage uncertain. Restore through a separate checked commit
        // and read back rather than relying on the Settings destructor to retry it.
        settings.TrySetIntAndCommit("brightness", stored_brightness);
        int32_t restored_brightness = 0;
        persisted_ = settings.TryGetInt("brightness", restored_brightness) == ESP_OK &&
                     restored_brightness == configured_brightness_;
        RestoreAppliedBrightness(previous_brightness);
        error_ = err;
        return GetStatus();
    }

    configured_brightness_ = brightness;
    persisted_ = true;
    error_ = ESP_OK;
    return GetStatus();
}

esp_err_t Backlight::SetDisplayOff(bool off) {
    std::lock_guard<std::recursive_mutex> lock(mutex_);
    auto err = StopTransition();
    if (err != ESP_OK) {
        error_ = err;
        return err;
    }
    const auto previous_brightness = applied_brightness_;
    err = ApplyBrightness(off ? 0 : configured_brightness_);
    if (err != ESP_OK) {
        RestoreAppliedBrightness(previous_brightness);
        error_ = err;
        return err;
    }
    display_off_ = off;
    error_ = ESP_OK;
    return ESP_OK;
}

PwmBacklight::PwmBacklight(gpio_num_t pin, bool output_invert, uint32_t freq_hz) : Backlight() {
    const ledc_timer_config_t backlight_timer = {
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .duty_resolution = LEDC_TIMER_10_BIT,
        .timer_num = LEDC_TIMER_0,
        .freq_hz = freq_hz, //背光pwm频率需要高一点，防止电感啸叫
        .clk_cfg = LEDC_AUTO_CLK,
        .deconfigure = false
    };
    ESP_ERROR_CHECK(ledc_timer_config(&backlight_timer));

    // Setup LEDC peripheral for PWM backlight control
    const ledc_channel_config_t backlight_channel = {
        .gpio_num = pin,
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .channel = LEDC_CHANNEL_0,
        .intr_type = LEDC_INTR_DISABLE,
        .timer_sel = LEDC_TIMER_0,
        .duty = 0,
        .hpoint = 0,
        .flags = {
            .output_invert = output_invert,
        }
    };
    ESP_ERROR_CHECK(ledc_channel_config(&backlight_channel));
}

PwmBacklight::~PwmBacklight() {
    ledc_stop(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, 0);
}

void PwmBacklight::SetBrightnessImpl(uint8_t brightness) {
    // LEDC resolution set to 10bits, thus: 100% = 1023
    uint32_t duty_cycle = (1023 * brightness) / 100;
    pwm_error_ = ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, duty_cycle);
    if (pwm_error_ == ESP_OK) {
        pwm_error_ = ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0);
    }
}

esp_err_t PwmBacklight::ApplyBrightnessChecked(uint8_t brightness) {
    pwm_error_ = ESP_OK;
    // Keep dispatching through the existing virtual hook for PwmBacklight subclasses.
    SetBrightnessImpl(brightness);
    return pwm_error_;
}
