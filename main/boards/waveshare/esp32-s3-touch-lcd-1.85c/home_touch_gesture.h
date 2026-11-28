#pragma once

#include <cstdint>

class HomeTouchGesture {
public:
    static constexpr uint32_t kMaxTapMs = 300;
    static constexpr uint32_t kDoubleTapGapMs = 300;
    static constexpr int32_t kMaxDistancePx = 30;

    // Called with monotonic time and one complete controller sample. A trigger is
    // emitted only on the second release; the following burst is consumed until
    // the screen has been untouched for a full double-tap window.
    bool Update(uint64_t now_ms, bool read_ok, uint8_t points, uint16_t x = 0, uint16_t y = 0,
                bool menu = false) {
        if (!read_ok || points > 1) {
            touching_ = false;
            first_tap_pending_ = false;
            suppress_burst_ = false;
            discard_until_release_ = true;
            return false;
        }
        if (discard_until_release_) {
            discard_until_release_ = points != 0;
            return false;
        }
        if (suppress_burst_) {
            if (points != 0) {
                touching_ = true;
            } else if (touching_) {
                touching_ = false;
                released_ms_ = now_ms;
            } else if (now_ms - released_ms_ > kDoubleTapGapMs) {
                suppress_burst_ = false;
            }
            return false;
        }
        if (!touching_ && first_tap_pending_ && now_ms - released_ms_ > kDoubleTapGapMs) {
            first_tap_pending_ = false;
        }
        if (points != 0) {
            if (!touching_) {
                touching_ = true;
                started_ms_ = now_ms;
                start_x_ = x;
                start_y_ = y;
                tap_valid_ = true;
            }
            last_x_ = x;
            last_y_ = y;
            tap_valid_ =
                tap_valid_ && now_ms - started_ms_ <= kMaxTapMs && Near(x, y, start_x_, start_y_);
            return false;
        }
        if (!touching_) {
            return false;
        }
        touching_ = false;
        if (!tap_valid_ || now_ms - started_ms_ > kMaxTapMs) {
            first_tap_pending_ = false;
            return false;
        }
        // Menu rows use a complete short tap, never the double-tap backlight gesture.
        if (menu) {
            first_tap_pending_ = false;
            return true;
        }
        if (first_tap_pending_ && Near(start_x_, start_y_, first_x_, first_y_) &&
            Near(last_x_, last_y_, first_x_, first_y_)) {
            first_tap_pending_ = false;
            suppress_burst_ = true;
            released_ms_ = now_ms;
            return true;
        }
        first_tap_pending_ = true;
        released_ms_ = now_ms;
        first_x_ = start_x_;
        first_y_ = start_y_;
        return false;
    }

    bool UpdateMenu(uint64_t now_ms, bool read_ok, uint8_t points, uint16_t x = 0, uint16_t y = 0) {
        return Update(now_ms, read_ok, points, x, y, true);
    }

    void Reset() {
        *this = HomeTouchGesture();
        discard_until_release_ = true;
    }

    void ResetForMenuChange() {
        const bool active = touching_ || discard_until_release_;
        Reset();
        discard_until_release_ = active;
    }

    uint16_t tap_x() const { return last_x_; }
    uint16_t tap_y() const { return last_y_; }

    bool NeedsPolling() const {
        return touching_ || first_tap_pending_ || suppress_burst_ || discard_until_release_;
    }

private:
    static bool Near(uint16_t x, uint16_t y, uint16_t other_x, uint16_t other_y) {
        const int32_t dx = static_cast<int32_t>(x) - other_x;
        const int32_t dy = static_cast<int32_t>(y) - other_y;
        if (dx < -kMaxDistancePx || dx > kMaxDistancePx || dy < -kMaxDistancePx ||
            dy > kMaxDistancePx) {
            return false;
        }
        return dx * dx + dy * dy <= kMaxDistancePx * kMaxDistancePx;
    }

    bool touching_ = false;
    bool tap_valid_ = false;
    bool first_tap_pending_ = false;
    bool suppress_burst_ = false;
    bool discard_until_release_ = false;
    uint64_t started_ms_ = 0;
    uint64_t released_ms_ = 0;
    uint16_t start_x_ = 0;
    uint16_t start_y_ = 0;
    uint16_t last_x_ = 0;
    uint16_t last_y_ = 0;
    uint16_t first_x_ = 0;
    uint16_t first_y_ = 0;
};
