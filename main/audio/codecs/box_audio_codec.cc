#include "box_audio_codec.h"
#include "settings.h"

#include <driver/i2c_master.h>
#include <driver/i2s_tdm.h>
#include <esp_log.h>

#define TAG "BoxAudioCodec"

BoxAudioCodec::BoxAudioCodec(void* i2c_master_handle, int input_sample_rate, int output_sample_rate,
                             gpio_num_t mclk, gpio_num_t bclk, gpio_num_t ws, gpio_num_t dout,
                             gpio_num_t din, gpio_num_t pa_pin, uint8_t es8311_addr,
                             uint8_t es7210_addr, bool input_reference, float input_gain,
                             int reference_gain_channel, float reference_gain, float pa_voltage) {
    duplex_ = true;                              // 是否双工
    input_reference_ = input_reference;          // 是否使用参考输入，实现回声消除
    input_channels_ = input_reference_ ? 2 : 1;  // 输入通道数
    input_sample_rate_ = input_sample_rate;
    output_sample_rate_ = output_sample_rate;
    input_gain_ = input_gain;
    reference_gain_channel_ = reference_gain_channel;
    reference_gain_ = reference_gain;

    CreateDuplexChannels(mclk, bclk, ws, dout, din);

    // Do initialize of related interface: data_if, ctrl_if and gpio_if
    audio_codec_i2s_cfg_t i2s_cfg = {
        .port = I2S_NUM_0,
        .rx_handle = rx_handle_,
        .tx_handle = tx_handle_,
    };
    data_if_ = audio_codec_new_i2s_data(&i2s_cfg);
    assert(data_if_ != NULL);

    // Output
    audio_codec_i2c_cfg_t i2c_cfg = {
        .port = (i2c_port_t)1,
        .addr = es8311_addr,
        .bus_handle = i2c_master_handle,
    };
    out_ctrl_if_ = audio_codec_new_i2c_ctrl(&i2c_cfg);
    assert(out_ctrl_if_ != NULL);

    gpio_if_ = audio_codec_new_gpio();
    assert(gpio_if_ != NULL);

    es8311_codec_cfg_t es8311_cfg = {};
    es8311_cfg.ctrl_if = out_ctrl_if_;
    es8311_cfg.gpio_if = gpio_if_;
    es8311_cfg.codec_mode = ESP_CODEC_DEV_WORK_MODE_DAC;
    es8311_cfg.pa_pin = pa_pin;
    es8311_cfg.use_mclk = true;
    es8311_cfg.hw_gain.pa_voltage = pa_voltage;
    es8311_cfg.hw_gain.codec_dac_voltage = 3.3;
    out_codec_if_ = es8311_codec_new(&es8311_cfg);
    assert(out_codec_if_ != NULL);

    esp_codec_dev_cfg_t dev_cfg = {
        .dev_type = ESP_CODEC_DEV_TYPE_OUT,
        .codec_if = out_codec_if_,
        .data_if = data_if_,
    };
    output_dev_ = esp_codec_dev_new(&dev_cfg);
    assert(output_dev_ != NULL);

    // Input
    i2c_cfg.addr = es7210_addr;
    in_ctrl_if_ = audio_codec_new_i2c_ctrl(&i2c_cfg);
    assert(in_ctrl_if_ != NULL);

    es7210_codec_cfg_t es7210_cfg = {};
    es7210_cfg.ctrl_if = in_ctrl_if_;
    es7210_cfg.mic_selected = ES7210_SEL_MIC1 | ES7210_SEL_MIC2 | ES7210_SEL_MIC3 | ES7210_SEL_MIC4;
    in_codec_if_ = es7210_codec_new(&es7210_cfg);
    assert(in_codec_if_ != NULL);

    dev_cfg.dev_type = ESP_CODEC_DEV_TYPE_IN;
    dev_cfg.codec_if = in_codec_if_;
    input_dev_ = esp_codec_dev_new(&dev_cfg);
    assert(input_dev_ != NULL);

    ESP_LOGI(TAG, "BoxAudioDevice initialized");
}

BoxAudioCodec::~BoxAudioCodec() {
    ESP_ERROR_CHECK(esp_codec_dev_close(output_dev_));
    esp_codec_dev_delete(output_dev_);
    ESP_ERROR_CHECK(esp_codec_dev_close(input_dev_));
    esp_codec_dev_delete(input_dev_);

    audio_codec_delete_codec_if(in_codec_if_);
    audio_codec_delete_ctrl_if(in_ctrl_if_);
    audio_codec_delete_codec_if(out_codec_if_);
    audio_codec_delete_ctrl_if(out_ctrl_if_);
    audio_codec_delete_gpio_if(gpio_if_);
    audio_codec_delete_data_if(data_if_);
}

void BoxAudioCodec::CreateDuplexChannels(gpio_num_t mclk, gpio_num_t bclk, gpio_num_t ws,
                                         gpio_num_t dout, gpio_num_t din) {
    assert(input_sample_rate_ == output_sample_rate_);

    i2s_chan_config_t chan_cfg = {
        .id = I2S_NUM_0,
        .role = I2S_ROLE_MASTER,
        .dma_desc_num = AUDIO_CODEC_DMA_DESC_NUM,
        .dma_frame_num = AUDIO_CODEC_DMA_FRAME_NUM,
        .auto_clear_after_cb = true,
        .auto_clear_before_cb = false,
        .intr_priority = 0,
    };
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, &tx_handle_, &rx_handle_));

    i2s_std_config_t std_cfg = {
        .clk_cfg = {.sample_rate_hz = (uint32_t)output_sample_rate_,
                    .clk_src = I2S_CLK_SRC_DEFAULT,
                    .ext_clk_freq_hz = 0,
                    .mclk_multiple = I2S_MCLK_MULTIPLE_256},
        .slot_cfg = {.data_bit_width = I2S_DATA_BIT_WIDTH_16BIT,
                     .slot_bit_width = I2S_SLOT_BIT_WIDTH_AUTO,
                     .slot_mode = I2S_SLOT_MODE_STEREO,
                     .slot_mask = I2S_STD_SLOT_BOTH,
                     .ws_width = I2S_DATA_BIT_WIDTH_16BIT,
                     .ws_pol = false,
                     .bit_shift = true,
                     .left_align = true,
                     .big_endian = false,
                     .bit_order_lsb = false},
        .gpio_cfg = {.mclk = mclk,
                     .bclk = bclk,
                     .ws = ws,
                     .dout = dout,
                     .din = I2S_GPIO_UNUSED,
                     .invert_flags = {.mclk_inv = false, .bclk_inv = false, .ws_inv = false}}};

    i2s_tdm_config_t tdm_cfg = {
        .clk_cfg =
            {
                .sample_rate_hz = (uint32_t)input_sample_rate_,
                .clk_src = I2S_CLK_SRC_DEFAULT,
                .ext_clk_freq_hz = 0,
                .mclk_multiple = I2S_MCLK_MULTIPLE_256,
                .bclk_div = 8,
            },
        .slot_cfg = {.data_bit_width = I2S_DATA_BIT_WIDTH_16BIT,
                     .slot_bit_width = I2S_SLOT_BIT_WIDTH_AUTO,
                     .slot_mode = I2S_SLOT_MODE_STEREO,
                     .slot_mask = i2s_tdm_slot_mask_t(I2S_TDM_SLOT0 | I2S_TDM_SLOT1 |
                                                      I2S_TDM_SLOT2 | I2S_TDM_SLOT3),
                     .ws_width = I2S_TDM_AUTO_WS_WIDTH,
                     .ws_pol = false,
                     .bit_shift = true,
                     .left_align = false,
                     .big_endian = false,
                     .bit_order_lsb = false,
                     .skip_mask = false,
                     .total_slot = I2S_TDM_AUTO_SLOT_NUM},
        .gpio_cfg = {.mclk = mclk,
                     .bclk = bclk,
                     .ws = ws,
                     .dout = I2S_GPIO_UNUSED,
                     .din = din,
                     .invert_flags = {.mclk_inv = false, .bclk_inv = false, .ws_inv = false}}};

    ESP_ERROR_CHECK(i2s_channel_init_std_mode(tx_handle_, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_init_tdm_mode(rx_handle_, &tdm_cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(tx_handle_));
    ESP_ERROR_CHECK(i2s_channel_enable(rx_handle_));
    ESP_LOGI(TAG, "Duplex channels created");
}

void BoxAudioCodec::Start() {
    std::lock_guard<std::mutex> lock(data_if_mutex_);
    AudioCodec::Start();
#if CONFIG_WAVESHARE_1_85C_HOME
    Settings settings("audio");
    int32_t stored_volume = -1;
    auto err = settings.TryGetInt("output_volume", stored_volume);
    output_volume_status_ = {output_volume_, err == ESP_OK && stored_volume == output_volume_,
                             false, err == ESP_ERR_NVS_NOT_FOUND ? ESP_OK : err};
#endif
}

void BoxAudioCodec::SetOutputVolume(int volume) {
#if CONFIG_WAVESHARE_1_85C_HOME
    auto status = SetOutputVolumeChecked(volume);
    if (status.error != ESP_OK) {
        ESP_LOGE(TAG, "Set output volume failed: %d", status.error);
    }
#else
    std::lock_guard<std::mutex> lock(data_if_mutex_);
    ESP_ERROR_CHECK(esp_codec_dev_set_out_vol(output_dev_, volume));
    AudioCodec::SetOutputVolume(volume);
#endif
}

OutputVolumeStatus BoxAudioCodec::SetOutputVolumeChecked(int volume) {
#if CONFIG_WAVESHARE_1_85C_HOME
    std::lock_guard<std::mutex> lock(data_if_mutex_);
    if (volume < 0 || volume > 100) {
        output_volume_status_.error = ESP_ERR_INVALID_ARG;
        return output_volume_status_;
    }

    const int previous_volume = output_volume_;
    if (output_enabled_) {
        auto err = ApplyOutputVolumeChecked(volume);
        if (err != ESP_OK) {
            // An I2C failure can leave registers partially changed. Only a successful
            // explicit restoration establishes that the old setting is still applied.
            output_volume_status_.applied = ApplyOutputVolumeChecked(previous_volume) == ESP_OK;
            output_volume_status_.error = err;
            return output_volume_status_;
        }
    }

    Settings settings("audio", true);
    auto err = settings.TrySetIntAndCommit("output_volume", volume);
    if (err != ESP_OK) {
        // A failed commit may already have written storage. Restore the previous
        // software setting, and keep storage/hardware certainty separate if it fails.
        Settings rollback("audio", true);
        auto rollback_err = rollback.TrySetIntAndCommit("output_volume", previous_volume);
        Settings readback("audio");
        int32_t stored_volume = -1;
        auto read_err = readback.TryGetInt("output_volume", stored_volume);
        output_volume_status_.persisted =
            rollback_err == ESP_OK && read_err == ESP_OK && stored_volume == previous_volume;
        output_volume_status_.applied =
            output_enabled_ && ApplyOutputVolumeChecked(previous_volume) == ESP_OK;
        output_volume_status_.error = err;
        return output_volume_status_;
    }

    output_volume_ = volume;
    output_volume_status_ = {volume, true, output_enabled_,
                             output_needs_close_ ? output_volume_status_.error : ESP_OK};
    return output_volume_status_;
#else
    return AudioCodec::SetOutputVolumeChecked(volume);
#endif
}

esp_err_t BoxAudioCodec::ApplyOutputVolumeChecked(int volume) {
    auto err = esp_codec_dev_set_out_vol(output_dev_, volume);
    if (err != ESP_OK) {
        return err;
    }
    // esp_codec_dev 1.6.2 updates its cached percentage but discards ES8311 set_vol errors.
    // Repeat the hardware write with its default curve, in dB rather than percent.
    // ES8311 applies its PA/DAC gain correction inside set_vol, just as on the first call.
    if (out_codec_if_ == nullptr || out_codec_if_->set_vol == nullptr) {
        return ESP_ERR_NOT_SUPPORTED;
    }
    const float db = volume == 0 ? -96.0f : -50.0f + 0.5f * volume;
    return out_codec_if_->set_vol(out_codec_if_, db);
}

esp_err_t BoxAudioCodec::CloseOutputChecked() {
    auto err = esp_codec_dev_close(output_dev_);
    // esp_codec_dev 1.6.2 discards enable(false)'s result and clears its opened flag.
    // Confirm ES8311 suspension so its enabled flag cannot suppress the next PA start.
    // Do not replay data_if_->enable: the public close already releases that reference.
    esp_err_t codec_err = ESP_ERR_NOT_SUPPORTED;
    if (out_codec_if_ != nullptr && out_codec_if_->enable != nullptr) {
        codec_err = out_codec_if_->enable(out_codec_if_, false);
    }
    if (err == ESP_OK) {
        err = codec_err;
    }
    output_needs_close_ = err != ESP_OK;
    output_volume_status_.applied = false;
    AudioCodec::EnableOutput(false);
    return err;
}

OutputVolumeStatus BoxAudioCodec::GetOutputVolumeStatus() const {
    std::lock_guard<std::mutex> lock(data_if_mutex_);
#if CONFIG_WAVESHARE_1_85C_HOME
    return output_volume_status_;
#else
    return AudioCodec::GetOutputVolumeStatus();
#endif
}

void BoxAudioCodec::EnableInput(bool enable) {
    std::lock_guard<std::mutex> lock(data_if_mutex_);
    if (enable == input_enabled_) {
        return;
    }
    if (enable) {
        esp_codec_dev_sample_info_t fs = {
            .bits_per_sample = 16,
            .channel = 4,
            .channel_mask = ESP_CODEC_DEV_MAKE_CHANNEL_MASK(0),
            .sample_rate = (uint32_t)output_sample_rate_,
            .mclk_multiple = 0,
        };
        if (input_reference_) {
            fs.channel_mask |= ESP_CODEC_DEV_MAKE_CHANNEL_MASK(1);
        }
        ESP_ERROR_CHECK(esp_codec_dev_open(input_dev_, &fs));
        ESP_ERROR_CHECK(esp_codec_dev_set_in_channel_gain(
            input_dev_, ESP_CODEC_DEV_MAKE_CHANNEL_MASK(0), input_gain_));
        if (input_reference_ && reference_gain_channel_ >= 0) {
            // ES7210 gain masks use physical MIC numbering, which differs
            // from the TDM slot order (MIC1, MIC3, MIC2, MIC4).
            ESP_ERROR_CHECK(esp_codec_dev_set_in_channel_gain(
                input_dev_, ESP_CODEC_DEV_MAKE_CHANNEL_MASK(reference_gain_channel_),
                reference_gain_));
        }
    } else {
        ESP_ERROR_CHECK(esp_codec_dev_close(input_dev_));
    }
    AudioCodec::EnableInput(enable);
}

void BoxAudioCodec::EnableOutput(bool enable) {
    std::lock_guard<std::mutex> lock(data_if_mutex_);
#if CONFIG_WAVESHARE_1_85C_HOME
    if (enable) {
        if (output_needs_close_) {
            auto err = CloseOutputChecked();
            if (err != ESP_OK) {
                output_volume_status_.error = err;
                return;
            }
        }
        if (output_enabled_ && output_volume_status_.applied) {
            return;
        }
        if (!output_enabled_) {
            esp_codec_dev_sample_info_t fs = {
                .bits_per_sample = 16,
                .channel = 1,
                .channel_mask = 0,
                .sample_rate = (uint32_t)output_sample_rate_,
                .mclk_multiple = 0,
            };
            auto err = esp_codec_dev_open(output_dev_, &fs);
            if (err != ESP_OK) {
                output_volume_status_.applied = false;
                output_volume_status_.error = err;
                // The codec library marks its output open before all setup completes.
                // Close even on an open failure so a retry performs the full setup.
                // A failed ES8311 start may leave PA on while enabled is false, so a
                // successful no-op disable still does not establish physical PA-off.
                // Keep the original open error and applied=false until a new open succeeds.
                CloseOutputChecked();
                return;
            }
            AudioCodec::EnableOutput(true);
        }
        auto err = ApplyOutputVolumeChecked(output_volume_);
        output_volume_status_.applied = err == ESP_OK;
        output_volume_status_.error = err;
        if (err != ESP_OK) {
            CloseOutputChecked();
        }
    } else if (output_enabled_ || output_needs_close_) {
        output_volume_status_.error = CloseOutputChecked();
    }
#else
    if (enable == output_enabled_) {
        return;
    }
    if (enable) {
        // Play 16bit 1 channel
        esp_codec_dev_sample_info_t fs = {
            .bits_per_sample = 16,
            .channel = 1,
            .channel_mask = 0,
            .sample_rate = (uint32_t)output_sample_rate_,
            .mclk_multiple = 0,
        };
        ESP_ERROR_CHECK(esp_codec_dev_open(output_dev_, &fs));
        ESP_ERROR_CHECK(esp_codec_dev_set_out_vol(output_dev_, output_volume_));
    } else {
        ESP_ERROR_CHECK(esp_codec_dev_close(output_dev_));
    }
    AudioCodec::EnableOutput(enable);
#endif
}

int BoxAudioCodec::Read(int16_t* dest, int samples) {
    if (input_enabled_) {
        ESP_ERROR_CHECK_WITHOUT_ABORT(
            esp_codec_dev_read(input_dev_, (void*)dest, samples * sizeof(int16_t)));
    }
    return samples;
}

int BoxAudioCodec::Write(const int16_t* data, int samples) {
    if (output_enabled_) {
        ESP_ERROR_CHECK_WITHOUT_ABORT(
            esp_codec_dev_write(output_dev_, (void*)data, samples * sizeof(int16_t)));
    }
    return samples;
}

bool BoxAudioCodec::SupportsCheckedOutput() const {
#if CONFIG_WAVESHARE_1_85C_HOME
    return true;
#else
    return false;
#endif
}

bool BoxAudioCodec::OutputDataChecked(std::vector<int16_t>& data) {
#if CONFIG_WAVESHARE_1_85C_HOME
    // Serialize with EnableInput/EnableOutput and volume writes: the shared I2S
    // data interface can otherwise return OK without writing while reconfiguring.
    // Its 1000 ms timeout applies to the write semaphore and each DMA queue wait,
    // not the entire call. Cancellation must wait for its result before finishing.
    std::lock_guard<std::mutex> lock(data_if_mutex_);
    // A rejected setting transaction can leave the previous volume explicitly
    // restored (applied=true) while retaining its NVS/I2C error for the caller.
    // Playback depends on known hardware application, not that transaction code.
    if (data.empty() || !output_enabled_ || output_volume_ == 0 || output_needs_close_ ||
        !output_volume_status_.applied) {
        return false;
    }
    return esp_codec_dev_write(output_dev_, data.data(), data.size() * sizeof(int16_t)) ==
           ESP_CODEC_DEV_OK;
#else
    return false;
#endif
}
