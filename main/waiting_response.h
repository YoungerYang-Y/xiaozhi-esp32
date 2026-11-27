#ifndef WAITING_RESPONSE_H
#define WAITING_RESPONSE_H

#include <cstdint>

// Main-task-only display state. It never changes the protocol or capture state.
class WaitingResponse {
public:
    void Begin(uint64_t turn) {
        turn_ = turn;
        enabled_ = true;
        speech_ = false;
        waiting_ = false;
    }
    void Cancel() {
        enabled_ = false;
        speech_ = false;
        waiting_ = false;
    }
    void Speech(uint64_t turn) {
        if (enabled_ && turn == turn_ && !waiting_) {
            speech_ = true;
        }
    }
    bool Recognized(uint64_t turn, uint64_t now_ms, bool nonempty) {
        if (!enabled_ || turn != turn_ || !speech_ || !nonempty || waiting_) {
            return false;
        }
        speech_ = false;
        waiting_ = true;
        deadline_ms_ = now_ms + 10000;
        return true;
    }
    bool Expire(uint64_t now_ms) {
        if (!waiting_ || now_ms < deadline_ms_) {
            return false;
        }
        waiting_ = false;
        return true;
    }
    bool waiting() const { return waiting_; }

private:
    uint64_t turn_ = 0;
    uint64_t deadline_ms_ = 0;
    bool enabled_ = false;
    bool speech_ = false;
    bool waiting_ = false;
};

#endif
