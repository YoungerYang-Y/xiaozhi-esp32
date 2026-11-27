#pragma once

#include <cstdint>

#include "boards/common/battery_status.h"

// Main-task-owned reminder bookkeeping. The board alone confirms low/recovery
// using measured thresholds; this class never classifies voltage or capacity.
class LowBatteryReminder {
public:
    void Observe(const BatteryStatus& status) {
        const bool known = status.supported && status.band != BatteryBand::Unknown;
        reading_low_ = known && status.state == BatteryState::Low;
        if (reading_low_ && !active_) {
            active_ = true;
            started_ = false;
            if (++episode_ == 0) {
                ++episode_;
            }
        } else if (known && status.state == BatteryState::Normal) {
            active_ = false;
            started_ = false;
        }
    }
    uint64_t PendingEpisode() const { return HasPending() && reading_low_ ? episode_ : 0; }
    bool HasPending() const { return active_ && !started_; }
    bool MarkStarted(uint64_t episode) {
        // This receipt can follow an unknown reading or user interruption.
        // Successful output is still consumed for the same active episode.
        if (!active_ || started_ || episode == 0 || episode != episode_) {
            return false;
        }
        started_ = true;
        return true;
    }

private:
    uint64_t episode_ = 0;
    bool active_ = false;
    bool started_ = false;
    bool reading_low_ = false;
};
