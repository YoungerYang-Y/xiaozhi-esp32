#ifndef BATTERY_STATUS_H
#define BATTERY_STATUS_H

// Optional coarse battery capability. Voltage and sampling timestamps belong
// to the board sampler; callers must not infer charging or a percentage.
enum class BatteryState { Unknown, Unclassified, Normal, Low };
enum class BatteryBand { Unknown, OneBar, TwoBars, ThreeBars };

struct BatteryStatus {
    bool supported = false;
    BatteryState state = BatteryState::Unknown;
    BatteryBand band = BatteryBand::Unknown;
};

inline const char* BatteryStateName(BatteryState state) {
    switch (state) {
        case BatteryState::Unclassified:
            return "unclassified";
        case BatteryState::Normal:
            return "normal";
        case BatteryState::Low:
            return "low";
        default:
            return "unknown";
    }
}

inline const char* BatteryBandName(BatteryBand band) {
    switch (band) {
        case BatteryBand::OneBar:
            return "one_bar";
        case BatteryBand::TwoBars:
            return "two_bars";
        case BatteryBand::ThreeBars:
            return "three_bars";
        default:
            return nullptr;
    }
}

#endif
