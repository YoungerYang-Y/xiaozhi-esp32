#ifndef HOME_BATTERY_CALIBRATION_H
#define HOME_BATTERY_CALIBRATION_H

#include <cstdint>

// Intentionally empty until this board has been measured on the bench.
// Values describe the calibrated battery-rail millivolts, not a percentage.
struct HomeBatteryCalibration {
    bool enabled = false;
    int one_to_two_mv = 0;
    int two_to_three_mv = 0;
    int band_hysteresis_mv = 0;
    int low_mv = 0;
    int recovery_mv = 0;
    uint32_t enter_low_ms = 0;
    uint32_t recover_normal_ms = 0;

    constexpr bool IsValid() const {
        return enabled && InRange(one_to_two_mv) && InRange(two_to_three_mv) && InRange(low_mv) &&
               InRange(recovery_mv) && one_to_two_mv < two_to_three_mv && low_mv < recovery_mv &&
               band_hysteresis_mv >= 0 &&
               static_cast<int64_t>(one_to_two_mv) + band_hysteresis_mv <
                   static_cast<int64_t>(two_to_three_mv) - band_hysteresis_mv &&
               enter_low_ms > 0 && recover_normal_ms > 0;
    }

private:
    static constexpr bool InRange(int value) { return value >= 2500 && value <= 4400; }
};

inline constexpr HomeBatteryCalibration kHomeBatteryCalibration{};

#endif
