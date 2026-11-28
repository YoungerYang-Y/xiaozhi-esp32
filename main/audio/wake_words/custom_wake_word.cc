#include "custom_wake_word.h"
#include "assets.h"
#include "audio_service.h"
#include "system_info.h"

#include <esp_log.h>
#include <esp_mn_iface.h>
#include <esp_mn_models.h>
#include <esp_mn_speech_commands.h>
#include <cJSON.h>

#define TAG "CustomWakeWord"

CustomWakeWord::CustomWakeWord() = default;

CustomWakeWord::~CustomWakeWord() {
    Stop();
    if (multinet_model_data_ != nullptr && multinet_ != nullptr) {
        multinet_->destroy(multinet_model_data_);
        multinet_model_data_ = nullptr;
    }

    if (owns_models_ && models_ != nullptr) {
        esp_srmodel_deinit(models_);
    }
}

void CustomWakeWord::ParseWakenetModelConfig() {
    // Read index.json
    auto& assets = Assets::GetInstance();
    void* ptr = nullptr;
    size_t size = 0;
    if (!assets.GetAssetData("index.json", ptr, size)) {
        ESP_LOGE(TAG, "Failed to read index.json");
        return;
    }
    cJSON* root = cJSON_ParseWithLength(static_cast<char*>(ptr), size);
    if (root == nullptr) {
        ESP_LOGE(TAG, "Failed to parse index.json");
        return;
    }
    cJSON* multinet_model = cJSON_GetObjectItem(root, "multinet_model");
    if (cJSON_IsObject(multinet_model)) {
        cJSON* language = cJSON_GetObjectItem(multinet_model, "language");
        cJSON* duration = cJSON_GetObjectItem(multinet_model, "duration");
        cJSON* threshold = cJSON_GetObjectItem(multinet_model, "threshold");
        cJSON* commands = cJSON_GetObjectItem(multinet_model, "commands");
        if (cJSON_IsString(language)) {
            language_ = language->valuestring;
        }
        if (cJSON_IsNumber(duration)) {
            duration_ = duration->valueint;
        }
        if (cJSON_IsNumber(threshold)) {
            threshold_ = threshold->valuedouble;
        }
        if (cJSON_IsArray(commands)) {
            for (int i = 0; i < cJSON_GetArraySize(commands); i++) {
                cJSON* command = cJSON_GetArrayItem(commands, i);
                if (cJSON_IsObject(command)) {
                    cJSON* command_name = cJSON_GetObjectItem(command, "command");
                    cJSON* text = cJSON_GetObjectItem(command, "text");
                    cJSON* action = cJSON_GetObjectItem(command, "action");
                    if (cJSON_IsString(command_name) && cJSON_IsString(text) &&
                        cJSON_IsString(action)) {
                        commands_.push_back(
                            {command_name->valuestring, text->valuestring, action->valuestring});
                        ESP_LOGI(TAG, "Command: %s, Text: %s, Action: %s",
                                 command_name->valuestring, text->valuestring, action->valuestring);
                    }
                }
            }
        }
    }
    cJSON_Delete(root);
}

bool CustomWakeWord::Initialize(AudioCodec* codec, srmodel_list_t* models_list) {
    codec_ = codec;
    commands_.clear();

    if (models_list == nullptr) {
        language_ = "cn";
        models_ = esp_srmodel_init("model");
        owns_models_ = models_ != nullptr;
#ifdef CONFIG_CUSTOM_WAKE_WORD
        threshold_ = CONFIG_CUSTOM_WAKE_WORD_THRESHOLD / 100.0f;
        commands_.push_back({CONFIG_CUSTOM_WAKE_WORD, CONFIG_CUSTOM_WAKE_WORD_DISPLAY, "wake"});
#endif
    } else {
        models_ = models_list;
        ParseWakenetModelConfig();
    }

    if (models_ == nullptr || models_->num == -1) {
        ESP_LOGE(TAG, "Failed to initialize wakenet model");
        return false;
    }

    // 初始化 multinet (命令词识别)
    mn_name_ = esp_srmodel_filter(models_, ESP_MN_PREFIX, language_.c_str());
    if (mn_name_ == nullptr) {
        ESP_LOGW(TAG, "Language '%s' multinet not found, falling back to any multinet model",
                 language_.c_str());
        mn_name_ = esp_srmodel_filter(models_, ESP_MN_PREFIX, NULL);
    }
    if (mn_name_ == nullptr) {
        ESP_LOGE(TAG, "Failed to initialize multinet, mn_name is nullptr");
        ESP_LOGI(TAG,
                 "Please refer to https://pcn7cs20v8cr.feishu.cn/wiki/CpQjwQsCJiQSWSkYEvrcxcbVnwh "
                 "to add custom wake word");
        return false;
    }

    multinet_ = esp_mn_handle_from_name(mn_name_);
    if (multinet_ == nullptr) {
        return false;
    }
    multinet_model_data_ = multinet_->create(mn_name_, duration_);
    if (multinet_model_data_ == nullptr) {
        return false;
    }
    multinet_->set_det_threshold(multinet_model_data_, threshold_);
    input_buffer_.reserve(multinet_->get_samp_chunksize(multinet_model_data_));
    esp_mn_commands_clear();
    for (int i = 0; i < commands_.size(); i++) {
        if (esp_mn_commands_add(i + 1, commands_[i].command.c_str()) != ESP_OK) {
            ESP_LOGE(TAG, "Failed to register MultiNet command %d", i + 1);
            multinet_->destroy(multinet_model_data_);
            multinet_model_data_ = nullptr;
            return false;
        }
    }
    if (esp_mn_commands_update() != nullptr) {
        ESP_LOGE(TAG, "MultiNet rejected one or more command phrases");
        multinet_->destroy(multinet_model_data_);
        multinet_model_data_ = nullptr;
        return false;
    }

    multinet_->print_active_speech_commands(multinet_model_data_);
#if CONFIG_SEND_WAKE_WORD_DATA
    if (!wake_word_encoder_.Initialize(16000 * 2, 4096 * 7)) {
        ESP_LOGW(TAG, "Wake-word audio upload disabled: PSRAM cache allocation failed");
    }
#endif
    return true;
}

void CustomWakeWord::OnWakeWordDetected(
    std::function<void(const std::string& wake_word)> callback) {
    wake_word_detected_callback_ = callback;
}

std::vector<std::string> CustomWakeWord::GetWakeWords() const {
    std::vector<std::string> words;
    if (multinet_model_data_ == nullptr) {
        return words;
    }
    for (const auto& command : commands_) {
        if (command.action == "wake" && !command.text.empty()) {
            words.push_back(command.text);
        }
    }
    return words;
}

bool CustomWakeWord::IsLocalAction(const std::string& action) {
    return action == "screen_on" || action == "screen_off" || action == "volume_up" ||
           action == "volume_down" || action == "brightness_up" || action == "brightness_down" ||
           action == "device_status" || action == "cancel";
}

bool CustomWakeWord::HasLocalCommands() const {
    if (multinet_model_data_ == nullptr) {
        return false;
    }
    for (const auto& command : commands_) {
        if (IsLocalAction(command.action)) {
            return true;
        }
    }
    return false;
}

bool CustomWakeWord::StartLocalCommands(uint64_t generation) {
    if (generation == 0 || !HasLocalCommands()) {
        return false;
    }
    std::lock_guard<std::mutex> lock(input_buffer_mutex_);
    input_buffer_.clear();
    multinet_->clean(multinet_model_data_);
    local_generation_ = generation;
    running_ = true;
    return true;
}

void CustomWakeWord::OnLocalCommand(std::function<void(const std::string&, uint64_t)> callback) {
    local_command_callback_ = std::move(callback);
}

void CustomWakeWord::Start() {
    std::lock_guard<std::mutex> lock(input_buffer_mutex_);
    input_buffer_.clear();
    if (multinet_model_data_ != nullptr) {
        multinet_->clean(multinet_model_data_);
    }
    local_generation_ = 0;
    running_ = true;
}

void CustomWakeWord::Stop() {
    running_ = false;

    std::lock_guard<std::mutex> lock(input_buffer_mutex_);
    input_buffer_.clear();
    local_generation_ = 0;
}

void CustomWakeWord::ResetCapture() {
    Stop();
    // Stop waits for a current detect/callback before the shared encoder cache
    // is invalidated. The old encoder never clears a subsequent session.
    wake_word_encoder_.Reset();
    std::lock_guard<std::mutex> lock(input_buffer_mutex_);
    if (multinet_model_data_ != nullptr) {
        multinet_->clean(multinet_model_data_);
    }
}

void CustomWakeWord::Feed(const std::vector<int16_t>& data) {
    FeedSamples(data.data(), data.size(), false);
}

void CustomWakeWord::FeedMono(const int16_t* data, size_t samples) {
    FeedSamples(data, samples, true);
}

void CustomWakeWord::FeedSamples(const int16_t* data, size_t samples, bool mono) {
    if (multinet_model_data_ == nullptr || data == nullptr || samples == 0) {
        return;
    }

    std::lock_guard<std::mutex> lock(input_buffer_mutex_);
    // Check running state inside lock to avoid TOCTOU race with Stop()
    if (!running_) {
        return;
    }

    // If input channels is 2, we need to fetch the left channel data
    if (!mono && codec_->input_channels() > 1) {
        for (size_t i = 0; i < samples; i += codec_->input_channels()) {
            input_buffer_.push_back(data[i]);
        }
    } else {
        input_buffer_.insert(input_buffer_.end(), data, data + samples);
    }

    int chunksize = multinet_->get_samp_chunksize(multinet_model_data_);
    while (input_buffer_.size() >= chunksize) {
#if CONFIG_SEND_WAKE_WORD_DATA
        if (local_generation_ == 0) {
            wake_word_encoder_.Store(input_buffer_.data(), chunksize);
        }
#endif

        esp_mn_state_t mn_state = multinet_->detect(multinet_model_data_, input_buffer_.data());

        if (mn_state == ESP_MN_STATE_DETECTED) {
            esp_mn_results_t* mn_result = multinet_->get_results(multinet_model_data_);
            for (int i = 0; mn_result != nullptr && i < mn_result->num && running_; i++) {
                const int id = mn_result->command_id[i];
                if (id <= 0 || id > static_cast<int>(commands_.size())) {
                    continue;
                }
                const auto& command = commands_[id - 1];
                if (local_generation_ != 0 && IsLocalAction(command.action)) {
                    const auto generation = local_generation_;
                    running_ = false;
                    input_buffer_.clear();
                    if (local_command_callback_) {
                        local_command_callback_(command.action, generation);
                    }
                } else if (local_generation_ == 0 && command.action == "wake") {
                    last_detected_wake_word_ = command.text;
                    running_ = false;
                    input_buffer_.clear();
                    if (wake_word_detected_callback_) {
                        wake_word_detected_callback_(last_detected_wake_word_);
                    }
                }
            }
            multinet_->clean(multinet_model_data_);
        } else if (mn_state == ESP_MN_STATE_TIMEOUT) {
            ESP_LOGD(TAG, "Command word detection timeout, cleaning state");
            multinet_->clean(multinet_model_data_);
        }

        if (!running_) {
            break;
        }
        input_buffer_.erase(input_buffer_.begin(), input_buffer_.begin() + chunksize);
    }
}

size_t CustomWakeWord::GetFeedSize() {
    if (multinet_model_data_ == nullptr) {
        return 0;
    }
    return multinet_->get_samp_chunksize(multinet_model_data_);
}

void CustomWakeWord::EncodeWakeWordData() {
    const esp_opus_enc_config_t config = AS_OPUS_ENC_CONFIG();
    wake_word_encoder_.Encode(config);
}

bool CustomWakeWord::GetWakeWordOpus(std::vector<uint8_t>& opus) {
    return wake_word_encoder_.GetOpus(opus);
}
