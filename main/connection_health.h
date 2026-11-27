#pragma once

#include <cstdint>

namespace ConnectionHealthTypes {

enum class Stage : uint8_t {
    Configuration = 0,
    Control,
    Handshake,
    Media,
    Activation,
};

enum class StageState : uint8_t {
    Unknown,
    Healthy,
    Failed,
};

enum class ServiceState : uint8_t {
    Unknown,
    Unavailable,
    Available,
};

enum class WifiState : uint8_t {
    Unknown,
    Connected,
    Disconnected,
};

enum class PromptKind : uint8_t {
    None,
    WifiUnavailable,
    ServiceUnavailable,
};

struct PromptTicket {
    PromptKind kind = PromptKind::None;
    uint64_t episode = 0;
};

constexpr uint8_t StageMask(Stage stage) {
    return static_cast<uint8_t>(1U << static_cast<uint8_t>(stage));
}

}  // namespace ConnectionHealthTypes

// Main-task-owned bookkeeping only. Callers provide stage-specific evidence;
// this helper does not inspect protocols, schedule retries, or manage audio.
class ConnectionHealth {
public:
    using Stage = ConnectionHealthTypes::Stage;
    using StageState = ConnectionHealthTypes::StageState;
    using ServiceState = ConnectionHealthTypes::ServiceState;
    using WifiState = ConnectionHealthTypes::WifiState;
    using PromptKind = ConnectionHealthTypes::PromptKind;
    using PromptTicket = ConnectionHealthTypes::PromptTicket;

    void SetRequiredStages(uint8_t mask) {
        required_stages_ = static_cast<uint8_t>(mask & kAllStages);
        SyncServiceEpisode();
    }

    uint8_t GetRequiredStages() const { return required_stages_; }

    void BeginAttempt(Stage stage, uint64_t identity) {
        if (!IsValidStage(stage) || identity == 0) {
            return;
        }
        const auto index = Index(stage);
        if (attempts_[index] == identity) {
            return;
        }
        attempts_[index] = identity;
        if (states_[index] == StageState::Healthy) {
            states_[index] = StageState::Unknown;
        }
        SyncServiceEpisode();
    }

    void Retire(Stage stage) {
        if (!IsValidStage(stage)) {
            return;
        }
        const auto index = Index(stage);
        attempts_[index] = 0;
        if (states_[index] == StageState::Healthy) {
            states_[index] = StageState::Unknown;
        }
        SyncServiceEpisode();
    }

    bool Retire(Stage stage, uint64_t identity) {
        if (!Accepts(stage, identity)) {
            return false;
        }
        Retire(stage);
        return true;
    }

    bool ReportFailure(Stage stage, uint64_t identity) {
        if (!Accepts(stage, identity)) {
            return false;
        }
        states_[Index(stage)] = StageState::Failed;
        SyncServiceEpisode();
        return true;
    }

    bool ReportSuccess(Stage stage, uint64_t identity) {
        if (!Accepts(stage, identity)) {
            return false;
        }
        states_[Index(stage)] = StageState::Healthy;
        SyncServiceEpisode();
        return true;
    }

    StageState GetStageState(Stage stage) const {
        return IsValidStage(stage) ? states_[Index(stage)] : StageState::Unknown;
    }

    ServiceState GetServiceState() const {
        if (required_stages_ == 0) {
            return ServiceState::Unknown;
        }
        bool all_healthy = true;
        for (uint8_t i = 0; i < kStageCount; ++i) {
            if ((required_stages_ & (1U << i)) == 0) {
                continue;
            }
            if (states_[i] == StageState::Failed) {
                return ServiceState::Unavailable;
            }
            all_healthy = all_healthy && states_[i] == StageState::Healthy;
        }
        return all_healthy && wifi_state_ == WifiState::Connected ? ServiceState::Available
                                                                  : ServiceState::Unknown;
    }

    WifiState GetWifiState() const { return wifi_state_; }

    void OnWifiConnected() {
        wifi_state_ = WifiState::Connected;
        wifi_episode_active_ = false;
        wifi_prompt_started_ = false;
    }

    void OnWifiDisconnected() {
        if (wifi_state_ != WifiState::Disconnected) {
            wifi_state_ = WifiState::Disconnected;
            wifi_episode_ = NextEpisode(wifi_episode_);
            wifi_episode_active_ = true;
            wifi_prompt_started_ = false;
        }
    }

    PromptTicket GetPendingPrompt() const {
        if (wifi_episode_active_ && !wifi_prompt_started_) {
            return {PromptKind::WifiUnavailable, wifi_episode_};
        }
        if (wifi_state_ == WifiState::Connected && service_episode_active_ &&
            !service_prompt_started_) {
            return {PromptKind::ServiceUnavailable, service_episode_};
        }
        return {};
    }

    // Invoke only when the corresponding sound has actually started. This is
    // a receipt, not an admission check: a higher-priority link event may have
    // reached the main task before this callback. Still consume the same live
    // episode so an interrupted sound cannot be replayed after link recovery.
    bool MarkPromptStarted(PromptTicket ticket) {
        if (ticket.episode == 0) {
            return false;
        }
        if (ticket.kind == PromptKind::WifiUnavailable) {
            if (!wifi_episode_active_ || wifi_prompt_started_ || ticket.episode != wifi_episode_) {
                return false;
            }
            wifi_prompt_started_ = true;
        } else if (ticket.kind == PromptKind::ServiceUnavailable) {
            if (!service_episode_active_ || service_prompt_started_ ||
                ticket.episode != service_episode_) {
                return false;
            }
            service_prompt_started_ = true;
        } else {
            return false;
        }
        return true;
    }

private:
    static constexpr uint8_t kStageCount = 5;
    static constexpr uint8_t kAllStages = (1U << kStageCount) - 1U;

    static bool IsValidStage(Stage stage) { return static_cast<uint8_t>(stage) < kStageCount; }

    static uint8_t Index(Stage stage) { return static_cast<uint8_t>(stage); }

    bool Accepts(Stage stage, uint64_t identity) const {
        return IsValidStage(stage) && identity != 0 && attempts_[Index(stage)] == identity;
    }

    static uint64_t NextEpisode(uint64_t episode) {
        ++episode;
        return episode == 0 ? 1 : episode;
    }

    bool HasRequiredFault() const {
        for (uint8_t i = 0; i < kStageCount; ++i) {
            if ((required_stages_ & (1U << i)) != 0 && states_[i] == StageState::Failed) {
                return true;
            }
        }
        return false;
    }

    void SyncServiceEpisode() {
        if (!HasRequiredFault()) {
            service_episode_active_ = false;
            service_prompt_started_ = false;
        } else if (!service_episode_active_) {
            service_episode_ = NextEpisode(service_episode_);
            service_episode_active_ = true;
            service_prompt_started_ = false;
        }
    }

    StageState states_[kStageCount] = {};
    uint64_t attempts_[kStageCount] = {};
    uint8_t required_stages_ = kAllStages;
    WifiState wifi_state_ = WifiState::Unknown;
    uint64_t service_episode_ = 0;
    uint64_t wifi_episode_ = 0;
    bool service_episode_active_ = false;
    bool service_prompt_started_ = false;
    bool wifi_episode_active_ = false;
    bool wifi_prompt_started_ = false;
};
