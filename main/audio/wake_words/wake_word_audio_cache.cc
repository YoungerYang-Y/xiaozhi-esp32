#include "wake_word_audio_cache.h"

#include <algorithm>
#include <cstring>

#include <esp_audio_enc.h>
#include <esp_heap_caps.h>
#include <esp_log.h>

#define TAG "WakeWordCache"

WakeWordAudioCache::~WakeWordAudioCache() {
    if (buffer_ != nullptr) {
        heap_caps_free(buffer_);
    }
}

bool WakeWordAudioCache::Initialize(size_t sample_count) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (buffer_ != nullptr) {
        return capacity_ == sample_count;
    }
    if (sample_count == 0) {
        return false;
    }

    buffer_ = static_cast<int16_t*>(heap_caps_malloc(
        sample_count * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (buffer_ == nullptr) {
        ESP_LOGE(TAG, "Failed to allocate %u bytes in PSRAM",
            static_cast<unsigned>(sample_count * sizeof(int16_t)));
        return false;
    }

    capacity_ = sample_count;
    ESP_LOGI(TAG, "Allocated %u bytes in PSRAM",
        static_cast<unsigned>(capacity_ * sizeof(int16_t)));
    return true;
}

void WakeWordAudioCache::Store(const int16_t* data, size_t samples) {
    if (data == nullptr || samples == 0) {
        return;
    }

    std::lock_guard<std::mutex> lock(mutex_);
    if (buffer_ == nullptr) {
        return;
    }

    if (samples >= capacity_) {
        data += samples - capacity_;
        samples = capacity_;
        std::memcpy(buffer_, data, samples * sizeof(int16_t));
        size_ = capacity_;
        write_position_ = 0;
        return;
    }

    const size_t first = std::min(samples, capacity_ - write_position_);
    std::memcpy(buffer_ + write_position_, data, first * sizeof(int16_t));
    if (samples > first) {
        std::memcpy(buffer_, data + first, (samples - first) * sizeof(int16_t));
    }
    write_position_ = (write_position_ + samples) % capacity_;
    size_ = std::min(capacity_, size_ + samples);
}

size_t WakeWordAudioCache::Read(size_t offset, int16_t* output, size_t samples) const {
    if (output == nullptr || samples == 0) {
        return 0;
    }

    std::lock_guard<std::mutex> lock(mutex_);
    if (buffer_ == nullptr || offset >= size_) {
        return 0;
    }

    samples = std::min(samples, size_ - offset);
    const size_t oldest = (write_position_ + capacity_ - size_) % capacity_;
    const size_t read_position = (oldest + offset) % capacity_;
    const size_t first = std::min(samples, capacity_ - read_position);
    std::memcpy(output, buffer_ + read_position, first * sizeof(int16_t));
    if (samples > first) {
        std::memcpy(output + first, buffer_, (samples - first) * sizeof(int16_t));
    }
    return samples;
}

size_t WakeWordAudioCache::Size() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return size_;
}

void WakeWordAudioCache::Clear() {
    std::lock_guard<std::mutex> lock(mutex_);
    size_ = 0;
    write_position_ = 0;
}

WakeWordAudioEncoder::~WakeWordAudioEncoder() {
    if (task_ != nullptr) {
        std::unique_lock<std::mutex> lock(mutex_);
        stopping_ = true;
        ++generation_;
        xTaskNotifyGive(task_);
        cv_.wait(lock, [this]() { return stopped_; });
        lock.unlock();
        vTaskDelete(task_);
    }
    heap_caps_free(task_stack_);
    heap_caps_free(task_buffer_);
}

bool WakeWordAudioEncoder::Initialize(size_t sample_count, size_t stack_size) {
    std::lock_guard<std::mutex> lock(mutex_);
    stack_size_ = stack_size;
    return cache_.Initialize(sample_count);
}

void WakeWordAudioEncoder::Store(const int16_t* data, size_t samples) {
    std::lock_guard<std::mutex> lock(mutex_);
    cache_.Store(data, samples);
}

void WakeWordAudioEncoder::Reset() {
    std::lock_guard<std::mutex> lock(mutex_);
    ++generation_;
    requested_ = false;
    completed_ = true;
    opus_.clear();
    cache_.Clear();
    cv_.notify_all();
}

void WakeWordAudioEncoder::Encode(const esp_opus_enc_config_t& config) {
    std::lock_guard<std::mutex> lock(mutex_);
    ++generation_;
    opus_.clear();
    config_ = config;
    requested_ = true;
    completed_ = false;
    // Wake waiters from a superseded job as well as consumers of this one.
    cv_.notify_all();
    if (task_ == nullptr) {
        if (task_stack_ == nullptr) {
            task_stack_ =
                static_cast<StackType_t*>(heap_caps_malloc(stack_size_, MALLOC_CAP_SPIRAM));
        }
        if (task_buffer_ == nullptr) {
            task_buffer_ = static_cast<StaticTask_t*>(
                heap_caps_malloc(sizeof(StaticTask_t), MALLOC_CAP_INTERNAL));
        }
        if (task_stack_ != nullptr && task_buffer_ != nullptr) {
            task_ = xTaskCreateStatic(
                [](void* arg) { static_cast<WakeWordAudioEncoder*>(arg)->Run(); },
                "encode_wake_word", stack_size_, this, 2, task_stack_, task_buffer_);
        }
        if (task_ == nullptr) {
            requested_ = false;
            completed_ = true;
            ESP_LOGE(TAG, "Failed to create wake-word encoder task");
            cv_.notify_all();
            return;
        }
    }
    xTaskNotifyGive(task_);
}

void WakeWordAudioEncoder::Run() {
    while (true) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        std::unique_lock<std::mutex> lock(mutex_);
        if (stopping_) {
            stopped_ = true;
            cv_.notify_all();
            lock.unlock();
            // The owner deletes this parked task before releasing its stack.
            vTaskSuspend(nullptr);
            return;
        }
        if (!requested_) {
            continue;
        }
        const uint32_t generation = generation_;
        const size_t cached_samples = cache_.Size();
        auto config = config_;
        requested_ = false;
        lock.unlock();

        void* encoder = nullptr;
        auto ret = esp_opus_enc_open(&config, sizeof(config), &encoder);
        if (encoder != nullptr) {
            int frame_bytes = 0;
            int outbuf_size = 0;
            esp_opus_enc_get_frame_size(encoder, &frame_bytes, &outbuf_size);
            const size_t frame_samples = frame_bytes / sizeof(int16_t);
            std::vector<int16_t> input(frame_samples);
            std::vector<uint8_t> output(outbuf_size);
            for (size_t offset = 0; frame_samples > 0 && offset + frame_samples <= cached_samples;
                 offset += frame_samples) {
                lock.lock();
                const bool current = generation == generation_ && !stopping_;
                const size_t copied =
                    current ? cache_.Read(offset, input.data(), frame_samples) : 0;
                lock.unlock();
                if (copied != frame_samples) {
                    break;
                }
                esp_audio_enc_in_frame_t in = {
                    .buffer = reinterpret_cast<uint8_t*>(input.data()),
                    .len = static_cast<uint32_t>(frame_bytes),
                };
                esp_audio_enc_out_frame_t out = {
                    .buffer = output.data(),
                    .len = static_cast<uint32_t>(outbuf_size),
                    .encoded_bytes = 0,
                };
                ret = esp_opus_enc_process(encoder, &in, &out);
                lock.lock();
                if (generation == generation_ && !stopping_ && ret == ESP_AUDIO_ERR_OK) {
                    opus_.emplace_back(output.data(), output.data() + out.encoded_bytes);
                    cv_.notify_all();
                }
                lock.unlock();
            }
            esp_opus_enc_close(encoder);
        } else {
            ESP_LOGE(TAG, "Failed to create wake-word encoder: %d", ret);
        }

        lock.lock();
        if (generation == generation_) {
            cache_.Clear();
            completed_ = true;
            cv_.notify_all();
        }
    }
}

bool WakeWordAudioEncoder::GetOpus(std::vector<uint8_t>& opus) {
    std::unique_lock<std::mutex> lock(mutex_);
    const uint32_t generation = generation_;
    cv_.wait(lock, [this, generation]() {
        return generation != generation_ || completed_ || !opus_.empty();
    });
    if (generation != generation_ || opus_.empty()) {
        return false;
    }
    opus.swap(opus_.front());
    opus_.pop_front();
    return true;
}
