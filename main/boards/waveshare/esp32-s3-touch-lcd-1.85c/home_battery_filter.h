#ifndef HOME_BATTERY_FILTER_H
#define HOME_BATTERY_FILTER_H

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>

#include "battery_status.h"
#include "home_battery_calibration.h"
#include "home_low_battery_filter.h"

class HomeBatteryFilter {
public:
    static constexpr size_t kWindowSize = 5;
    static constexpr int64_t kStaleUs = 3000000;
    static constexpr int kMinimumMv = 2500;
    static constexpr int kMaximumMv = 4400;

    // Temporary voltage bands for later bench tuning. These are neither
    // low-battery thresholds nor an estimate of remaining capacity.
    static constexpr int kOneToTwoMv = 3650;
    static constexpr int kTwoToThreeMv = 3950;
    static constexpr int kHysteresisMv = 30;

    explicit HomeBatteryFilter(HomeBatteryCalibration calibration = kHomeBatteryCalibration,
                               bool low_battery_alerts_enabled = false)
        : calibration_(calibration),
          calibrated_(low_battery_alerts_enabled && calibration.IsValid()),
          low_battery_(calibration, low_battery_alerts_enabled) {}

    void Invalidate() {
        count_ = 0;
        next_ = 0;
        median_mv_ = 0;
        band_ = BatteryBand::Unknown;
        low_battery_.Invalidate();
    }

    void AddSample(int millivolts, int64_t now_us) {
        Expire(now_us);
        if (millivolts < kMinimumMv || millivolts > kMaximumMv) {
            Invalidate();
            return;
        }
        samples_[next_] = millivolts;
        next_ = (next_ + 1) % kWindowSize;
        if (count_ < kWindowSize) {
            ++count_;
        }
        last_valid_us_ = now_us;
        if (count_ == kWindowSize) {
            auto sorted = samples_;
            std::sort(sorted.begin(), sorted.end());
            median_mv_ = sorted[kWindowSize / 2];
            band_ = Classify(median_mv_);
            low_battery_.Observe(median_mv_, now_us);
        }
    }

    // This also discards stale partial windows. A task that stops sampling
    // cannot leave either a displayed band or four old recovery samples alive.
    BatteryStatus GetStatus(int64_t now_us) {
        Expire(now_us);
        const auto state = count_ != kWindowSize
                               ? BatteryState::Unknown
                               : (low_battery_.IsEnabled() ? low_battery_.GetState(now_us)
                                                           : BatteryState::Unclassified);
        return {true, state, band_};
    }

    int GetMedianMillivolts() const { return median_mv_; }

private:
    std::array<int, kWindowSize> samples_{};
    size_t count_ = 0;
    size_t next_ = 0;
    int64_t last_valid_us_ = 0;
    int median_mv_ = 0;
    BatteryBand band_ = BatteryBand::Unknown;
    HomeBatteryCalibration calibration_;
    bool calibrated_ = false;
    HomeLowBatteryFilter low_battery_;

    void Expire(int64_t now_us) {
        if (count_ && (now_us < last_valid_us_ || now_us - last_valid_us_ >= kStaleUs)) {
            Invalidate();
        }
    }

    BatteryBand Classify(int millivolts) const {
        const int one_to_two = calibrated_ ? calibration_.one_to_two_mv : kOneToTwoMv;
        const int two_to_three = calibrated_ ? calibration_.two_to_three_mv : kTwoToThreeMv;
        const int hysteresis = calibrated_ ? calibration_.band_hysteresis_mv : kHysteresisMv;
        switch (band_) {
            case BatteryBand::OneBar:
                if (millivolts >= two_to_three + hysteresis) {
                    return BatteryBand::ThreeBars;
                }
                return millivolts >= one_to_two + hysteresis ? BatteryBand::TwoBars
                                                             : BatteryBand::OneBar;
            case BatteryBand::TwoBars:
                if (millivolts <= one_to_two - hysteresis) {
                    return BatteryBand::OneBar;
                }
                return millivolts >= two_to_three + hysteresis ? BatteryBand::ThreeBars
                                                               : BatteryBand::TwoBars;
            case BatteryBand::ThreeBars:
                if (millivolts <= one_to_two - hysteresis) {
                    return BatteryBand::OneBar;
                }
                return millivolts <= two_to_three - hysteresis ? BatteryBand::TwoBars
                                                               : BatteryBand::ThreeBars;
            default:
                if (millivolts < one_to_two) {
                    return BatteryBand::OneBar;
                }
                return millivolts < two_to_three ? BatteryBand::TwoBars : BatteryBand::ThreeBars;
        }
    }
};

#endif
