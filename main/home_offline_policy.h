#pragma once

#include <algorithm>
#include <cstdint>

// Main-task-owned policy. Time and hardware operations are supplied by the caller.
class HomeOfflinePolicy {
public:
    enum class Mode { Online, AutomaticOffline, ManualOffline };
    void Start(bool manual, uint64_t now) {
        mode_ = manual ? Mode::ManualOffline : Mode::AutomaticOffline;
        retry_at_ = now;
    }
    Mode mode() const { return mode_; }
    void AllowNetwork(uint64_t now) {
        mode_ = Mode::AutomaticOffline;
        recovered_ = false;
        retry_at_ = now;
    }
    bool CanRecover(uint64_t now) const {
        return mode_ == Mode::AutomaticOffline && now >= retry_at_;
    }
    void RecoveryFailed(uint64_t now) {
        if (mode_ == Mode::ManualOffline)
            return;
        mode_ = Mode::AutomaticOffline;
        recovered_ = false;
        retry_at_ = now + delay_;
        delay_ = std::min<uint32_t>(delay_ * 2, 300000);
    }
    uint32_t RetryDelayMs() const { return delay_; }
    uint64_t BeginCommand(uint64_t now) {
        command_ = true;
        deadline_ = now + 8000;
        return ++generation_;
    }
    bool CommandActive() const { return command_; }
    bool AcceptCommand(uint64_t generation, uint64_t now) const {
        return command_ && generation == generation_ && now < deadline_;
    }
    void CancelCommand() {
        command_ = false;
        ++generation_;
    }
    bool Expired(uint64_t now) const { return command_ && now >= deadline_; }
    void OpenMenu() {
        CancelCommand();
        menu_ = true;
        selected_ = MenuSize() - 1;
    }
    void CloseMenu() { menu_ = false; }
    bool MenuOpen() const { return menu_; }
    int Selected() const { return selected_; }
    int MenuSize() const { return mode_ == Mode::AutomaticOffline ? 4 : 3; }
    void Select(int index) {
        if (index >= 0 && index < MenuSize())
            selected_ = index;
    }
    void NextMenuItem() { selected_ = (selected_ + 1) % MenuSize(); }
    void RecoverySucceeded() { recovered_ = mode_ == Mode::AutomaticOffline; }
    bool ApplyRecovery() {
        if (!recovered_ || menu_ || command_)
            return false;
        recovered_ = false;
        mode_ = Mode::Online;
        return true;
    }
    void ManualOffline() {
        mode_ = Mode::ManualOffline;
        recovered_ = false;
        CancelCommand();
    }

private:
    Mode mode_ = Mode::AutomaticOffline;
    bool menu_ = false, command_ = false, recovered_ = false;
    int selected_ = 0;
    uint64_t generation_ = 0, deadline_ = 0, retry_at_ = 0;
    uint32_t delay_ = 10000;
};
