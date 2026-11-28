#ifndef HOME_NETWORK_SERVICE_H
#define HOME_NETWORK_SERVICE_H

#include <atomic>
#include <cstdint>
#include <mutex>
#include <optional>
#include <string>

// Single, bounded worker. The application owns retry policy and generation changes.
class HomeNetworkService {
public:
    struct Result {
        uint64_t generation = 0;
        bool ready = false;
        bool use_mqtt = false;
        bool has_server_time = false;
        std::string activation_message;
        std::string error;
    };

    HomeNetworkService() = default;
    ~HomeNetworkService();
    HomeNetworkService(const HomeNetworkService&) = delete;
    HomeNetworkService& operator=(const HomeNetworkService&) = delete;

    bool StartRecovery(uint64_t generation);
    bool TakeResult(Result& result);
    bool Busy() const;
    // Pass the application's new generation; any old result retains its old ID.
    void Invalidate(uint64_t generation);

private:
    mutable std::mutex mutex_;
    std::atomic<uint64_t> generation_{0};
    std::atomic<bool> stopping_{false};
    uint64_t job_generation_ = 0;
    bool running_ = false;
    std::optional<Result> result_;

    bool Cancelled(uint64_t generation) const;
    void Run();
    Result Recover(uint64_t generation);
};

#endif  // HOME_NETWORK_SERVICE_H
