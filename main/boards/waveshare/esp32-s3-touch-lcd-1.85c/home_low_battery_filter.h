#ifndef HOME_LOW_BATTERY_FILTER_H
#define HOME_LOW_BATTERY_FILTER_H

#include <cstdint>

#include "battery_status.h"
#include "home_battery_calibration.h"

// Consumes only fresh, valid filtered samples. GetState is a snapshot and never
// advances either dwell timer. Invalidation clears dwell while preserving a
// previously confirmed low latch.
class HomeLowBatteryFilter {
public:
    explicit constexpr HomeLowBatteryFilter(
        HomeBatteryCalibration calibration = kHomeBatteryCalibration, bool feature_enabled = false)
        : calibration_(calibration), enabled_(feature_enabled && calibration.IsValid()) {}

    constexpr bool IsEnabled() const { return enabled_; }

    void Invalidate() {
        entering_low_ = false;
        recovering_ = false;
        has_sample_time_ = false;
        if (!low_latched_) {
            state_ = BatteryState::Unknown;
        }
    }

    void Observe(int millivolts, int64_t now_us) {
        if (!enabled_) {
            state_ = BatteryState::Unclassified;
            return;
        }
        if (has_sample_time_ &&
            (now_us < last_sample_us_ || now_us - last_sample_us_ >= kMaximumSampleGapUs)) {
            entering_low_ = false;
            recovering_ = false;
        }
        has_sample_time_ = true;
        last_sample_us_ = now_us;

        if (low_latched_) {
            entering_low_ = false;
            if (millivolts < calibration_.recovery_mv) {
                recovering_ = false;
                state_ = BatteryState::Low;
                return;
            }
            if (!recovering_) {
                recovering_ = true;
                recovery_started_us_ = now_us;
            }
            if (ElapsedAtLeast(now_us, recovery_started_us_, calibration_.recover_normal_ms)) {
                low_latched_ = false;
                recovering_ = false;
                state_ = BatteryState::Normal;
                return;
            }
            state_ = BatteryState::Low;
            return;
        }

        recovering_ = false;
        if (millivolts >= calibration_.low_mv) {
            entering_low_ = false;
            state_ = BatteryState::Normal;
            return;
        }
        if (!entering_low_) {
            entering_low_ = true;
            low_started_us_ = now_us;
        }
        if (ElapsedAtLeast(now_us, low_started_us_, calibration_.enter_low_ms)) {
            low_latched_ = true;
            entering_low_ = false;
            state_ = BatteryState::Low;
            return;
        }
        state_ = BatteryState::Normal;
    }

    BatteryState GetState(int64_t now_us) {
        if (!enabled_) {
            return BatteryState::Unclassified;
        }
        if (has_sample_time_ &&
            (now_us < last_sample_us_ || now_us - last_sample_us_ >= kMaximumSampleGapUs)) {
            Invalidate();
        }
        return state_;
    }

private:
    static constexpr int64_t kMaximumSampleGapUs = 3000000;
    HomeBatteryCalibration calibration_;
    bool enabled_ = false;
    bool low_latched_ = false;
    BatteryState state_ = BatteryState::Unknown;
    bool entering_low_ = false;
    bool recovering_ = false;
    bool has_sample_time_ = false;
    int64_t last_sample_us_ = 0;
    int64_t low_started_us_ = 0;
    int64_t recovery_started_us_ = 0;

    static bool ElapsedAtLeast(int64_t now_us, int64_t start_us, uint32_t duration_ms) {
        return now_us >= start_us && now_us - start_us >= static_cast<int64_t>(duration_ms) * 1000;
    }
};

#endif
