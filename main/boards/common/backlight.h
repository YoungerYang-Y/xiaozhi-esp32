#pragma once

#include <cstdint>
#include <functional>
#include <mutex>

#include <driver/gpio.h>
#include <esp_err.h>
#include <esp_timer.h>

struct BacklightStatus {
    int brightness;
    int applied_brightness;
    bool display_off;
    bool persisted;
    esp_err_t error;
};

class Backlight {
public:
    Backlight();
    ~Backlight();

    void RestoreBrightness();
    void SetBrightness(uint8_t brightness, bool permanent = false);
    BacklightStatus SetBrightnessChecked(uint8_t brightness);
    BacklightStatus GetStatus() const;
    esp_err_t SetDisplayOff(bool off);
    bool display_off() const;
    uint8_t brightness() const;

protected:
    void OnTransitionTimer();
    virtual void SetBrightnessImpl(uint8_t brightness) = 0;
    virtual esp_err_t ApplyBrightnessChecked(uint8_t brightness);

    esp_timer_handle_t transition_timer_ = nullptr;
    uint8_t brightness_ = 0;
    uint8_t target_brightness_ = 0;
    uint8_t step_ = 1;

private:
    esp_err_t StopTransition();
    esp_err_t ApplyBrightness(uint8_t brightness);
    void RestoreAppliedBrightness(int previous_brightness);

    mutable std::recursive_mutex mutex_;
    uint8_t configured_brightness_ = 75;
    int applied_brightness_ = 0;
    bool display_off_ = false;
    bool persisted_ = false;
    esp_err_t error_ = ESP_OK;
};

class PwmBacklight : public Backlight {
public:
    PwmBacklight(gpio_num_t pin, bool output_invert = false, uint32_t freq_hz = 25000);
    ~PwmBacklight();

    void SetBrightnessImpl(uint8_t brightness) override;

protected:
    esp_err_t ApplyBrightnessChecked(uint8_t brightness) override;

private:
    esp_err_t pwm_error_ = ESP_OK;
};
