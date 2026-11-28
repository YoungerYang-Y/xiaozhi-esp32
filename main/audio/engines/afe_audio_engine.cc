#include "afe_audio_engine.h"

#include <cassert>
#include <cstring>
#include <string_view>
#include <vector>

#include <esp_heap_caps.h>
#include <esp_log.h>
#include <esp_timer.h>
#include <esp_vadn_models.h>

#include "audio_service.h"
#include "wake_words/custom_wake_word.h"

#define TAG "AfeAudioEngine"

#if CONFIG_USE_AUDIO_PROCESSOR
static constexpr bool kUseAfeForVoiceProcessing = true;
#else
static constexpr bool kUseAfeForVoiceProcessing = false;
#endif

AfeAudioEngine::AfeAudioEngine() { event_group_ = xEventGroupCreate(); }

AfeAudioEngine::~AfeAudioEngine() {
    ResetCapture();
    if (processing_task_ != nullptr) {
        std::unique_lock<std::mutex> lock(task_mutex_);
        stopping_ = true;
        xEventGroupSetBits(event_group_, kStopRequested);
        task_cv_.wait(lock, [this] { return task_stopped_; });
        lock.unlock();
        vTaskDelete(processing_task_);
    }
    custom_wake_word_.reset();
    if (afe_data_ != nullptr) {
        afe_iface_->destroy(afe_data_);
    }
    if (processing_task_stack_ != nullptr) {
        heap_caps_free(processing_task_stack_);
    }
    if (processing_task_buffer_ != nullptr) {
        heap_caps_free(processing_task_buffer_);
    }
    if (owns_models_ && models_ != nullptr) {
        esp_srmodel_deinit(models_);
    }
    if (event_group_ != nullptr) {
        vEventGroupDelete(event_group_);
    }
}

bool AfeAudioEngine::Initialize(AudioCodec* codec, int frame_duration_ms,
                                srmodel_list_t* models_list) {
    if (afe_data_ != nullptr || (codec_ != nullptr && !kUseAfeForVoiceProcessing &&
                                 wake_detector_ == WakeDetector::kNone)) {
        return true;
    }
    if (codec == nullptr) {
        ESP_LOGE(TAG, "Codec is null");
        return false;
    }

    codec_ = codec;
    frame_samples_ = frame_duration_ms * 16000 / 1000;
    output_buffer_.reserve(frame_samples_);

    if (models_list == nullptr) {
        models_ = esp_srmodel_init("model");
        owns_models_ = models_ != nullptr;
    } else {
        models_ = models_list;
    }

    char* wakenet_model_name = nullptr;
    char* multinet_model_name = nullptr;
    if (models_ != nullptr && models_->num > 0) {
        wakenet_model_name = esp_srmodel_filter(models_, ESP_WN_PREFIX, nullptr);
        multinet_model_name = esp_srmodel_filter(models_, ESP_MN_PREFIX, nullptr);
        for (int i = 0; i < models_->num; ++i) {
            ESP_LOGI(TAG, "Model %d: %s", i, models_->model_name[i]);
        }
    }

    if (multinet_model_name != nullptr) {
        wake_detector_ = WakeDetector::kMultiNet;
        custom_wake_word_ = std::make_unique<CustomWakeWord>();
        custom_wake_word_->OnWakeWordDetected([this](const std::string& wake_word) {
            last_detected_wake_word_ = wake_word;
            xEventGroupClearBits(event_group_, kWakeWordEnabled);
            UpdateActiveState();
            if (wake_word_detected_callback_) {
                wake_word_detected_callback_(wake_word);
            }
        });
        custom_wake_word_->OnLocalCommand([this](const std::string& action, uint64_t generation) {
            xEventGroupClearBits(event_group_, kLocalCommandsEnabled);
            UpdateActiveState();
            if (local_command_callback_) {
                local_command_callback_(action, generation);
            }
        });
        if (!custom_wake_word_->Initialize(codec_, models_)) {
            ESP_LOGE(TAG, "Failed to initialize MultiNet wake-word detector");
            custom_wake_word_.reset();
            wake_detector_ = WakeDetector::kNone;
            return false;
        }
    } else if (wakenet_model_name != nullptr) {
        wake_detector_ = WakeDetector::kWakeNet;
#if CONFIG_SEND_WAKE_WORD_DATA
        if (!wake_word_encoder_.Initialize(16000 * 2)) {
            ESP_LOGW(TAG, "Wake-word audio upload disabled: PSRAM cache allocation failed");
        }
#endif
    }

    const bool needs_afe = kUseAfeForVoiceProcessing || wake_detector_ != WakeDetector::kNone;
    if (!needs_afe) {
        ESP_LOGI(TAG, "Initialized as raw engine because AFE features are disabled");
        return true;
    }

    int ref_num = codec_->input_reference() ? 1 : 0;
    std::string input_format;
    for (int i = 0; i < codec_->input_channels() - ref_num; ++i) {
        input_format.push_back('M');
    }
    for (int i = 0; i < ref_num; ++i) {
        input_format.push_back('R');
    }

    char* vad_model_name =
        models_ == nullptr ? nullptr : esp_srmodel_filter(models_, ESP_VADN_PREFIX, nullptr);
    afe_config_t* afe_config =
        afe_config_init(input_format.c_str(), models_, AFE_TYPE_FD, AFE_MODE_LOW_COST);
    if (afe_config == nullptr) {
        ESP_LOGE(TAG, "Failed to create AFE configuration");
        return false;
    }

    afe_config->aec_init = codec_->input_reference();
    afe_config->aec_mode = AEC_MODE_FD_LOW_COST;
    afe_config->aec_nlp_level = AEC_NLP_LEVEL_VERYAGGR;
    afe_config->ns_init = false;
    afe_config->vad_init = kUseAfeForVoiceProcessing;
    afe_config->vad_mode = VAD_MODE_0;
    afe_config->vad_min_noise_ms = 100;
    if (vad_model_name != nullptr) {
        afe_config->vad_model_name = vad_model_name;
    }
    afe_config->wakenet_init = wake_detector_ == WakeDetector::kWakeNet;
    afe_config->wakenet_model_name = nullptr;
    afe_config->wakenet_model_name_2 = nullptr;
    if (wake_detector_ == WakeDetector::kWakeNet && models_ != nullptr) {
        std::vector<char*> wakenet_models;
        for (int i = 0; i < models_->num; ++i) {
            char* name = models_->model_name[i];
            if (name != nullptr && strncmp(name, ESP_WN_PREFIX, strlen(ESP_WN_PREFIX)) == 0) {
                wakenet_models.push_back(name);
            }
        }
        if (!wakenet_models.empty()) {
            afe_config->wakenet_model_name = wakenet_models[0];
        }
        if (wakenet_models.size() > 1) {
            afe_config->wakenet_model_name_2 = wakenet_models[1];
        }
        // Only the two models selected above are active in the AFE.
        wake_words_.clear();
        for (auto name : {afe_config->wakenet_model_name, afe_config->wakenet_model_name_2}) {
            if (name == nullptr) {
                continue;
            }
            auto words = esp_srmodel_get_wake_words(models_, name);
            if (words == nullptr) {
                continue;
            }
            std::string_view remaining(words);
            while (!remaining.empty()) {
                const auto end = remaining.find(';');
                const auto word = remaining.substr(0, end);
                if (!word.empty()) {
                    wake_words_.emplace_back(word);
                }
                if (end == std::string_view::npos) {
                    break;
                }
                remaining.remove_prefix(end + 1);
            }
        }
    }
    afe_config->agc_init = false;
    afe_config->memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_PSRAM;

    ESP_LOGI(TAG, "Before AFE create: free=%u min=%u largest=%u",
             heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
             heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
    afe_iface_ = esp_afe_handle_from_config(afe_config);
    if (afe_iface_ != nullptr) {
        afe_data_ = afe_iface_->create_from_config(afe_config);
    }
    afe_config_free(afe_config);

    if (afe_iface_ == nullptr || afe_data_ == nullptr) {
        ESP_LOGE(TAG, "Failed to create FD AFE instance");
        afe_iface_ = nullptr;
        afe_data_ = nullptr;
        return false;
    }

    if (wake_detector_ == WakeDetector::kWakeNet) {
        afe_iface_->disable_wakenet(afe_data_);
    }
    if (codec_->input_reference()) {
        afe_iface_->disable_aec(afe_data_);
    }
    afe_iface_->print_pipeline(afe_data_);

    if (processing_task_stack_ == nullptr) {
        processing_task_stack_ = static_cast<StackType_t*>(
            heap_caps_malloc(kProcessingTaskStackSize, MALLOC_CAP_SPIRAM));
    }
    if (processing_task_buffer_ == nullptr) {
        processing_task_buffer_ =
            static_cast<StaticTask_t*>(heap_caps_malloc(sizeof(StaticTask_t), MALLOC_CAP_INTERNAL));
    }
    if (processing_task_stack_ == nullptr || processing_task_buffer_ == nullptr) {
        ESP_LOGE(TAG, "Failed to allocate AFE processing task memory");
        afe_iface_->destroy(afe_data_);
        afe_data_ = nullptr;
        afe_iface_ = nullptr;
        return false;
    }

    processing_task_ = xTaskCreateStatic(
        [](void* arg) {
            auto* engine = static_cast<AfeAudioEngine*>(arg);
            engine->ProcessingTask();
            vTaskSuspend(nullptr);
        },
        "audio_afe", kProcessingTaskStackSize, this, 3, processing_task_stack_,
        processing_task_buffer_);
    if (processing_task_ == nullptr) {
        ESP_LOGE(TAG, "Failed to create AFE processing task, internal free=%u largest=%u",
                 heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                 heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
        afe_iface_->destroy(afe_data_);
        afe_data_ = nullptr;
        afe_iface_ = nullptr;
        return false;
    }

    const char* detector = wake_detector_ == WakeDetector::kWakeNet
                               ? "WakeNet"
                               : (wake_detector_ == WakeDetector::kMultiNet ? "MultiNet" : "none");
    ESP_LOGI(TAG, "Initialized FD AFE, detector: %s, NS: off, feed: %d, fetch: %d", detector,
             afe_iface_->get_feed_chunksize(afe_data_), afe_iface_->get_fetch_chunksize(afe_data_));
    ESP_LOGI(TAG, "After AFE create: free=%u min=%u largest=%u",
             heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
             heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
    return true;
}

void AfeAudioEngine::Feed(std::vector<int16_t>&& data) {
    EventBits_t bits = xEventGroupGetBits(event_group_);
    if ((bits & kVoiceProcessingEnabled) && !kUseAfeForVoiceProcessing) {
        std::lock_guard<std::mutex> callback_lock(callback_mutex_);
        if (xEventGroupGetBits(event_group_) & kVoiceProcessingEnabled) {
            OutputRawAudio(data);
        }
    }
    if (afe_data_ == nullptr || (bits & kAfeActive) == 0) {
        return;
    }

    std::lock_guard<std::mutex> lock(input_buffer_mutex_);
    if ((xEventGroupGetBits(event_group_) & kAfeActive) == 0) {
        return;
    }
    input_buffer_.insert(input_buffer_.end(), data.begin(), data.end());
    size_t chunk_size = afe_iface_->get_feed_chunksize(afe_data_) * codec_->input_channels();
    while (input_buffer_.size() >= chunk_size) {
        afe_iface_->feed(afe_data_, input_buffer_.data());
        input_buffer_.erase(input_buffer_.begin(), input_buffer_.begin() + chunk_size);
    }
}

void AfeAudioEngine::EnableWakeWordDetection(bool enable) {
    std::lock_guard<std::mutex> callback_lock(callback_mutex_);
    if (!HasWakeWord()) {
        return;
    }

    // WakeNet enable/disable on the AFE instance is applied by ProcessingTask
    // (see ApplyAfeControls), driven by the kWakeWordEnabled bit.
    if (enable) {
        if (wake_detector_ == WakeDetector::kMultiNet) {
            custom_wake_word_->Start();
        }
        xEventGroupSetBits(event_group_, kWakeWordEnabled);
    } else {
        control_generation_.fetch_add(1);
        reset_pending_ = true;
        xEventGroupClearBits(event_group_, kWakeWordEnabled);
        if (wake_detector_ == WakeDetector::kMultiNet) {
            custom_wake_word_->Stop();
        }
    }
    UpdateActiveState();
}

void AfeAudioEngine::EnableVoiceProcessing(bool enable) {
    std::lock_guard<std::mutex> callback_lock(callback_mutex_);
    if (enable) {
        xEventGroupSetBits(event_group_, kVoiceProcessingEnabled);
    } else {
        control_generation_.fetch_add(1);
        reset_pending_ = true;
        xEventGroupClearBits(event_group_, kVoiceProcessingEnabled);
        is_speaking_ = false;
    }
    UpdateActiveState();
}

void AfeAudioEngine::ResetCapture() {
    std::lock_guard<std::mutex> callback_lock(callback_mutex_);
    xEventGroupClearBits(event_group_, kWakeWordEnabled | kVoiceProcessingEnabled |
                                           kLocalCommandsEnabled | kAfeActive);
    control_generation_.fetch_add(1);
    is_speaking_ = false;
    output_reset_pending_ = true;
    reset_pending_ = true;
    afe_control_dirty_ = true;
    {
        std::lock_guard<std::mutex> input_lock(input_buffer_mutex_);
        input_buffer_.clear();
    }
    wake_word_encoder_.Reset();
    if (custom_wake_word_) {
        custom_wake_word_->ResetCapture();
    }
}

bool AfeAudioEngine::HasLocalCommands() const {
    return custom_wake_word_ && custom_wake_word_->HasLocalCommands();
}

bool AfeAudioEngine::StartLocalCommands(uint64_t generation) {
    std::lock_guard<std::mutex> callback_lock(callback_mutex_);
    if (!HasLocalCommands() || generation == 0) {
        return false;
    }
    // No wake callbacks or PCM output may escape the command-only phase.
    control_generation_.fetch_add(1);
    reset_pending_ = true;
    xEventGroupClearBits(event_group_, kWakeWordEnabled | kVoiceProcessingEnabled);
    if (!custom_wake_word_->StartLocalCommands(generation)) {
        return false;
    }
    xEventGroupSetBits(event_group_, kLocalCommandsEnabled);
    UpdateActiveState();
    return true;
}

void AfeAudioEngine::StopLocalCommands() {
    std::lock_guard<std::mutex> callback_lock(callback_mutex_);
    if ((xEventGroupGetBits(event_group_) & kLocalCommandsEnabled) == 0) {
        return;
    }
    control_generation_.fetch_add(1);
    reset_pending_ = true;
    xEventGroupClearBits(event_group_, kLocalCommandsEnabled);
    if (custom_wake_word_) {
        custom_wake_word_->Stop();
    }
    UpdateActiveState();
}

void AfeAudioEngine::OnLocalCommand(std::function<void(const std::string&, uint64_t)> callback) {
    local_command_callback_ = std::move(callback);
}

void AfeAudioEngine::EnableDeviceAec(bool enable) {
    device_aec_enabled_.store(enable);
    if (enable && (codec_ == nullptr || !codec_->input_reference())) {
        ESP_LOGW(TAG, "Device AEC requires a playback reference channel");
    }
    UpdateAecState();
}

bool AfeAudioEngine::HasWakeWord() const { return wake_detector_ != WakeDetector::kNone; }

std::vector<std::string> AfeAudioEngine::GetWakeWords() const {
    return custom_wake_word_ ? custom_wake_word_->GetWakeWords() : wake_words_;
}

bool AfeAudioEngine::IsWakeWordDetectionEnabled() const {
    return event_group_ != nullptr && (xEventGroupGetBits(event_group_) & kWakeWordEnabled) != 0;
}

bool AfeAudioEngine::IsVoiceProcessingEnabled() const {
    return event_group_ != nullptr &&
           (xEventGroupGetBits(event_group_) & kVoiceProcessingEnabled) != 0;
}

size_t AfeAudioEngine::GetFeedSize() const {
    return afe_data_ == nullptr ? frame_samples_ : afe_iface_->get_feed_chunksize(afe_data_);
}

void AfeAudioEngine::OnWakeWordDetected(
    std::function<void(const std::string& wake_word)> callback) {
    wake_word_detected_callback_ = std::move(callback);
}

void AfeAudioEngine::OnOutput(std::function<void(std::vector<int16_t>&& data)> callback) {
    output_callback_ = std::move(callback);
}

void AfeAudioEngine::OnVadStateChange(std::function<void(bool speaking)> callback) {
    vad_state_change_callback_ = std::move(callback);
}

void AfeAudioEngine::UpdateActiveState() {
    EventBits_t bits = xEventGroupGetBits(event_group_);
    const bool afe_active =
        afe_data_ != nullptr && ((bits & (kWakeWordEnabled | kLocalCommandsEnabled)) ||
                                 (kUseAfeForVoiceProcessing && (bits & kVoiceProcessingEnabled)));
    if (afe_active) {
        xEventGroupSetBits(event_group_, kAfeActive);
    } else {
        xEventGroupClearBits(event_group_, kAfeActive);
        control_generation_.fetch_add(1);
        std::lock_guard<std::mutex> lock(input_buffer_mutex_);
        input_buffer_.clear();
        if (afe_data_ != nullptr) {
            // Don't call reset_buffer() here: this runs in the main task while
            // ProcessingTask may be inside fetch_with_delay() on the same AFE
            // instance, and a concurrent reset corrupts the ring buffer state.
            // Defer the reset to ProcessingTask, which owns the fetch side.
            reset_pending_ = true;
        }
    }
    if ((bits & kVoiceProcessingEnabled) == 0) {
        // output_buffer_ is owned by the task that produces output frames,
        // so let that task clear it instead of racing with it here.
        output_reset_pending_ = true;
    }
    afe_control_dirty_ = true;
}

void AfeAudioEngine::UpdateAecState() {
    if (afe_data_ == nullptr || codec_ == nullptr || !codec_->input_reference()) {
        return;
    }
    afe_control_dirty_ = true;
}

void AfeAudioEngine::ApplyAfeControls() {
    EventBits_t bits = xEventGroupGetBits(event_group_);
    if (wake_detector_ == WakeDetector::kWakeNet) {
        if (bits & kWakeWordEnabled) {
            afe_iface_->enable_wakenet(afe_data_);
        } else {
            afe_iface_->disable_wakenet(afe_data_);
        }
    }
    if (codec_->input_reference()) {
        const bool enable_aec = (bits & (kWakeWordEnabled | kLocalCommandsEnabled)) ||
                                (device_aec_enabled_.load() && (bits & kVoiceProcessingEnabled));
        if (enable_aec) {
            afe_iface_->enable_aec(afe_data_);
        } else {
            afe_iface_->disable_aec(afe_data_);
        }
    }
}

void AfeAudioEngine::ApplyPendingReset() {
    if (!reset_pending_.exchange(false)) {
        return;
    }
    // Discard audio recorded before (re)activation. Holding input_buffer_mutex_
    // serializes the reset against Feed(); fetch/reset both run in this task.
    std::lock_guard<std::mutex> lock(input_buffer_mutex_);
    input_buffer_.clear();
    afe_iface_->reset_buffer(afe_data_);
}

void AfeAudioEngine::ProcessingTask() {
    while (!stopping_.load()) {
        xEventGroupWaitBits(event_group_, kAfeActive | kStopRequested, pdFALSE, pdFALSE,
                            portMAX_DELAY);
        if (stopping_.load()) {
            break;
        }
        uint32_t generation;
        {
            std::lock_guard<std::mutex> callback_lock(callback_mutex_);
            // Snapshot the generation together with the pending reset. Otherwise
            // reset/re-enable between these steps can label old ring data with
            // the new generation before fetch even starts.
            ApplyPendingReset();
            if ((xEventGroupGetBits(event_group_) & kAfeActive) == 0) {
                continue;
            }
            if (afe_control_dirty_.exchange(false)) {
                // WakeNet/AEC toggles are owned by the fetch task as well.
                ApplyAfeControls();
            }
            generation = control_generation_.load();
        }
        auto* result = afe_iface_->fetch_with_delay(afe_data_, pdMS_TO_TICKS(50));
        // Fence both the generation check and all callback submissions. Merely
        // checking the generation before a handler leaves a disable race.
        std::unique_lock<std::mutex> callback_lock(callback_mutex_);
        if (generation != control_generation_.load() ||
            (xEventGroupGetBits(event_group_) & kAfeActive) == 0) {
            // A disable/re-enable may make an old blocked fetch return after the
            // AFE is active again. Reset immediately and never process that frame.
            callback_lock.unlock();
            ApplyPendingReset();
            continue;
        }
        if (result == nullptr || result->ret_value == ESP_FAIL) {
            if (result != nullptr) {
                ESP_LOGW(TAG, "AFE fetch failed: %d", result->ret_value);
            }
            continue;
        }

        EventBits_t bits = xEventGroupGetBits(event_group_);
        if (bits & (kWakeWordEnabled | kLocalCommandsEnabled)) {
            HandleWakeWordResult(result);
        }
        if (kUseAfeForVoiceProcessing && (bits & kVoiceProcessingEnabled)) {
            HandleVoiceResult(result);
        }
    }
    std::lock_guard<std::mutex> lock(task_mutex_);
    task_stopped_ = true;
    task_cv_.notify_all();
}

void AfeAudioEngine::HandleWakeWordResult(const afe_fetch_result_t* result) {
    if (wake_detector_ == WakeDetector::kMultiNet) {
        custom_wake_word_->FeedMono(result->data, result->data_size / sizeof(int16_t));
        return;
    }

#if CONFIG_SEND_WAKE_WORD_DATA
    wake_word_encoder_.Store(result->data, result->data_size / sizeof(int16_t));
#endif
    if (result->wakeup_state != WAKENET_DETECTED) {
        return;
    }

    int model_index = result->wakenet_model_index - 1;
    if (model_index < 0 || model_index >= static_cast<int>(wake_words_.size())) {
        ESP_LOGE(TAG, "Invalid WakeNet model index: %d", result->wakenet_model_index);
        return;
    }

    last_detected_wake_word_ = wake_words_[model_index];
    xEventGroupClearBits(event_group_, kWakeWordEnabled);
    // UpdateActiveState marks the AFE controls dirty; the next loop iteration
    // of ProcessingTask disables WakeNet via ApplyAfeControls.
    UpdateActiveState();
    if (wake_word_detected_callback_) {
        wake_word_detected_callback_(last_detected_wake_word_);
    }
}

void AfeAudioEngine::HandleVoiceResult(const afe_fetch_result_t* result) {
    if (output_reset_pending_.exchange(false)) {
        output_buffer_.clear();
    }
    if (vad_state_change_callback_) {
        if (result->vad_state == VAD_SPEECH && !is_speaking_) {
            is_speaking_ = true;
            vad_state_change_callback_(true);
        } else if (result->vad_state == VAD_SILENCE && is_speaking_) {
            is_speaking_ = false;
            vad_state_change_callback_(false);
        }
    }
    if (!output_callback_) {
        return;
    }

    size_t samples = result->data_size / sizeof(int16_t);
    output_buffer_.insert(output_buffer_.end(), result->data, result->data + samples);
    while (output_buffer_.size() >= static_cast<size_t>(frame_samples_)) {
        if (output_buffer_.size() == static_cast<size_t>(frame_samples_)) {
            output_callback_(std::move(output_buffer_));
            output_buffer_.clear();
            output_buffer_.reserve(frame_samples_);
        } else {
            output_callback_(std::vector<int16_t>(output_buffer_.begin(),
                                                  output_buffer_.begin() + frame_samples_));
            output_buffer_.erase(output_buffer_.begin(), output_buffer_.begin() + frame_samples_);
        }
    }
}

void AfeAudioEngine::OutputRawAudio(const std::vector<int16_t>& data) {
    if (!output_callback_ || codec_ == nullptr) {
        return;
    }
    if (output_reset_pending_.exchange(false)) {
        output_buffer_.clear();
    }
    const size_t channels = codec_->input_channels();
    if (channels <= 1) {
        output_buffer_.insert(output_buffer_.end(), data.begin(), data.end());
    } else {
        for (size_t i = 0; i < data.size(); i += channels) {
            output_buffer_.push_back(data[i]);
        }
    }
    while (output_buffer_.size() >= static_cast<size_t>(frame_samples_)) {
        output_callback_(
            std::vector<int16_t>(output_buffer_.begin(), output_buffer_.begin() + frame_samples_));
        output_buffer_.erase(output_buffer_.begin(), output_buffer_.begin() + frame_samples_);
    }
}

void AfeAudioEngine::EncodeWakeWordData() {
    if (wake_detector_ == WakeDetector::kMultiNet) {
        custom_wake_word_->EncodeWakeWordData();
        return;
    }
    if (wake_detector_ != WakeDetector::kWakeNet) {
        return;
    }

    const esp_opus_enc_config_t config = AS_OPUS_ENC_CONFIG();
    wake_word_encoder_.Encode(config);
}

bool AfeAudioEngine::GetWakeWordOpus(std::vector<uint8_t>& opus) {
    if (wake_detector_ == WakeDetector::kMultiNet) {
        return custom_wake_word_->GetWakeWordOpus(opus);
    }
    if (wake_detector_ != WakeDetector::kWakeNet) {
        return false;
    }
    return wake_word_encoder_.GetOpus(opus);
}
