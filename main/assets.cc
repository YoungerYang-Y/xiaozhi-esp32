#include "assets.h"
#include "application.h"
#include "board.h"
#include "cjson_utils.h"
#include "display.h"
#include "lvgl_theme.h"
#if HAVE_LVGL
#include <spi_flash_mmap.h>
#endif

#include <esp_heap_caps.h>
#include <esp_log.h>
#include <esp_timer.h>
#include <cbin_font.h>
#include <noto_font_bundle.h>

#include <cstring>

#define TAG "Assets"
#define PARTITION_LABEL "assets"

namespace {
// ESP-SR 2.4.7's srmodel_load trusts every count/offset. Validate its binary
// directory before passing mapped bytes to that parser (which has no length).
bool ValidateSrmodels(const void* data, size_t size) {
    constexpr size_t kNameSize = 32;
    const auto* bytes = static_cast<const uint8_t*>(data);
    size_t cursor = 0;
    auto read_u32 = [&](uint32_t& value) {
        if (cursor > size || size - cursor < 4) {
            return false;
        }
        value = static_cast<uint32_t>(bytes[cursor]) |
                (static_cast<uint32_t>(bytes[cursor + 1]) << 8) |
                (static_cast<uint32_t>(bytes[cursor + 2]) << 16) |
                (static_cast<uint32_t>(bytes[cursor + 3]) << 24);
        cursor += 4;
        return true;
    };
    uint32_t model_count = 0;
    if (!read_u32(model_count) || model_count == 0 || model_count > (size - cursor) / 36) {
        return false;
    }
    for (uint32_t i = 0; i < model_count; ++i) {
        if (size - cursor < kNameSize || bytes[cursor] == 0) {
            return false;
        }
        cursor += kNameSize;
        uint32_t file_count = 0;
        if (!read_u32(file_count) || file_count == 0 || file_count > (size - cursor) / 40) {
            return false;
        }
        for (uint32_t j = 0; j < file_count; ++j) {
            if (size - cursor < kNameSize || bytes[cursor] == 0 ||
                std::memchr(bytes + cursor, '\0', kNameSize) == nullptr) {
                return false;
            }
            cursor += kNameSize;
            uint32_t offset = 0, length = 0;
            if (!read_u32(offset) || !read_u32(length) || offset > size || length > size - offset ||
                length > INT32_MAX) {
                return false;
            }
        }
    }
    return true;
}
}  // namespace

struct mmap_assets_table {
    char asset_name[32];   /*!< Name of the asset */
    uint32_t asset_size;   /*!< Size of the asset */
    uint32_t asset_offset; /*!< Offset of the asset */
    uint16_t asset_width;  /*!< Width of the asset */
    uint16_t asset_height; /*!< Height of the asset */
};

Assets::Assets() {
    UseBuiltInTextFontCapability();
#if HAVE_LVGL
    strategy_ = std::make_unique<Assets::LvglStrategy>();
#else
    strategy_ = std::make_unique<Assets::EmoteStrategy>();
#endif
    // Initialize the partition
    InitializePartition();
}

Assets::~Assets() { UnApplyPartition(); }

bool Assets::FindPartition(Assets* assets) {
    assets->partition_ = esp_partition_find_first(ESP_PARTITION_TYPE_ANY, ESP_PARTITION_SUBTYPE_ANY,
                                                  PARTITION_LABEL);
    if (assets->partition_ == nullptr) {
        ESP_LOGI(TAG, "No assets partition found");
        return false;
    }
    return true;
}

bool Assets::Apply(bool refresh_display_theme) {
    if (!partition_valid_) {
        return false;
    }
    return strategy_ ? strategy_->Apply(this, refresh_display_theme) : false;
}

bool Assets::InitializePartition() {
    return strategy_ ? strategy_->InitializePartition(this) : false;
}

void Assets::UnApplyPartition() {
    UseBuiltInTextFontCapability();
    // Caller must first fence audio and display users. Model metadata points
    // into this mapping, so release it before the mapping itself is removed.
    if (models_list_ != nullptr) {
        Application::GetInstance().GetAudioService().SetModelsList(nullptr);
        esp_srmodel_deinit(models_list_);
        models_list_ = nullptr;
    }
    partition_valid_ = false;
    if (strategy_) {
        strategy_->UnApplyPartition(this);
    }
}

void Assets::UseBuiltInTextFontCapability() {
#if HAVE_LVGL
    text_font_capability_ = {
        .glyph_push = true,
        .bundle = NOTO_FONT_BUNDLE_ID,
        .charset = "basic",
        .size = TEXT_FONT_SIZE,
        .bpp = TEXT_FONT_BPP,
    };
#else
    // Emote does not consume pushed glyphs; advertising charset=basic makes
    // the server embed a bitmap for every CJK character and inflates MQTT.
    text_font_capability_ = {};
#endif
}

void Assets::DisableTextFontGlyphPush() { text_font_capability_ = {}; }

bool Assets::GetAssetData(const std::string& name, void*& ptr, size_t& size) {
    return strategy_ ? strategy_->GetAssetData(this, name, ptr, size) : false;
}

bool Assets::LoadSrmodelsFromIndex(Assets* assets, cJSON* root) {
    // Applying the same partition again must not free a live engine's models.
    if (assets->models_list_ != nullptr) {
        return true;
    }
    void* ptr = nullptr;
    size_t size = 0;
    bool need_delete_root = false;

    // If root is not provided, parse index.json
    if (root == nullptr) {
        if (!assets->GetAssetData("index.json", ptr, size)) {
            ESP_LOGE(TAG, "The index.json file is not found");
            return false;
        }

        root = cJSON_ParseWithLength(static_cast<char*>(ptr), size);
        if (root == nullptr) {
            ESP_LOGE(TAG, "The index.json file is not valid");
            return false;
        }
        need_delete_root = true;
    }

    cJSON* srmodels = cJSON_GetObjectItem(root, "srmodels");
    if (cJSON_IsString(srmodels)) {
        std::string srmodels_file = srmodels->valuestring;
        if (assets->GetAssetData(srmodels_file, ptr, size) && ValidateSrmodels(ptr, size)) {
            assets->models_list_ = srmodel_load(static_cast<uint8_t*>(ptr));
            if (assets->models_list_ != nullptr) {
                auto& app = Application::GetInstance();
                app.GetAudioService().SetModelsList(assets->models_list_);
                if (need_delete_root) {
                    cJSON_Delete(root);
                }
                return true;
            } else {
                ESP_LOGE(TAG, "Failed to load srmodels.bin");
            }
        } else {
            ESP_LOGE(TAG, "The srmodels file %s is not found", srmodels_file.c_str());
        }
    }

    if (need_delete_root) {
        cJSON_Delete(root);
    }
    return false;
}

#if HAVE_LVGL
uint32_t Assets::LvglStrategy::CalculateChecksum(const char* data, uint32_t length) {
    uint32_t checksum = 0;
    for (uint32_t i = 0; i < length; i++) {
        checksum += static_cast<uint8_t>(data[i]);
    }
    return checksum & 0xFFFF;
}

bool Assets::LvglStrategy::InitializePartition(Assets* assets) {
    assets->partition_valid_ = false;
    assets_.clear();

    if (!Assets::FindPartition(assets)) {
        return false;
    }

    // Read the header first so we only mmap the payload in use. On ESP32-C3 the
    // free MMU data pages can be smaller than a full 1MB assets partition even
    // when the packed assets.bin itself fits.
    uint8_t header[12] = {};
    esp_err_t err = esp_partition_read(assets->partition_, 0, header, sizeof(header));
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to read assets header: %s", esp_err_to_name(err));
        return false;
    }

    uint32_t stored_files = *(uint32_t*)(header + 0);
    uint32_t stored_chksum = *(uint32_t*)(header + 4);
    uint32_t stored_len = *(uint32_t*)(header + 8);

    if (assets->partition_->size < 12 || stored_len == 0 ||
        stored_len > assets->partition_->size - 12 ||
        stored_files > stored_len / sizeof(mmap_assets_table)) {
        ESP_LOGD(TAG, "The stored_len (0x%lx) is greater than the partition size (0x%lx) - 12",
                 stored_len, assets->partition_->size);
        return false;
    }

    constexpr uint32_t kMmuPageSize = 64 * 1024;
    uint32_t map_size = 12 + stored_len;
    map_size = (map_size + kMmuPageSize - 1) & ~(kMmuPageSize - 1);
    if (map_size > assets->partition_->size) {
        map_size = assets->partition_->size;
    }

    int free_pages = spi_flash_mmap_get_free_pages(SPI_FLASH_MMAP_DATA);
    uint32_t storage_size = free_pages * kMmuPageSize;
    ESP_LOGI(TAG, "The storage free size is %ld KB", storage_size / 1024);
    ESP_LOGI(TAG, "The assets map size is %ld KB (partition %ld KB)", map_size / 1024,
             assets->partition_->size / 1024);
    if (storage_size < map_size) {
        ESP_LOGE(TAG, "The free size %ld KB is less than assets map required %ld KB",
                 storage_size / 1024, map_size / 1024);
        return false;
    }

    err = esp_partition_mmap(assets->partition_, 0, map_size, ESP_PARTITION_MMAP_DATA,
                             (const void**)&mmap_root_, &mmap_handle_);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to mmap assets partition: %s", esp_err_to_name(err));
        return false;
    }

    assets->partition_valid_ = true;

    auto start_time = esp_timer_get_time();
    uint32_t calculated_checksum = CalculateChecksum(mmap_root_ + 12, stored_len);
    auto end_time = esp_timer_get_time();
    ESP_LOGI(TAG, "The checksum calculation time is %d ms", int((end_time - start_time) / 1000));

    if (calculated_checksum != stored_chksum) {
        ESP_LOGE(TAG, "The calculated checksum (0x%lx) does not match the stored checksum (0x%lx)",
                 calculated_checksum, stored_chksum);
        UnApplyPartition(assets);
        assets->partition_valid_ = false;
        return false;
    }

    checksum_valid_ = true;

    const size_t data_offset = 12 + sizeof(mmap_assets_table) * stored_files;
    const size_t payload_size = stored_len - sizeof(mmap_assets_table) * stored_files;
    for (uint32_t i = 0; i < stored_files; i++) {
        auto item = (const mmap_assets_table*)(mmap_root_ + 12 + i * sizeof(mmap_assets_table));
        // Checksums alone do not validate offsets, names, or the per-file magic.
        const std::string name(item->asset_name,
                               strnlen(item->asset_name, sizeof(item->asset_name)));
        if (name.empty() || assets_.count(name) != 0 || item->asset_offset > payload_size ||
            payload_size - item->asset_offset < 2 ||
            item->asset_size > payload_size - item->asset_offset - 2) {
            UnApplyPartition(assets);
            assets->partition_valid_ = false;
            return false;
        }
        const size_t offset = data_offset + item->asset_offset;
        if (mmap_root_[offset] != 'Z' || mmap_root_[offset + 1] != 'Z') {
            UnApplyPartition(assets);
            assets->partition_valid_ = false;
            return false;
        }
        assets_[name] = Asset{.size = static_cast<size_t>(item->asset_size), .offset = offset};
    }
    return checksum_valid_;
}

void Assets::LvglStrategy::UnApplyPartition(Assets* assets) {
    if (mmap_handle_ != 0) {
        esp_partition_munmap(mmap_handle_);
        mmap_handle_ = 0;
        mmap_root_ = nullptr;
    }
    checksum_valid_ = false;
    assets_.clear();
    (void)assets;  // Unused parameter
}

bool Assets::LvglStrategy::GetAssetData(Assets* assets, const std::string& name, void*& ptr,
                                        size_t& size) {
    if (!checksum_valid_ || mmap_root_ == nullptr) {
        return false;
    }
    auto asset = assets_.find(name);
    if (asset == assets_.end()) {
        return false;
    }
    auto data = (const char*)(mmap_root_ + asset->second.offset);
    if (data[0] != 'Z' || data[1] != 'Z') {
        ESP_LOGE(TAG, "The asset %s is not valid with magic %02x%02x", name.c_str(), data[0],
                 data[1]);
        return false;
    }

    ptr = static_cast<void*>(const_cast<char*>(data + 2));
    size = asset->second.size;
    return true;
}

bool Assets::LvglStrategy::Apply(Assets* assets, bool refresh_display_theme) {
    void* ptr = nullptr;
    size_t size = 0;
    if (!assets->GetAssetData("index.json", ptr, size)) {
        ESP_LOGE(TAG, "The index.json file is not found");
        return false;
    }

    CJsonUniquePtr root(cJSON_ParseWithLength(static_cast<char*>(ptr), size));
    if (root == nullptr) {
        ESP_LOGE(TAG, "The index.json file is not valid");
        return false;
    }

    cJSON* version = cJSON_GetObjectItem(root.get(), "version");
    if (cJSON_IsNumber(version)) {
        if (version->valuedouble > 1) {
            ESP_LOGE(TAG, "The assets version %d is not supported, please upgrade the firmware",
                     version->valueint);
            return false;
        }
    }

    if (cJSON_IsString(cJSON_GetObjectItem(root.get(), "srmodels")) &&
        !Assets::LoadSrmodelsFromIndex(assets, root.get())) {
        return false;
    }

    // A missing or failed panel can leave a NoDisplay without initializing LVGL.
    // Keep speech models available, but do not allocate unused or unsafe UI resources.
    auto display = Board::GetInstance().GetDisplay();
    if (display == nullptr || !display->SupportsGuiOperations() || !lv_is_initialized()) {
        assets->DisableTextFontGlyphPush();
        ESP_LOGI(TAG, "Skipping UI assets: LVGL display is unavailable");
        return true;
    }

    auto& theme_manager = LvglThemeManager::GetInstance();
    auto light_theme = theme_manager.GetTheme("light");
    auto dark_theme = theme_manager.GetTheme("dark");

    cJSON* font = cJSON_GetObjectItem(root.get(), "text_font");
    if (cJSON_IsString(font)) {
        std::string fonts_text_file = font->valuestring;
        if (assets->GetAssetData(fonts_text_file, ptr, size)) {
            auto text_font = std::make_shared<LvglCBinFont>(ptr);
            if (text_font->font() == nullptr || !display->SetTextFont(text_font)) {
                ESP_LOGW(TAG, "Ignoring invalid text font asset %s", fonts_text_file.c_str());
            } else {
                assets->DisableTextFontGlyphPush();

                cJSON* metadata = cJSON_GetObjectItem(root.get(), "text_font_meta");
                cJSON* charset = cJSON_GetObjectItem(metadata, "charset");
                cJSON* font_size = cJSON_GetObjectItem(metadata, "size");
                cJSON* font_bpp = cJSON_GetObjectItem(metadata, "bpp");
                cJSON* bundle = cJSON_GetObjectItem(metadata, "bundle");
                bool supports_glyph_push =
                    cJSON_IsString(charset) &&
                    (std::strcmp(charset->valuestring, "basic") == 0 ||
                     std::strcmp(charset->valuestring, "common") == 0) &&
                    cJSON_IsNumber(font_size) && font_size->valueint > 0 &&
                    font_size->valuedouble == font_size->valueint && cJSON_IsNumber(font_bpp) &&
                    font_bpp->valuedouble == font_bpp->valueint &&
                    (font_bpp->valueint == 1 || font_bpp->valueint == 4) &&
                    font_bpp->valueint == text_font->bpp() && cJSON_IsString(bundle) &&
                    bundle->valuestring[0] != '\0' && std::strlen(bundle->valuestring) <= 64;
                if (supports_glyph_push) {
                    assets->text_font_capability_ = {
                        .glyph_push = true,
                        .bundle = bundle->valuestring,
                        .charset = charset->valuestring,
                        .size = font_size->valueint,
                        .bpp = font_bpp->valueint,
                    };
                } else {
                    ESP_LOGW(TAG, "Loaded custom text font without compatible glyph push metadata");
                }
            }
        } else {
            ESP_LOGE(TAG, "The font file %s is not found", fonts_text_file.c_str());
        }
    }

    cJSON* emoji_collection = cJSON_GetObjectItem(root.get(), "emoji_collection");
    if (cJSON_IsArray(emoji_collection)) {
        auto custom_emoji_collection = std::make_shared<EmojiCollection>();
        int emoji_count = cJSON_GetArraySize(emoji_collection);
        for (int i = 0; i < emoji_count; i++) {
            cJSON* emoji = cJSON_GetArrayItem(emoji_collection, i);
            if (cJSON_IsObject(emoji)) {
                cJSON* name = cJSON_GetObjectItem(emoji, "name");
                cJSON* file = cJSON_GetObjectItem(emoji, "file");
                cJSON* eaf = cJSON_GetObjectItem(emoji, "eaf");
                if (cJSON_IsString(name) && cJSON_IsString(file) && (NULL == eaf)) {
                    if (!assets->GetAssetData(file->valuestring, ptr, size)) {
                        ESP_LOGE(TAG, "Emoji %s image file %s is not found", name->valuestring,
                                 file->valuestring);
                        continue;
                    }
                    custom_emoji_collection->AddEmoji(name->valuestring,
                                                      new LvglRawImage(ptr, size));
                }
            }
        }
        if (light_theme != nullptr) {
            light_theme->set_emoji_collection(custom_emoji_collection);
        }
        if (dark_theme != nullptr) {
            dark_theme->set_emoji_collection(custom_emoji_collection);
        }
        display->SetEmojiCollection(custom_emoji_collection);
    }

    cJSON* skin = cJSON_GetObjectItem(root.get(), "skin");
    if (cJSON_IsObject(skin)) {
        cJSON* light_skin = cJSON_GetObjectItem(skin, "light");
        if (cJSON_IsObject(light_skin) && light_theme != nullptr) {
            cJSON* text_color = cJSON_GetObjectItem(light_skin, "text_color");
            cJSON* background_color = cJSON_GetObjectItem(light_skin, "background_color");
            cJSON* background_image = cJSON_GetObjectItem(light_skin, "background_image");
            if (cJSON_IsString(text_color)) {
                light_theme->set_text_color(LvglTheme::ParseColor(text_color->valuestring));
            }
            if (cJSON_IsString(background_color)) {
                light_theme->set_background_color(
                    LvglTheme::ParseColor(background_color->valuestring));
                light_theme->set_chat_background_color(
                    LvglTheme::ParseColor(background_color->valuestring));
            }
            if (cJSON_IsString(background_image)) {
                if (!assets->GetAssetData(background_image->valuestring, ptr, size)) {
                    ESP_LOGE(TAG, "The background image file %s is not found",
                             background_image->valuestring);
                    return false;
                }
                auto background_image = std::make_shared<LvglCBinImage>(ptr);
                light_theme->set_background_image(background_image);
            }
        }
        cJSON* dark_skin = cJSON_GetObjectItem(skin, "dark");
        if (cJSON_IsObject(dark_skin) && dark_theme != nullptr) {
            cJSON* text_color = cJSON_GetObjectItem(dark_skin, "text_color");
            cJSON* background_color = cJSON_GetObjectItem(dark_skin, "background_color");
            cJSON* background_image = cJSON_GetObjectItem(dark_skin, "background_image");
            if (cJSON_IsString(text_color)) {
                dark_theme->set_text_color(LvglTheme::ParseColor(text_color->valuestring));
            }
            if (cJSON_IsString(background_color)) {
                dark_theme->set_background_color(
                    LvglTheme::ParseColor(background_color->valuestring));
                dark_theme->set_chat_background_color(
                    LvglTheme::ParseColor(background_color->valuestring));
            }
            if (cJSON_IsString(background_image)) {
                if (!assets->GetAssetData(background_image->valuestring, ptr, size)) {
                    ESP_LOGE(TAG, "The background image file %s is not found",
                             background_image->valuestring);
                    return false;
                }
                auto background_image = std::make_shared<LvglCBinImage>(ptr);
                dark_theme->set_background_image(background_image);
            }
        }
    }

    if (refresh_display_theme) {
        ESP_LOGI(TAG, "Refreshing display theme...");

        auto current_theme = display->GetTheme();
        if (current_theme != nullptr) {
            display->SetTheme(current_theme);
        }

        // Parse hide_subtitle configuration
        cJSON* hide_subtitle = cJSON_GetObjectItem(root.get(), "hide_subtitle");
        if (cJSON_IsBool(hide_subtitle)) {
            bool hide = cJSON_IsTrue(hide_subtitle);
            display->SetHideSubtitle(hide);
            ESP_LOGI(TAG, "Set hide_subtitle to %s", hide ? "true" : "false");
        }
    }

    return true;
}
#endif  // HAVE_LVGL

bool Assets::EmoteStrategy::InitializePartition(Assets* assets) {
    assets->DisableTextFontGlyphPush();
    assets->partition_valid_ = false;

    if (!Assets::FindPartition(assets)) {
        return false;
    }

    auto display = Board::GetInstance().GetDisplay();
    if (display == nullptr || !display->MountAssets(PARTITION_LABEL)) {
        ESP_LOGE(TAG, "Emote display is not initialized");
        return false;
    }
    assets->partition_valid_ = true;
    return assets->partition_valid_;
}

void Assets::EmoteStrategy::UnApplyPartition(Assets* assets) {
    auto display = Board::GetInstance().GetDisplay();
    if (display != nullptr) {
        display->UnmountAssets();
    }
    (void)assets;  // Unused parameter
}

bool Assets::EmoteStrategy::GetAssetData(Assets* assets, const std::string& name, void*& ptr,
                                         size_t& size) {
    auto display = Board::GetInstance().GetDisplay();
    if (display != nullptr) {
        const uint8_t* data = nullptr;
        size_t data_size = 0;
        if (display->GetAssetData(name, data, data_size)) {
            ptr = const_cast<void*>(static_cast<const void*>(data));
            size = data_size;
            return true;
        }
        ESP_LOGE(TAG, "Failed to get asset data by name: %s", name.c_str());
        return false;
    }
    (void)assets;  // Unused parameter
    return false;
}

bool Assets::EmoteStrategy::Apply(Assets* assets, bool refresh_display_theme) {
    assets->DisableTextFontGlyphPush();
    Assets::LoadSrmodelsFromIndex(assets);

    auto display = Board::GetInstance().GetDisplay();
    if (display != nullptr) {
        display->LoadAssets();
    }
    return true;
}

bool Assets::Download(std::string url,
                      std::function<void(int progress, size_t speed)> progress_callback) {
    ESP_LOGI(TAG, "Downloading new version of assets from %s", url.c_str());

    if (partition_ == nullptr) {
        return false;
    }
    auto network = Board::GetInstance().GetNetwork();
    if (network == nullptr) {
        return false;
    }
    auto http = network->CreateHttp(0);
    if (!http) {
        return false;
    }

    if (auto opened = http->Open("GET", url); !opened) {
        ESP_LOGE(TAG, "Failed to open HTTP connection: %s", opened.error().ToString().c_str());
        return false;
    }

    auto status_code = http->GetStatusCode();
    if (!status_code) {
        ESP_LOGE(TAG, "Failed to read HTTP status: %s", status_code.error().ToString().c_str());
        return false;
    }
    if (*status_code != 200) {
        ESP_LOGE(TAG, "Failed to get assets, status code: %d", *status_code);
        return false;
    }

    size_t content_length = http->GetBodyLength();

    if (content_length == 0) {
        ESP_LOGE(TAG, "Failed to get content length");
        return false;
    }

    if (content_length > partition_->size) {
        ESP_LOGE(TAG, "Assets file size (%u) is larger than partition size (%lu)", content_length,
                 partition_->size);
        return false;
    }

    constexpr size_t HEADER_SIZE = 12;

    if (content_length < HEADER_SIZE) {
        ESP_LOGE(TAG, "Content length (%u) is smaller than header size (%u)", content_length,
                 HEADER_SIZE);
        return false;
    }

    const size_t SECTOR_SIZE = esp_partition_get_main_flash_sector_size();
    using BufferPtr = std::unique_ptr<char, decltype(&heap_caps_free)>;

    BufferPtr buffer(static_cast<char*>(heap_caps_malloc(SECTOR_SIZE, MALLOC_CAP_INTERNAL)),
                     &heap_caps_free);

    if (!buffer) {
        ESP_LOGE(TAG, "Failed to allocate buffer");
        return false;
    }

    // Unapply the partition
    UnApplyPartition();

    size_t sectors_to_erase = (content_length + SECTOR_SIZE - 1) / SECTOR_SIZE;
    size_t total_erase_size = sectors_to_erase * SECTOR_SIZE;
    ESP_LOGI(TAG,
             "Sector size: %u, content length: %u, "
             "sectors to erase: %u, total erase size: %u",
             SECTOR_SIZE, content_length, sectors_to_erase, total_erase_size);

    size_t total_written = 0;
    size_t recent_written = 0;
    size_t current_sector = 0;

    int64_t last_calc_time = esp_timer_get_time();

    uint8_t header_buf[HEADER_SIZE];
    size_t header_collected = 0;
    bool success = false;
    while (true) {
        auto ret = http->Read(buffer.get(), SECTOR_SIZE);
        if (!ret) {
            ESP_LOGE(TAG, "Failed to read HTTP data: %s", ret.error().ToString().c_str());
            break;
        }
        int n = *ret;

        if (n == 0) {
            // End of data
            success = true;
            break;
        }

        if (n < 0 || static_cast<size_t>(n) > content_length - header_collected - total_written) {
            ESP_LOGE(TAG, "Assets response exceeds the declared length");
            break;
        }
        size_t buf_pos = 0;

        // Collect header
        if (header_collected < HEADER_SIZE) {
            size_t need = HEADER_SIZE - header_collected;
            size_t take = std::min(static_cast<size_t>(n), need);
            memcpy(header_buf + header_collected, buffer.get(), take);
            header_collected += take;
            buf_pos += take;
        }

        // Write payload
        if ((size_t)n > buf_pos) {
            size_t write_len = (size_t)n - buf_pos;
            size_t write_end_offset = HEADER_SIZE + total_written + write_len;
            size_t needed_sectors = (write_end_offset + SECTOR_SIZE - 1) / SECTOR_SIZE;
            // Erase sectors
            bool erase_failed = false;
            while (current_sector < needed_sectors) {
                size_t sector_start = current_sector * SECTOR_SIZE;
                size_t sector_end = sector_start + SECTOR_SIZE;
                if (sector_end > partition_->size) {
                    ESP_LOGE(TAG, "Sector end (%u) exceeds partition size (%lu)", sector_end,
                             partition_->size);
                    erase_failed = true;
                    break;
                }
                ESP_LOGD(TAG, "Erasing sector %u (offset: %u, size: %u)", current_sector,
                         sector_start, SECTOR_SIZE);
                esp_err_t err = esp_partition_erase_range(partition_, sector_start, SECTOR_SIZE);
                if (err != ESP_OK) {
                    ESP_LOGE(TAG, "Failed to erase sector %u at offset %u: %s", current_sector,
                             sector_start, esp_err_to_name(err));
                    erase_failed = true;
                    break;
                }
                current_sector++;
            }

            if (erase_failed) {
                break;
            }

            esp_err_t err = esp_partition_write(partition_, HEADER_SIZE + total_written,
                                                buffer.get() + buf_pos, write_len);
            if (err != ESP_OK) {
                ESP_LOGE(TAG, "Failed to write to assets partition at offset %u: %s",
                         (unsigned int)(HEADER_SIZE + total_written), esp_err_to_name(err));
                break;
            }

            total_written += write_len;
            recent_written += write_len;
        }

        // Calculate progress
        if (esp_timer_get_time() - last_calc_time >= 1000000 ||
            (header_collected + total_written) == content_length) {
            size_t progress = (header_collected + total_written) * 100 / content_length;
            size_t speed = recent_written;
            ESP_LOGI(TAG, "Progress: %u%% (%u/%u), Speed: %u B/s, Sectors erased: %u", progress,
                     (unsigned int)(header_collected + total_written), (unsigned int)content_length,
                     (unsigned int)speed, (unsigned int)current_sector);

            if (progress_callback) {
                progress_callback(progress, speed);
            }
            last_calc_time = esp_timer_get_time();
            recent_written = 0;
        }
    }

    // Check if the downloaded size matches the expected size
    if (success && (header_collected + total_written != content_length)) {
        ESP_LOGE(TAG, "Downloaded size (%u) does not match expected size (%u)",
                 (unsigned int)(header_collected + total_written), (unsigned int)content_length);
        success = false;
    }

    // Write header
    if (success) {
        esp_err_t err = esp_partition_write(partition_, 0, header_buf, HEADER_SIZE);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Failed to write assets header to partition: %s", esp_err_to_name(err));
            success = false;
        }
    }

    if (!success) {
        ESP_LOGE(TAG, "Assets download failed");
        // A failed transfer may have left a valid untouched package, or an
        // invalid partial one. Only checksum/bounds validation may remap it.
        InitializePartition();
        return false;
    }

    ESP_LOGI(TAG,
             "Header written, assets download completed, total written: %u bytes, total sectors "
             "erased: %u",
             (unsigned int)(header_collected + total_written), (unsigned int)current_sector);

    // Re-initialize the assets partition
    if (!InitializePartition()) {
        ESP_LOGE(TAG, "Failed to re-initialize assets partition");
        return false;
    }

    return true;
}
