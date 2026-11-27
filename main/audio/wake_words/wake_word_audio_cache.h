#ifndef WAKE_WORD_AUDIO_CACHE_H
#define WAKE_WORD_AUDIO_CACHE_H

#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <mutex>
#include <vector>

#include <esp_opus_enc.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

class WakeWordAudioCache {
public:
    WakeWordAudioCache() = default;
    ~WakeWordAudioCache();

    WakeWordAudioCache(const WakeWordAudioCache&) = delete;
    WakeWordAudioCache& operator=(const WakeWordAudioCache&) = delete;

    bool Initialize(size_t sample_count);
    void Store(const int16_t* data, size_t samples);
    size_t Read(size_t offset, int16_t* output, size_t samples) const;
    size_t Size() const;
    void Clear();

private:
    int16_t* buffer_ = nullptr;
    size_t capacity_ = 0;
    size_t size_ = 0;
    size_t write_position_ = 0;
    mutable std::mutex mutex_;
};

// One persistent task owns the static encoder stack. Reset cancels a job without
// waiting for Opus and without allowing that job to clear a newer PCM cache.
class WakeWordAudioEncoder {
public:
    ~WakeWordAudioEncoder();
    bool Initialize(size_t sample_count, size_t stack_size = 4096 * 6);
    void Store(const int16_t* data, size_t samples);
    void Encode(const esp_opus_enc_config_t& config);
    bool GetOpus(std::vector<uint8_t>& opus);
    void Reset();

private:
    size_t stack_size_ = 4096 * 6;
    WakeWordAudioCache cache_;
    TaskHandle_t task_ = nullptr;
    StaticTask_t* task_buffer_ = nullptr;
    StackType_t* task_stack_ = nullptr;
    std::mutex mutex_;
    std::condition_variable cv_;
    std::deque<std::vector<uint8_t>> opus_;
    esp_opus_enc_config_t config_ = {};
    uint32_t generation_ = 0;
    bool requested_ = false;
    bool completed_ = true;
    bool stopping_ = false;
    bool stopped_ = false;

    void Run();
};

#endif
