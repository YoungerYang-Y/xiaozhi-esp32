"""Run production status-bar methods against LVGL and board boundary adapters."""

from pathlib import Path
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib

from test_audio_capture_pause import method, without_includes
from test_checked_settings import ROOT, compiler_command


PLATFORM = r'''
#include <algorithm>
#include <cassert>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <functional>
#include <memory>
#include <string>
#include <vector>
#include "main/boards/common/battery_status.h"
#include "managed_components/78__xiaozhi-fonts/include/material_symbols.h"
#define ESP_LOGW(...) ((void)0)
#define ESP_ERROR_CHECK(x) assert((x) == 0)
constexpr int LV_OBJ_FLAG_HIDDEN = 1, LV_PALETTE_RED = 1;
struct lv_font_t { int line_height = 20; };
using lv_color_t = int;
struct lv_obj_t {
    std::string text;
    int flags = 0, color = 0;
    const lv_font_t* font = nullptr;
};
void lv_label_set_text(lv_obj_t* obj, const char* text) { obj->text = text ? text : ""; }
void lv_label_set_text_fmt(lv_obj_t* obj, const char* fmt, int value) {
    char text[24]; snprintf(text, sizeof(text), fmt, value); obj->text = text;
}
void lv_obj_set_style_text_font(lv_obj_t* obj, const lv_font_t* font, int) { obj->font = font; }
void lv_obj_set_style_text_color(lv_obj_t* obj, int color, int) { obj->color = color; }
int lv_palette_main(int) { return 99; }
bool lv_obj_has_flag(lv_obj_t* obj, int flag) { return obj->flags & flag; }
void lv_obj_add_flag(lv_obj_t* obj, int flag) { obj->flags |= flag; }
void lv_obj_remove_flag(lv_obj_t* obj, int flag) { obj->flags &= ~flag; }
int esp_pm_lock_acquire(void*) { return 0; }
int esp_pm_lock_release(void*) { return 0; }
struct Font { lv_font_t value; const lv_font_t* font() { return &value; } };
struct LvglTheme {
    std::shared_ptr<Font> text = std::make_shared<Font>();
    std::shared_ptr<Font> icon = std::make_shared<Font>();
    std::shared_ptr<Font> large = std::make_shared<Font>();
    int color = 7;
    auto text_font() { return text; }
    auto icon_font() { return icon; }
    auto large_icon_font() { return large; }
    int text_color() { return color; }
};
enum DeviceState { kDeviceStateIdle, kDeviceStateStarting, kDeviceStateWifiConfiguring,
    kDeviceStateListening, kDeviceStateActivating, kDeviceStateSpeaking,
    kDeviceStateConnecting, kDeviceStateUpgrading };
namespace Lang::Sounds { constexpr int OGG_LOW_BATTERY = 1; }
struct Application {
    DeviceState state = kDeviceStateSpeaking;
    bool paused = false, connection_alert = false;
    int sounds = 0;
    static Application& GetInstance() { static Application app; return app; }
    auto GetDeviceState() { return state; }
    bool IsListeningPaused() { return paused; }
    bool HasConnectionAlert() const { return connection_alert; }
    void Schedule(std::function<void()> fn) { fn(); }
    void PlaySound(int) { ++sounds; }
};
struct OutputVolumeStatus { int volume; bool persisted, applied; int error; };
struct Codec {
    int volume = 0, snapshot_reads = 0, legacy_reads = 0;
    OutputVolumeStatus GetOutputVolumeStatus() { ++snapshot_reads; return {volume, true, false, 0}; }
    int output_volume() { ++legacy_reads; return 71; }
};
struct Board {
    BatteryStatus battery;
    Codec codec;
    bool has_codec = true, has_legacy = true, charging = false, discharging = true;
    int percentage = 0, legacy_reads = 0, battery_reads = 0, network_reads = 0;
    const char* network = "wifi";
    static Board& GetInstance() { static Board board; return board; }
    Codec* GetAudioCodec() { return has_codec ? &codec : nullptr; }
    BatteryStatus GetBatteryStatus() { ++battery_reads; return battery; }
    bool GetBatteryLevel(int& level, bool& charge, bool& discharge) {
        ++legacy_reads; level = percentage; charge = charging; discharge = discharging;
        return has_legacy;
    }
    const char* GetNetworkStateIcon() { ++network_reads; return network; }
};
struct LvglDisplay {
    lv_obj_t network, mute, battery, volume, popup, clock;
    lv_obj_t* network_label_ = &network;
    lv_obj_t* mute_label_ = &mute;
    lv_obj_t* battery_label_ = &battery;
    lv_obj_t* volume_label_ = &volume;
    lv_obj_t* low_battery_popup_ = &popup;
    lv_obj_t* home_clock_label_ = &clock;
    const char* battery_icon_ = nullptr;
    const char* network_icon_ = nullptr;
    bool muted_ = false;
    bool battery_status_supported_ = false, battery_status_unknown_ = true, battery_status_low_ = false;
    void* pm_lock_ = nullptr;
    LvglTheme theme;
    LvglTheme* current_theme_ = &theme;
    int last_displayed_clock_min_ = -1;
    std::string status;
    LvglDisplay() { popup.flags = LV_OBJ_FLAG_HIDDEN; }
    void SetStatus(const char* text) { status = text; }
    void UpdateStatusBar(bool update_all = false);
    void ApplyBatteryStatusStyle(LvglTheme* theme);
};
struct DisplayLockGuard { explicit DisplayLockGuard(LvglDisplay*) {} };
time_t time(time_t* output) noexcept {
    const time_t fixed = 1893456000; // system clock boundary: 2030-01-01 UTC
    if (output) *output = fixed;
    return fixed;
}
'''


CHECKS = r'''
int main(int argc, char** argv) {
    assert(argc == 2);
    const std::string scenario = argv[1];
    auto& board = Board::GetInstance();
    auto& app = Application::GetInstance();
    LvglDisplay display;
    if (scenario == "legacy") {
        display.UpdateStatusBar(false);
        assert(board.legacy_reads == 1);
        assert(display.battery.text == MATERIAL_SYMBOLS_BATTERY_ANDROID_0);
        assert(app.sounds == 1 && !lv_obj_has_flag(&display.popup, LV_OBJ_FLAG_HIDDEN));
        return 0;
    }
    board.battery.supported = true;
    display.UpdateStatusBar(false);
    assert(board.legacy_reads == 0 && board.battery_reads == 1);
    assert(display.battery.text == "?");
    assert(display.battery.font == display.theme.text_font()->font());
    assert(display.battery.color == display.theme.text_color());
    assert(app.sounds == 0 && lv_obj_has_flag(&display.popup, LV_OBJ_FLAG_HIDDEN));
    if (scenario == "unknown") return 0;
    if (scenario == "bands") {
        std::vector<std::string> icons;
        for (auto band : {BatteryBand::OneBar, BatteryBand::TwoBars, BatteryBand::ThreeBars}) {
            board.battery = {true, BatteryState::Unclassified, band};
            display.UpdateStatusBar(false);
            assert(display.battery.text != "?" && !display.battery.text.empty());
            assert(display.battery.text != MATERIAL_SYMBOLS_BATTERY_ANDROID_FRAME_BOLT);
            assert(display.battery.color == display.theme.text_color());
            assert(display.battery.font == display.theme.icon_font()->font());
            icons.push_back(display.battery.text);
        }
        assert(icons[0] != icons[1] && icons[1] != icons[2] && icons[0] != icons[2]);
        board.battery = {true, BatteryState::Low, BatteryBand::OneBar};
        display.UpdateStatusBar(false);
        assert(display.battery.color == lv_palette_main(LV_PALETTE_RED));
        board.battery = {true, BatteryState::Unknown, BatteryBand::ThreeBars};
        display.UpdateStatusBar(false);
        assert(display.battery.text == "?" && display.battery.color == display.theme.text_color());
        board.battery = {true, BatteryState::Normal, BatteryBand::Unknown};
        display.UpdateStatusBar(false);
        assert(display.battery.text == "?");
        assert(app.sounds == 0 && board.legacy_reads == 0);
        return 0;
    }
    if (scenario == "connection-clock") {
        app.state = kDeviceStateIdle;
        app.connection_alert = true;
        display.status = "service unavailable";
        display.UpdateStatusBar(true);
        assert(display.status == "service unavailable");
        app.connection_alert = false;
        display.UpdateStatusBar(true);
#if CONFIG_WAVESHARE_1_85C_HOME
        assert(display.status == "service unavailable");
        assert(display.clock.text.size() == 5 && display.clock.text[2] == ':');
#else
        assert(display.status != "service unavailable");
#endif
        return 0;
    }
    if (scenario == "volume-network") {
#if CONFIG_WAVESHARE_1_85C_HOME
        assert(display.volume.text == "0");
        assert(board.codec.snapshot_reads == 1 && board.codec.legacy_reads == 0);
        for (auto state : {kDeviceStateSpeaking, kDeviceStateConnecting, kDeviceStateUpgrading}) {
            app.state = state;
            board.network = state == kDeviceStateConnecting ? "offline" : "wifi";
            board.codec.volume = 38;
            const int reads = board.network_reads;
            display.UpdateStatusBar(false);
            assert(board.network_reads == reads + 1 && display.network.text == board.network);
            assert(display.volume.text == "38");
        }
        board.has_codec = false;
        display.UpdateStatusBar(false);
        assert(display.volume.text == "?");
#else
        assert(board.codec.snapshot_reads == 0 && board.codec.legacy_reads == 1);
        assert(display.volume.text.empty() && board.network_reads == 0);
#endif
        return 0;
    }
    assert(false);
}
'''


class HomeDisplayStatusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        build = Path(cls.temp.name)
        path = "main/display/lvgl_display/lvgl_display.cc"
        production = method(path, "void LvglDisplay::UpdateStatusBar(")
        text = (ROOT / path).read_text(encoding="utf-8")
        if "void LvglDisplay::ApplyBatteryStatusStyle(" in text:
            production += method(path, "void LvglDisplay::ApplyBatteryStatusStyle(")
        source = build / "display.cc"
        source.write_text(PLATFORM + production + CHECKS, encoding="utf-8")
        cls.binaries = {}
        for home in (0, 1):
            binary = build / f"display-{home}"
            result = subprocess.run(compiler_command() + ["-std=c++17", f"-DCONFIG_WAVESHARE_1_85C_HOME={home}",
                                    "-I", str(ROOT), str(source), "-o", str(binary)],
                                    capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
            cls.binaries[home] = binary

    def run_scenario(self, scenario, home=1):
        result = subprocess.run([str(self.binaries[home]), scenario],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unknown_never_uses_legacy_battery_or_sound(self):
        self.run_scenario("unknown")

    def test_coarse_bands_low_and_recovery_share_one_snapshot_path(self):
        self.run_scenario("bands")

    def test_optional_capability_uses_legacy_percentage_and_popup(self):
        for home in (0, 1):
            with self.subTest(home=home):
                self.run_scenario("legacy", home)

    def test_home_volume_zero_and_network_all_states(self):
        self.run_scenario("volume-network")

    def test_other_boards_keep_legacy_volume_and_network_refresh_policy(self):
        self.run_scenario("volume-network", 0)

    def test_idle_clock_does_not_replace_a_connection_failure_status(self):
        self.run_scenario("connection-clock")


LVGL_PLATFORM = r'''
#include <lvgl.h>
#include <algorithm>
#include <cstdio>
#include <cassert>
#include <chrono>
#include <cstring>
#include <ctime>
#include <memory>
#include <functional>
#include <map>
#include <string>
#include <vector>
#include "managed_components/78__xiaozhi-fonts/include/material_symbols.h"
#include "main/display/lvgl_display/lvgl_font.h"
#include "main/display/lvgl_display/lvgl_image.h"
#include "main/boards/common/battery_status.h"
#include "png_samples.h"
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define ESP_ERROR_CHECK(x) assert((x) == 0)
#define PREVIEW_IMAGE_DURATION_MS 5000
LV_FONT_DECLARE(font_noto_sans_basic_16_4);
LV_FONT_DECLARE(font_noto_sans_basic_30_4);
LV_FONT_DECLARE(font_material_symbols_16_4);
LV_FONT_DECLARE(font_material_symbols_30_4);
int timer_count = 0;
struct HostTimer {
    bool active = false;
    std::function<void()> callback;
    HostTimer() { ++timer_count; }
    ~HostTimer() { --timer_count; }
};
using esp_timer_handle_t = HostTimer*;
enum DeviceState { kDeviceStateIdle, kDeviceStateStarting, kDeviceStateWifiConfiguring,
    kDeviceStateListening, kDeviceStateActivating, kDeviceStateSpeaking,
    kDeviceStateConnecting, kDeviceStateUpgrading };
namespace Lang::Sounds { constexpr int OGG_LOW_BATTERY = 1; }
struct Application {
    bool paused = false;
    bool connection_alert = false;
    DeviceState state = kDeviceStateIdle;
    static Application& GetInstance() { static Application app; return app; }
    bool IsListeningPaused() const { return paused; }
    bool HasConnectionAlert() const { return connection_alert; }
    DeviceState GetDeviceState() const { return state; }
    void Schedule(std::function<void()> fn) { fn(); }
    void PlaySound(int) {}
};
struct Codec {
    struct Volume { int volume = 30; };
    Volume GetOutputVolumeStatus() { return {}; }
};
struct Board {
    Codec codec;
    static Board& GetInstance() { static Board board; return board; }
    Codec* GetAudioCodec() { return &codec; }
    BatteryStatus GetBatteryStatus() { return {true, BatteryState::Unknown, BatteryBand::Unknown}; }
    bool GetBatteryLevel(int&, bool&, bool&) { return false; }
    const char* GetNetworkStateIcon() { return MATERIAL_SYMBOLS_WIFI; }
};
time_t host_now = 1893456000;
time_t time(time_t* output) noexcept {
    if (output) *output = host_now;
    return host_now;
}
int esp_timer_stop(HostTimer* timer) { timer->active = false; return 0; }
int esp_timer_start_once(HostTimer* timer, uint64_t) { timer->active = true; return 0; }
int esp_timer_delete(HostTimer* timer) { delete timer; return 0; }
int esp_pm_lock_delete(void*) { return 0; }
int esp_pm_lock_acquire(void*) { return 0; }
int esp_pm_lock_release(void*) { return 0; }
int esp_lcd_panel_del(void*) { return 0; }
int esp_lcd_panel_io_del(void*) { return 0; }
int live_images = 0, live_gifs = 0;
struct HostImage : LvglImage {
    lv_image_dsc_t descriptor{};
    std::vector<uint32_t> pixels;
    bool gif;
    HostImage(int width, int height, bool animated = false)
        : pixels(width * height, 0xff102030), gif(animated) {
        ++live_images;
        descriptor.header.magic = LV_IMAGE_HEADER_MAGIC;
        descriptor.header.cf = LV_COLOR_FORMAT_ARGB8888;
        descriptor.header.w = width;
        descriptor.header.h = height;
        descriptor.header.stride = width * 4;
        descriptor.data_size = pixels.size() * 4;
        descriptor.data = reinterpret_cast<const uint8_t*>(pixels.data());
    }
    ~HostImage() { --live_images; }
    const lv_image_dsc_t* image_dsc() const override { return &descriptor; }
    bool IsGif() const override { return gif; }
};
struct HostPngImage : LvglImage {
    lv_image_dsc_t descriptor{};
    HostPngImage(const uint8_t* png, size_t size) {
        ++live_images;
        descriptor.header.magic = LV_IMAGE_HEADER_MAGIC;
        descriptor.header.cf = LV_COLOR_FORMAT_RAW_ALPHA;
        descriptor.data = png;
        descriptor.data_size = size;
    }
    ~HostPngImage() { --live_images; }
    const lv_image_dsc_t* image_dsc() const override { return &descriptor; }
};
// Decoder/timer boundary only: the production SetEmotion callback and LVGL's
// actual image sizing/source-change code still handle every decoded frame.
struct LvglGif {
    HostImage frame;
    bool playing = false;
    std::function<void()> callback;
    explicit LvglGif(const lv_image_dsc_t* source) : frame(source->header.w, source->header.h) { ++live_gifs; }
    ~LvglGif() { --live_gifs; }
    bool IsLoaded() { return true; }
    bool IsPlaying() { return playing; }
    const lv_image_dsc_t* image_dsc() { return frame.image_dsc(); }
    void SetFrameCallback(std::function<void()> fn) { callback = fn; }
    void Start() { playing = true; if (callback) callback(); }
    void Stop() { playing = false; }
    void Pause() { playing = false; }
    void NextFrame(int width, int height) {
        frame.descriptor.header.w = width;
        frame.descriptor.header.h = height;
        frame.descriptor.header.stride = width * 4;
        if (callback) callback();
    }
};
const char* noto_emoji_get_utf8(const char*) { return "?"; }
struct EmojiCollection {
    std::map<std::string, std::shared_ptr<LvglImage>> images;
    std::shared_ptr<LvglImage> GetEmojiImage(const char* name) {
        auto found = images.find(name);
        return found == images.end() ? nullptr : found->second;
    }
};
namespace Lang::Strings {
    constexpr auto INITIALIZING = "初始化中...";
    constexpr auto STANDBY = "待机";
    constexpr auto LISTENING = "聆听中...";
    constexpr auto WAITING_RESPONSE = "等待回应";
    constexpr auto SPEAKING = "说话中...";
    constexpr auto LISTENING_PAUSED = "监听已暂停";
}
struct Theme {};
struct Image { const void* image_dsc() { return nullptr; } };
struct LvglTheme : Theme {
    std::shared_ptr<LvglFont> text = std::make_shared<LvglBuiltInFont>(&font_noto_sans_basic_16_4);
    std::shared_ptr<LvglFont> icon = std::make_shared<LvglBuiltInFont>(&font_material_symbols_16_4);
    std::shared_ptr<LvglFont> large = std::make_shared<LvglBuiltInFont>(&font_material_symbols_30_4);
    std::shared_ptr<EmojiCollection> emojis = std::make_shared<EmojiCollection>();
    lv_color_t color = lv_color_black();
    auto text_font() { return text; }
    void set_text_font(std::shared_ptr<LvglFont> value) { text = value; }
    auto icon_font() { return icon; }
    auto large_icon_font() { return large; }
    auto emoji_font() { return large; }
    auto emoji_collection() { return emojis; }
    auto text_color() { return color; }
    auto background_color() { return lv_color_white(); }
    auto low_battery_color() { return lv_palette_main(LV_PALETTE_RED); }
    std::shared_ptr<Image> background_image() { return nullptr; }
};
struct LvglThemeManager {
    LvglTheme* light = nullptr;
    LvglTheme* dark = nullptr;
    static LvglThemeManager& GetInstance() { static LvglThemeManager manager; return manager; }
    LvglTheme* GetTheme(const char* name) { return strcmp(name, "light") == 0 ? light : dark; }
};
struct Display {
    int width_ = 360, height_ = 360;
    bool setup_ui_called_ = false;
    LvglTheme theme;
    LvglTheme dark_theme;
    Theme* current_theme_ = &theme;
    Display() {
        LvglThemeManager::GetInstance().light = &theme;
        LvglThemeManager::GetInstance().dark = &dark_theme;
    }
    virtual ~Display() = default;
    void SetupUI() { setup_ui_called_ = true; }
    virtual void SetTheme(Theme* value) { current_theme_ = value; }
};
struct LvglDisplay : Display {
    lv_obj_t* battery_label_ = nullptr;
    lv_obj_t* network_label_ = nullptr;
    lv_obj_t* mute_label_ = nullptr;
    lv_obj_t* volume_label_ = nullptr;
    lv_obj_t* status_label_ = nullptr;
    lv_obj_t* notification_label_ = nullptr;
    lv_obj_t* home_clock_label_ = nullptr;
    lv_obj_t* low_battery_popup_ = nullptr;
    lv_display_t* display_ = nullptr;
    void* pm_lock_ = nullptr;
    HostTimer* notification_timer_ = new HostTimer;
    bool battery_status_supported_ = false, battery_status_unknown_ = true, battery_status_low_ = false;
    bool home_status_is_icon_ = false;
    const char* battery_icon_ = nullptr;
    const char* network_icon_ = nullptr;
    int last_displayed_clock_min_ = -1;
    std::chrono::system_clock::time_point last_status_update_time_;
    void ApplyBatteryStatusStyle(LvglTheme*);
    void SetStatus(const char*);
    void UpdateStatusBar(bool update_all = false);
    bool SetTextFont(std::shared_ptr<LvglFont>);
    void ShowNotification(const char*, int duration_ms = 3000);
    ~LvglDisplay();
};
struct LcdDisplay : LvglDisplay {
    lv_obj_t* container_ = nullptr;
    lv_obj_t* emoji_box_ = nullptr;
    lv_obj_t* home_media_box_ = nullptr;
    lv_obj_t* emoji_label_ = nullptr;
    lv_obj_t* emoji_image_ = nullptr;
    lv_obj_t* preview_image_ = nullptr;
    lv_obj_t* top_bar_ = nullptr;
    lv_obj_t* status_bar_ = nullptr;
    lv_obj_t* bottom_bar_ = nullptr;
    lv_obj_t* chat_message_label_ = nullptr;
    lv_obj_t* content_ = nullptr;
    lv_obj_t* side_bar_ = nullptr;
    void* panel_ = nullptr;
    void* panel_io_ = nullptr;
    std::unique_ptr<LvglGif> gif_controller_;
    std::unique_ptr<LvglImage> preview_image_cached_;
    HostTimer* preview_timer_ = new HostTimer;
    bool emotion_uses_image_ = false;
    bool hide_subtitle_ = false;
    LcdDisplay() { preview_timer_->callback = [this]() { SetPreviewImage(nullptr); }; }
    ~LcdDisplay();
    void SetupUI();
    void SetupHomeUI();
    void UpdateHomeLayout(LvglTheme*);
    void ShowEmotion();
    bool SetEmotion(const char*);
    void SetPreviewImage(std::unique_ptr<LvglImage>);
    void SetTheme(Theme*);
    void SetChatMessage(const char*, const char*);
    void RemoveChatMessage(const char*, const char*);
    void ClearChatMessages();
    void SetHideSubtitle(bool);
};
std::function<void()> before_display_lock;
struct DisplayLockGuard {
    explicit DisplayLockGuard(LvglDisplay*) {
        if (before_display_lock) {
            auto run = std::move(before_display_lock);
            before_display_lock = {};
            run();
        }
    }
};
'''


LAYOUT_CHECKS = r'''
void inside_circle(lv_obj_t* object) {
    lv_area_t box;
    lv_obj_get_coords(object, &box);
    for (int x : {box.x1, box.x2}) for (int y : {box.y1, box.y2}) {
        assert((x - 180) * (x - 180) + (y - 180) * (y - 180) < 180 * 180);
    }
}
void within_parent(lv_obj_t* object) {
    lv_area_t child, parent;
    lv_obj_get_coords(object, &child);
    lv_obj_get_coords(lv_obj_get_parent(object), &parent);
    assert(child.x1 >= parent.x1 && child.x2 <= parent.x2);
    assert(child.y1 >= parent.y1 && child.y2 <= parent.y2);
}
void check_default_layout() {
    LcdDisplay ui;
    ui.SetupUI();
    assert(ui.setup_ui_called_ && ui.volume_label_ && ui.emoji_image_);
    const auto children = lv_obj_get_child_count(lv_screen_active());
    ui.SetupUI();
    assert(lv_obj_get_child_count(lv_screen_active()) == children);
    assert(lv_obj_get_style_text_font(ui.volume_label_, LV_PART_MAIN) == ui.theme.text_font()->font());
    ui.SetStatus("监听已暂停");
    std::string long_text;
    for (int n = 0; n < 200; ++n) long_text += "小智正在回答，请听我说。";
    ui.SetChatMessage("assistant", long_text.c_str());
    lv_obj_update_layout(lv_screen_active());
    for (auto obj : {ui.top_bar_, ui.status_bar_, ui.bottom_bar_, ui.emoji_box_}) inside_circle(obj);
    assert(lv_obj_get_height(ui.bottom_bar_) == 40);
    assert(lv_obj_get_y(ui.status_bar_) >= lv_obj_get_y(ui.top_bar_) + lv_obj_get_height(ui.top_bar_));
    assert(lv_obj_get_y(ui.bottom_bar_) >= lv_obj_get_y(ui.status_bar_) + lv_obj_get_height(ui.status_bar_));
    assert(strcmp(lv_label_get_text(ui.status_label_), MATERIAL_SYMBOLS_MIC_OFF) == 0);
    assert(!lv_obj_has_flag(ui.status_label_, LV_OBJ_FLAG_HIDDEN));
#if CONFIG_USE_MULTILINE_CHAT_MESSAGE
    assert(lv_obj_get_height(ui.chat_message_label_) > lv_obj_get_height(ui.bottom_bar_));
    assert(lv_obj_get_scroll_y(ui.bottom_bar_) > 0);
#else
    assert(lv_obj_get_height(ui.chat_message_label_) <= lv_obj_get_height(ui.bottom_bar_));
#endif
    ui.SetHideSubtitle(true);
    ui.SetChatMessage("assistant", "still hidden");
    assert(lv_obj_has_flag(ui.bottom_bar_, LV_OBJ_FLAG_HIDDEN));
    ui.SetHideSubtitle(false);
    assert(!lv_obj_has_flag(ui.bottom_bar_, LV_OBJ_FLAG_HIDDEN));

    LvglTheme dark;
    dark.color = lv_color_white();
    ui.SetTheme(&dark);
    assert(lv_obj_get_style_text_font(ui.volume_label_, LV_PART_MAIN) == dark.text_font()->font());
    assert(lv_color_eq(lv_obj_get_style_text_color(ui.volume_label_, LV_PART_MAIN), dark.text_color()));
    assert(lv_obj_get_style_text_font(ui.battery_label_, LV_PART_MAIN) == dark.text_font()->font());
    assert(lv_color_eq(lv_obj_get_style_text_color(ui.battery_label_, LV_PART_MAIN), dark.text_color()));
    ui.SetChatMessage("assistant", "short");
    lv_obj_update_layout(lv_screen_active());
    assert(lv_obj_get_height(ui.status_label_) >= dark.text_font()->font()->line_height);
    assert(lv_obj_get_height(ui.chat_message_label_) >= dark.text_font()->font()->line_height);
    assert(lv_obj_get_height(ui.bottom_bar_) == 40);
    assert(lv_obj_get_scroll_y(ui.bottom_bar_) == 0);
    inside_circle(ui.bottom_bar_);
    ui.RemoveChatMessage("assistant", "different");
    assert(!lv_obj_has_flag(ui.bottom_bar_, LV_OBJ_FLAG_HIDDEN));
    ui.RemoveChatMessage("assistant", "short");
    assert(lv_obj_has_flag(ui.bottom_bar_, LV_OBJ_FLAG_HIDDEN));
    ui.SetChatMessage("system", "hello");
    ui.ClearChatMessages();
    assert(lv_obj_has_flag(ui.bottom_bar_, LV_OBJ_FLAG_HIDDEN));
    assert(strcmp(lv_label_get_text(ui.chat_message_label_), "") == 0);
}
void check_idle_focus() {
    LcdDisplay ui;
    ui.SetupUI();
    lv_label_set_text(ui.network_label_, MATERIAL_SYMBOLS_WIFI);
    lv_obj_update_layout(lv_screen_active());
    const int idle_height = lv_obj_get_height(ui.home_media_box_);
    const int idle_top = lv_obj_get_y(ui.home_media_box_);
    assert(idle_height == 160 && lv_obj_get_width(ui.home_media_box_) == 160);
    lv_area_t wifi;
    lv_obj_get_coords(ui.network_label_, &wifi);
    assert((wifi.x1 + wifi.x2 + 1) / 2 == 180);
    for (auto object : {ui.top_bar_, ui.status_bar_, ui.home_media_box_}) inside_circle(object);
    ui.SetChatMessage("assistant", "这里是回答字幕");
    lv_obj_update_layout(lv_screen_active());
    assert(lv_obj_get_height(ui.home_media_box_) == idle_height);
    assert(lv_obj_get_y(ui.home_media_box_) == idle_top);
    for (auto object : {ui.top_bar_, ui.status_bar_, ui.bottom_bar_, ui.home_media_box_}) inside_circle(object);
    assert(lv_obj_get_y(ui.home_media_box_) + lv_obj_get_height(ui.home_media_box_) < lv_obj_get_y(ui.status_bar_));
    ui.SetHideSubtitle(true);
    lv_obj_update_layout(lv_screen_active());
    assert(lv_obj_get_height(ui.home_media_box_) == idle_height);
    ui.SetHideSubtitle(false);
    lv_obj_update_layout(lv_screen_active());
    assert(lv_obj_get_height(ui.home_media_box_) == idle_height);
    ui.ClearChatMessages();
    lv_obj_update_layout(lv_screen_active());
    assert(lv_obj_get_height(ui.home_media_box_) == idle_height);
    ui.SetStatus("聆听中...");
    ui.SetStatus("说话中...");
    ui.SetStatus("监听已暂停");
    ui.dark_theme.color = lv_color_white();
    ui.SetTheme(&ui.dark_theme);
    lv_obj_update_layout(lv_screen_active());
    assert(lv_obj_get_height(ui.home_media_box_) == idle_height);
    assert(lv_obj_get_y(ui.home_media_box_) == idle_top);
}
void check_independent_clock() {
    LcdDisplay ui;
    ui.SetupUI();
    assert(ui.home_clock_label_);
    assert(strcmp(lv_label_get_text(ui.home_clock_label_), "--:--") == 0);
    auto& app = Application::GetInstance();
    for (auto state : {kDeviceStateIdle, kDeviceStateListening, kDeviceStateSpeaking,
                       kDeviceStateConnecting, kDeviceStateUpgrading}) {
        app.state = state;
        app.connection_alert = state == kDeviceStateConnecting;
        for (bool paused : {false, true}) {
            app.paused = paused;
            ui.SetStatus("服务暂不可用");
            ui.UpdateStatusBar(true);
            char expected[8];
            strftime(expected, sizeof(expected), "%H:%M", localtime(&host_now));
            assert(strcmp(lv_label_get_text(ui.home_clock_label_), expected) == 0);
            assert(strcmp(lv_label_get_text(ui.status_label_), "服务暂不可用") == 0);
            assert(!lv_obj_has_flag(ui.home_clock_label_, LV_OBJ_FLAG_HIDDEN));
        }
    }
    host_now = 0;
    ui.UpdateStatusBar(false);
    assert(strcmp(lv_label_get_text(ui.home_clock_label_), "--:--") == 0);
    lv_obj_update_layout(lv_screen_active());
    lv_area_t clock, wifi;
    lv_obj_get_coords(ui.home_clock_label_, &clock);
    lv_obj_get_coords(ui.network_label_, &wifi);
    assert(clock.x1 + clock.x2 == wifi.x1 + wifi.x2);
    assert(clock.y2 < lv_obj_get_y(ui.top_bar_));
    inside_circle(ui.home_clock_label_);
    assert(lv_obj_get_parent(ui.home_clock_label_) == lv_screen_active());
    app.paused = false;
    app.connection_alert = false;
}
void check_status_icons() {
    LcdDisplay ui;
    ui.SetupUI();
    assert(strcmp(lv_label_get_text(ui.status_label_), Lang::Strings::INITIALIZING) == 0);
    const std::pair<const char*, const char*> states[] = {
        {Lang::Strings::STANDBY, ""},
        {Lang::Strings::LISTENING, MATERIAL_SYMBOLS_MIC},
        {Lang::Strings::WAITING_RESPONSE, MATERIAL_SYMBOLS_PROGRESS_ACTIVITY},
        {Lang::Strings::SPEAKING, MATERIAL_SYMBOLS_VOLUME_UP},
        {Lang::Strings::LISTENING_PAUSED, MATERIAL_SYMBOLS_MIC_OFF},
    };
    for (auto state : states) {
        ui.SetStatus(state.first);
        assert(strcmp(lv_label_get_text(ui.status_label_), state.second) == 0);
        assert(lv_obj_get_style_text_font(ui.status_label_, LV_PART_MAIN) == ui.theme.large_icon_font()->font());
        if (*state.second) {
            // The project Material Symbols are three-byte UTF-8 private-use glyphs.
            const auto bytes = reinterpret_cast<const unsigned char*>(state.second);
            const uint32_t codepoint = ((bytes[0] & 15) << 12) | ((bytes[1] & 63) << 6) | (bytes[2] & 63);
            lv_font_glyph_dsc_t glyph{};
            assert(lv_font_get_glyph_dsc(ui.theme.large_icon_font()->font(), &glyph, codepoint, 0));
            assert(glyph.box_w > 0 && glyph.box_h > 0);
        }
        lv_obj_update_layout(lv_screen_active());
        within_parent(ui.status_label_);
    }
    ui.SetTheme(&ui.dark_theme);
    assert(lv_obj_get_style_text_font(ui.status_label_, LV_PART_MAIN) == ui.dark_theme.large_icon_font()->font());
    ui.SetStatus("网络服务异常");
    assert(strcmp(lv_label_get_text(ui.status_label_), "网络服务异常") == 0);
    assert(lv_obj_get_style_text_font(ui.status_label_, LV_PART_MAIN) == ui.dark_theme.text_font()->font());
    ui.ShowNotification("网络已恢复");
    ui.SetStatus(Lang::Strings::LISTENING_PAUSED);
    assert(strcmp(lv_label_get_text(ui.status_label_), MATERIAL_SYMBOLS_MIC_OFF) == 0);
    assert(lv_obj_has_flag(ui.notification_label_, LV_OBJ_FLAG_HIDDEN));
    ui.SetStatus(Lang::Strings::STANDBY);
    assert(strcmp(lv_label_get_text(ui.status_label_), "") == 0);
}
void check_large_font() {
    LcdDisplay ui;
    ui.SetupUI();
    const auto font = std::make_shared<LvglBuiltInFont>(&font_noto_sans_basic_30_4);
    assert(font->font()->line_height == 43);
    lv_font_glyph_dsc_t glyph{};
    assert(lv_font_get_glyph_dsc(font->font(), &glyph, 0x97f3, 0));
    assert(glyph.box_w > 0 && glyph.box_h > 0); // Real project Chinese glyph, not a placeholder.
    assert(ui.SetTextFont(font)); // Exercise the public production font-update entry.
    assert(ui.theme.text_font() == font && ui.dark_theme.text_font() == font);
    lv_obj_update_layout(lv_screen_active());
    const int media_height = lv_obj_get_height(ui.home_media_box_);
    const int media_top = lv_obj_get_y(ui.home_media_box_);
    assert(media_height == 112 && lv_obj_get_width(ui.home_media_box_) == 112);
    ui.SetStatus(Lang::Strings::LISTENING);
    lv_label_set_text(ui.network_label_, MATERIAL_SYMBOLS_WIFI);
    lv_label_set_text(ui.volume_label_, "100");
    ui.SetChatMessage("assistant", "音量");
    lv_obj_update_layout(lv_screen_active());
    assert(lv_obj_get_height(ui.top_bar_) >= 43 && lv_obj_get_height(ui.status_bar_) >= 43);
    for (auto object : {ui.home_clock_label_, ui.top_bar_, ui.status_bar_, ui.bottom_bar_, ui.home_media_box_}) inside_circle(object);
    for (auto object : {ui.status_label_, ui.battery_label_, ui.network_label_, ui.mute_label_,
                        ui.volume_label_, ui.chat_message_label_}) within_parent(object);
    within_parent(lv_obj_get_parent(ui.volume_label_));
    assert(lv_obj_get_y(ui.home_media_box_) + lv_obj_get_height(ui.home_media_box_) < lv_obj_get_y(ui.status_bar_));
    assert(lv_obj_get_y(ui.status_bar_) + lv_obj_get_height(ui.status_bar_) <= lv_obj_get_y(ui.bottom_bar_));
    ui.ShowNotification("音量正常");
    lv_obj_update_layout(lv_screen_active());
    assert(!lv_obj_has_flag(ui.notification_label_, LV_OBJ_FLAG_HIDDEN));
    within_parent(ui.notification_label_);
    ui.dark_theme.color = lv_color_white();
    ui.SetTheme(&ui.dark_theme);
    assert(lv_obj_get_style_text_font(ui.volume_label_, LV_PART_MAIN) == font->font());
    assert(lv_color_eq(lv_obj_get_style_text_color(ui.volume_label_, LV_PART_MAIN), lv_color_white()));
    for (bool hide : {true, false}) {
        ui.SetHideSubtitle(hide);
        lv_obj_update_layout(lv_screen_active());
        assert(lv_obj_get_height(ui.home_media_box_) == media_height);
        assert(lv_obj_get_y(ui.home_media_box_) == media_top);
    }
    ui.ClearChatMessages();
    lv_obj_update_layout(lv_screen_active());
    assert(lv_obj_get_height(ui.home_media_box_) == media_height);
    assert(lv_obj_get_y(ui.home_media_box_) == media_top);
    for (const char* text : {"23:59", "--:--"}) {
        lv_label_set_text(ui.home_clock_label_, text);
        lv_obj_update_layout(lv_screen_active());
        assert(lv_obj_get_height(ui.home_clock_label_) >= font->font()->line_height);
        lv_point_t size;
        lv_text_get_size(&size, text, font->font(), 0, 0, LV_COORD_MAX, LV_TEXT_FLAG_NONE);
        assert(size.x <= lv_obj_get_width(ui.home_clock_label_));
    }
    lv_font_t huge = font_noto_sans_basic_30_4;
    huge.line_height = 80;
    const auto old_font = ui.dark_theme.text_font();
    assert(!ui.SetTextFont(std::make_shared<LvglBuiltInFont>(&huge)));
    assert(ui.theme.text_font() == old_font && ui.dark_theme.text_font() == old_font);
    lv_font_t wide_clock = font_noto_sans_basic_30_4;
    wide_clock.get_glyph_dsc = [](const lv_font_t*, lv_font_glyph_dsc_t* dsc,
                                  uint32_t letter, uint32_t next) {
        const bool found = font_noto_sans_basic_30_4.get_glyph_dsc(&font_noto_sans_basic_30_4, dsc, letter, next);
        if (letter == ':') dsc->adv_w = 200;
        return found;
    };
    assert(!ui.SetTextFont(std::make_shared<LvglBuiltInFont>(&wide_clock)));
    assert(ui.theme.text_font() == old_font && ui.dark_theme.text_font() == old_font);
    assert(ui.SetTextFont(std::make_shared<LvglBuiltInFont>(&font_noto_sans_basic_16_4)));
    lv_obj_update_layout(lv_screen_active());
    assert(lv_obj_get_height(ui.top_bar_) == 32 && lv_obj_get_height(ui.status_bar_) == 32);
    assert(lv_obj_get_height(ui.bottom_bar_) == 40);
    assert(lv_obj_get_height(ui.home_media_box_) == 160);
}
void image_is_contained(lv_obj_t* object, int source_width, int source_height) {
    lv_obj_update_layout(lv_screen_active());
    within_parent(object);
    auto parent = lv_obj_get_parent(object);
    inside_circle(parent);
    assert(lv_image_get_inner_align(object) == LV_IMAGE_ALIGN_CONTAIN);
    const int scale_x = lv_image_get_scale_x(object), scale_y = lv_image_get_scale_y(object);
    assert(scale_x == scale_y && scale_x > 0);
    const int width = source_width * scale_x / 256, height = source_height * scale_y / 256;
    assert(width <= lv_obj_get_width(object) && height <= lv_obj_get_height(object));
    assert(width >= lv_obj_get_width(object) - 2 || height >= lv_obj_get_height(object) - 2);
}
void check_media() {
    LcdDisplay ui;
    ui.SetupUI();
    auto collection = ui.theme.emoji_collection();
    collection->images["square"] = std::make_shared<HostPngImage>(png_square, sizeof(png_square));
    collection->images["portrait"] = std::make_shared<HostPngImage>(png_portrait, sizeof(png_portrait));
    collection->images["small"] = std::make_shared<HostImage>(24, 24);
    collection->images["animated"] = std::make_shared<HostImage>(240, 240, true);
    assert(ui.SetEmotion("square"));
    image_is_contained(ui.emoji_image_, 240, 240);
    assert(ui.SetEmotion("portrait"));
    image_is_contained(ui.emoji_image_, 40, 640);
    assert(ui.SetEmotion("small"));
    image_is_contained(ui.emoji_image_, 24, 24);
    assert(ui.SetEmotion("animated"));
    assert(ui.gif_controller_ && ui.gif_controller_->IsPlaying());
    image_is_contained(ui.emoji_image_, 240, 240);
    ui.gif_controller_->NextFrame(80, 480);
    image_is_contained(ui.emoji_image_, 80, 480);
    ui.gif_controller_->NextFrame(120, 120);
    image_is_contained(ui.emoji_image_, 120, 120);
    ui.SetPreviewImage(std::make_unique<HostPngImage>(png_portrait, sizeof(png_portrait)));
    assert(ui.preview_timer_->active);
    assert(lv_obj_has_flag(ui.emoji_box_, LV_OBJ_FLAG_HIDDEN));
    assert(!lv_obj_has_flag(ui.preview_image_, LV_OBJ_FLAG_HIDDEN));
    assert(!ui.gif_controller_->IsPlaying());
    image_is_contained(ui.preview_image_, 40, 640);
    ui.SetPreviewImage(std::make_unique<HostImage>(240, 240));
    image_is_contained(ui.preview_image_, 240, 240);
    assert(ui.SetTextFont(std::make_shared<LvglBuiltInFont>(&font_noto_sans_basic_30_4)));
    image_is_contained(ui.preview_image_, 240, 240);
    ui.preview_timer_->callback(); // Expiry invokes the production clearing/restoration entry.
    assert(!ui.preview_timer_->active && !ui.preview_image_cached_);
    assert(lv_obj_has_flag(ui.preview_image_, LV_OBJ_FLAG_HIDDEN));
    assert(!lv_obj_has_flag(ui.emoji_box_, LV_OBJ_FLAG_HIDDEN));
    assert(ui.gif_controller_->IsPlaying());
    image_is_contained(ui.emoji_image_, 120, 120);
    assert(ui.SetEmotion("square"));
    assert(!ui.gif_controller_);
    image_is_contained(ui.emoji_image_, 240, 240);
    ui.SetPreviewImage(std::make_unique<HostImage>(24, 24));
    image_is_contained(ui.preview_image_, 24, 24);
    // Destruction below runs the real derived and base cleanup with an active preview.
}
void check_paused_notification() {
    LcdDisplay ui;
    ui.SetupUI();
    auto& app = Application::GetInstance();
    app.paused = true;
    ui.SetStatus("监听已暂停");
    ui.ShowNotification("网络已恢复", 30000);
    assert(!lv_obj_has_flag(ui.status_label_, LV_OBJ_FLAG_HIDDEN));
    assert(lv_obj_has_flag(ui.notification_label_, LV_OBJ_FLAG_HIDDEN));
    assert(strcmp(lv_label_get_text(ui.status_label_), MATERIAL_SYMBOLS_MIC_OFF) == 0);
    app.paused = false;
    ui.ShowNotification("网络已恢复", 30000);
    assert(lv_obj_has_flag(ui.status_label_, LV_OBJ_FLAG_HIDDEN));
    assert(!lv_obj_has_flag(ui.notification_label_, LV_OBJ_FLAG_HIDDEN));
    ui.SetStatus("待机");
    before_display_lock = [&]() {
        // The network callback passed its first pause check, then the main
        // task completed pause before that callback acquired the display lock.
        app.paused = true;
        ui.SetStatus("监听已暂停");
    };
    ui.ShowNotification("旧连接通知", 30000);
    assert(!lv_obj_has_flag(ui.status_label_, LV_OBJ_FLAG_HIDDEN));
    assert(lv_obj_has_flag(ui.notification_label_, LV_OBJ_FLAG_HIDDEN));
    assert(strcmp(lv_label_get_text(ui.status_label_), MATERIAL_SYMBOLS_MIC_OFF) == 0);
    app.paused = false;
}
int main(int argc, char** argv) {
    assert(argc == 2);
    lv_init();
    auto display = lv_display_create(360, 360);
    const auto original_children = lv_obj_get_child_count(lv_screen_active());
    std::string scenario = argv[1];
    if (scenario == "layout") check_default_layout();
    else if (scenario == "idle-focus") check_idle_focus();
    else if (scenario == "clock") check_independent_clock();
    else if (scenario == "status-icons") check_status_icons();
    else if (scenario == "font") check_large_font();
    else if (scenario == "media") check_media();
    else if (scenario == "paused-notification") check_paused_notification();
    else assert(false);
    assert(lv_obj_get_child_count(lv_screen_active()) == original_children);
    assert(timer_count == 0 && live_images == 0 && live_gifs == 0);
    lv_display_delete(display);
    lv_deinit();
}
'''


def extract_method(text, signature):
    """Select a method after preprocessing the production configuration branches."""
    start = text.index(signature)
    opening = text.index("{", start)
    end, depth = opening + 1, 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end] + "\n"


class HomeDisplayLayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("cmake"):
            raise unittest.SkipTest("requires CMake for the repository LVGL host library")
        compiler = compiler_command()
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        folder = Path(cls.temp.name)
        # Valid PNG fixtures exercise the repository decoder, with dimensions
        # supplied by its real info callback rather than a fabricated header.
        def png(width, height):
            def chunk(kind, data):
                return (struct.pack(">I", len(data)) + kind + data +
                        struct.pack(">I", zlib.crc32(kind + data)))
            rows = (b"\0" + bytes((24, 80, 128, 255)) * width) * height
            return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) +
                    chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
        (folder / "png_samples.h").write_text("\n".join(
            "static const uint8_t png_" + name + "[] = {" +
            ",".join(str(value) for value in png(width, height)) + "};"
            for name, width, height in (("square", 240, 240), ("portrait", 40, 640))),
            encoding="utf-8")
        (folder / "lv_conf.h").write_text(
            "#pragma once\n#define LV_USE_DRAW_SW 0\n#define LV_USE_LODEPNG 1\n"
            "#define LV_MEM_SIZE (1024 * 1024U)\n",
            encoding="utf-8")
        cmake = ["cmake_minimum_required(VERSION 3.16)", "project(home_ui LANGUAGES C CXX ASM)",
                 "set(CONFIG_LV_BUILD_EXAMPLES OFF CACHE BOOL \"\" FORCE)",
                 "set(CONFIG_LV_BUILD_DEMOS OFF CACHE BOOL \"\" FORCE)",
                 "set(CONFIG_LV_USE_THORVG_INTERNAL OFF CACHE BOOL \"\" FORCE)",
                 f'set(LV_BUILD_CONF_PATH "{folder.as_posix()}/lv_conf.h" CACHE PATH "" FORCE)',
                 f'add_subdirectory("{ROOT.as_posix()}/managed_components/lvgl__lvgl" lvgl)']
        font_folder = ROOT / "managed_components/78__xiaozhi-fonts/src"
        fonts = ("font_noto_sans_basic_16_4.c", "font_noto_sans_basic_30_4.c",
                 "font_material_symbols_16_4.c", "font_material_symbols_30_4.c", "material_symbols.c")
        cmake += ['add_library(home_fonts STATIC ' + " ".join(
            f'"{(font_folder / font).as_posix()}"' for font in fonts) + ')',
            f'target_include_directories(home_fonts PRIVATE "{font_folder.parent.as_posix()}/include")',
            "target_link_libraries(home_fonts PUBLIC lvgl::lvgl)"]
        raw = without_includes((ROOT / "main/display/lcd_display.cc").read_text(encoding="utf-8"))
        methods = ("void LcdDisplay::SetupHomeUI(", "void LcdDisplay::SetupUI(",
                   "void LcdDisplay::UpdateHomeLayout(", "LcdDisplay::~LcdDisplay(",
                   "void LcdDisplay::ShowEmotion(", "bool LcdDisplay::SetEmotion(",
                   "void LcdDisplay::SetPreviewImage(",
                   "void LcdDisplay::SetChatMessage(", "void LcdDisplay::RemoveChatMessage(",
                   "void LcdDisplay::ClearChatMessages(", "void LcdDisplay::SetTheme(",
                   "void LcdDisplay::SetHideSubtitle(")
        common = method("main/display/lvgl_display/lvgl_display.cc", "void LvglDisplay::SetStatus(")
        common += method("main/display/lvgl_display/lvgl_display.cc", "void LvglDisplay::ApplyBatteryStatusStyle(")
        common += method("main/display/lvgl_display/lvgl_display.cc", "bool LvglDisplay::SetTextFont(")
        common += method("main/display/lvgl_display/lvgl_display.cc", "LvglDisplay::~LvglDisplay(")
        common += method("main/display/lvgl_display/lvgl_display.cc", "void LvglDisplay::ShowNotification(const char*")
        common += method("main/display/lvgl_display/lvgl_display.cc", "void LvglDisplay::UpdateStatusBar(")
        cls.binaries = []
        for wechat in (0, 1):
            for multiline in (0, 1):
                name = f"layout-{wechat}-{multiline}"
                flags = ["-DCONFIG_WAVESHARE_1_85C_HOME=1", f"-DCONFIG_USE_WECHAT_MESSAGE_STYLE={wechat}",
                         f"-DCONFIG_USE_MULTILINE_CHAT_MESSAGE={multiline}"]
                result = subprocess.run(compiler + ["-E", "-P", "-x", "c++", *flags, "-"],
                                        input=raw, capture_output=True, text=True, timeout=30)
                if result.returncode:
                    raise AssertionError(result.stderr)
                production = "".join(extract_method(result.stdout, signature) for signature in methods)
                (folder / f"{name}.cc").write_text(LVGL_PLATFORM + common + production + LAYOUT_CHECKS,
                                                  encoding="utf-8")
                # Catch harness/API type errors before building the LVGL host library.
                result = subprocess.run(compiler + ["-fsyntax-only", "-std=c++17", *flags,
                                        f'-DLV_CONF_PATH="{folder.as_posix()}/lv_conf.h"',
                                        "-DLV_KCONFIG_IGNORE", "-I", str(ROOT), "-I",
                                        str(ROOT / "managed_components/lvgl__lvgl"),
                                        str(folder / f"{name}.cc")],
                                        capture_output=True, text=True, timeout=30)
                if result.returncode:
                    raise AssertionError(result.stdout + result.stderr)
                cmake += [f"add_executable({name} {name}.cc)",
                          f"target_compile_features({name} PRIVATE cxx_std_17)",
                          f'target_include_directories({name} PRIVATE "{ROOT.as_posix()}")',
                          f"target_compile_definitions({name} PRIVATE CONFIG_WAVESHARE_1_85C_HOME=1 CONFIG_USE_MULTILINE_CHAT_MESSAGE={multiline})",
                          f"target_link_libraries({name} PRIVATE home_fonts m)"]
                cls.binaries.append(folder / "build" / name)
        (folder / "CMakeLists.txt").write_text("\n".join(cmake), encoding="utf-8")
        for command in (["cmake", "-S", str(folder), "-B", str(folder / "build")],
                        ["cmake", "--build", str(folder / "build"), "-j", str(min(os.cpu_count() or 1, 8))]):
            result = subprocess.run(command, capture_output=True, text=True, timeout=180)
            if result.returncode:
                raise AssertionError(result.stdout[-3000:] + result.stderr[-6000:])

    def run_scenario(self, scenario):
        for binary in self.binaries:
            with self.subTest(configuration=binary.name):
                result = subprocess.run([str(binary), scenario], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_real_lvgl_layout_in_both_styles_and_multiline_modes(self):
        self.run_scenario("layout")

    def test_media_stays_fixed_across_subtitles_states_and_theme(self):
        self.run_scenario("idle-focus")

    def test_clock_has_own_centered_row_in_all_states_and_unknown_time(self):
        self.run_scenario("clock")

    def test_normal_states_use_material_icons_and_faults_keep_text(self):
        self.run_scenario("status-icons")

    def test_project_noto30_font_entry_parent_bounds_and_rejection(self):
        self.run_scenario("font")

    def test_production_emotion_gif_preview_containment_and_cleanup(self):
        self.run_scenario("media")

    def test_notifications_do_not_hide_the_persistent_paused_status(self):
        self.run_scenario("paused-notification")


if __name__ == "__main__":
    unittest.main()
