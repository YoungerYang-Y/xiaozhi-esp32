"""Host regressions for production menu touch release and display contracts."""

import pathlib
import shutil
import subprocess
import tempfile
import unittest

from test_startup_wake_word_hint import definition

ROOT = pathlib.Path(__file__).resolve().parents[2]
BOARD = ROOT / "main/boards/waveshare/esp32-s3-touch-lcd-1.85c"


@unittest.skipUnless(shutil.which("c++"), "requires a host C++ compiler")
class HomeNetworkMenuTests(unittest.TestCase):
    def compile_and_run(self, source):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)
            driver = path / "menu.cc"
            binary = path / "menu"
            driver.write_text(source)
            result = subprocess.run(
                ["c++", "-std=c++17", "-fsanitize=undefined", "-fno-sanitize-recover=undefined",
                 "-I", str(BOARD), str(driver), "-o", str(binary)],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_menu_uses_single_complete_short_release(self):
        self.compile_and_run(r'''
#include <cassert>
#include "home_touch_gesture.h"
int main() {
    HomeTouchGesture gesture;
    assert(!gesture.UpdateMenu(0, true, 1, 140, 200));
    assert(!gesture.UpdateMenu(20, true, 1, 142, 202));
    assert(gesture.UpdateMenu(60, true, 0));
    assert(!gesture.UpdateMenu(80, true, 0));
}
''')

    def test_menu_context_change_discards_held_touch_and_double_tap_history(self):
        self.compile_and_run(r'''
#include <cassert>
#include "home_touch_gesture.h"
int main() {
    HomeTouchGesture gesture;
    assert(!gesture.Update(0, true, 1, 100, 100));
    gesture.ResetForMenuChange();
    assert(!gesture.UpdateMenu(40, true, 1, 100, 100));
    assert(!gesture.UpdateMenu(80, true, 0));
    assert(!gesture.UpdateMenu(100, true, 1, 120, 150));
    assert(gesture.UpdateMenu(150, true, 0));
    assert(gesture.tap_x() == 120 && gesture.tap_y() == 150);
    gesture.ResetForMenuChange();
    // The menu tap cannot become the first tap of the ordinary double-tap gesture.
    assert(!gesture.Update(180, true, 1, 120, 150));
    assert(!gesture.Update(230, true, 0));
    assert(!gesture.Update(260, true, 1, 120, 150));
    assert(gesture.Update(310, true, 0));
    gesture.ResetForMenuChange();
    assert(!gesture.UpdateMenu(340, true, 1, 120, 150));
    assert(gesture.UpdateMenu(390, true, 0));
}
''')

    def test_menu_rejects_swipe_hold_and_read_error(self):
        self.compile_and_run(r'''
#include <cassert>
#include "home_touch_gesture.h"
int main() {
    HomeTouchGesture g;
    assert(!g.UpdateMenu(0, true, 1, 100, 100));
    assert(!g.UpdateMenu(20, true, 1, 150, 100));
    assert(!g.UpdateMenu(40, true, 1, 100, 100));
    assert(!g.UpdateMenu(60, true, 0));
    assert(!g.UpdateMenu(100, true, 1, 100, 100));
    assert(!g.UpdateMenu(401, true, 0));
    assert(!g.UpdateMenu(500, true, 1, 100, 100));
    assert(!g.UpdateMenu(510, false, 0));
    assert(!g.UpdateMenu(540, true, 0));
    assert(!g.UpdateMenu(600, true, 1, 100, 100));
    assert(g.UpdateMenu(660, true, 0));
}
''')

    def test_display_menu_hit_testing_visibility_and_ascii_recovery(self):
        display = (ROOT / "main/display/lcd_display.cc").read_text()
        self.compile_and_run(display_production_source() + "\n".join(
            definition(display, signature) for signature in (
                "void LcdDisplay::ShowNetworkMenu(", "int LcdDisplay::NetworkMenuHitTest(",
            )
        ) + r'''
int main() {
    LcdDisplay d;
    assert(d.NetworkMenuHitTest(180, 180) == -1);
    d.ShowNetworkMenu({"立即重试", "保持离线", "进入配网", "返回"}, 3);
    assert(d.network_menu_generation_ == 1);
    assert(d.network_menu_rows_.size() == 4);
    assert(d.network_menu_rows_[3]->children[0]->text == "Back");
    assert(d.network_menu_rows_[2]->children[0]->text == "Wi-Fi setup");
    for (int i = 0; i < 4; ++i) {
        auto* row = d.network_menu_rows_[i];
        assert(d.NetworkMenuHitTest(row->x + 1, row->y + 1) == i);
        assert(d.NetworkMenuHitTest(row->x + row->width - 1, row->y + row->height - 1) == i);
        assert(d.NetworkMenuHitTest(row->x - 1, row->y) == -1);
        // All row corners fit inside the circular 360px panel.
        for (int x : {row->x, row->x + row->width}) {
            for (int y : {row->y, row->y + row->height}) {
                assert((x - 180) * (x - 180) + (y - 180) * (y - 180) <= 180 * 180);
            }
        }
    }
    assert(d.NetworkMenuHitTest(-1, 180) == -1);
    assert(d.NetworkMenuHitTest(180, 360) == -1);
    const auto* first = d.network_menu_rows_[0];
    assert(d.NetworkMenuHitTest(180, first->y + first->height + 1) == -1);
    d.ShowNetworkMenu({"恢复联网", "进入配网", "返回"}, 0);
    assert(d.network_menu_generation_ == 1); // Selection/content refresh isn't a new lifetime.
    assert(d.network_menu_rows_[0]->children[0]->text == "Reconnect");
    d.ShowNetworkMenu({}, 0);
    assert(d.network_menu_generation_ == 2);
    assert(d.NetworkMenuHitTest(180, 180) == -1);
    d.ShowNetworkMenu({}, 0);
    assert(d.network_menu_generation_ == 2);
    lock_available = false;
    d.ShowNetworkMenu({"返回"}, 0);
    assert(d.network_menu_generation_ == 2);
    assert(d.NetworkMenuHitTest(180, 180) == -1);
}
''')

    def test_asset_barrier_releases_mapped_resources_and_is_repeatable(self):
        display = (ROOT / "main/display/lcd_display.cc").read_text()
        self.compile_and_run(display_production_source() + definition(
            display, "bool LcdDisplay::PrepareForAssetUpdate()"
        ) + r'''
int main() {
    LcdDisplay d;
    auto& themes = LvglThemeManager::GetInstance();
    d.current_theme_ = &themes.light;
    auto old_font = std::make_shared<LvglBuiltInFont>(&external_font);
    std::weak_ptr<LvglFont> weak_font = old_font;
    themes.light.set_text_font(old_font);
    themes.dark.set_text_font(old_font);
    old_font.reset();
    auto old_image = std::make_shared<LvglImage>();
    std::weak_ptr<LvglImage> weak_image = old_image;
    themes.light.set_background_image(old_image);
    themes.dark.set_background_image(old_image);
    old_image.reset();
    auto old_emojis = std::make_shared<EmojiCollection>();
    std::weak_ptr<EmojiCollection> weak_emojis = old_emojis;
    themes.light.set_emoji_collection(old_emojis);
    themes.dark.set_emoji_collection(old_emojis);
    old_emojis.reset();
    d.emoji_image_->source = d.preview_image_->source = &external_font;
    d.gif_controller_ = std::make_unique<LvglGif>();
    d.preview_image_cached_ = std::make_unique<LvglImage>();
    std::vector<lv_obj_t*> detached_labels;
    for (auto* layer : {&screen_layer, &top_layer, &sys_layer, &bottom_layer}) {
        auto* label = lv_obj_create(layer);
        label->font = &external_font;
        label->source = &external_font;
        detached_labels.push_back(label);
    }
    lock_available = false;
    assert(!d.PrepareForAssetUpdate());
    assert(!weak_font.expired() && !weak_image.expired() && !weak_emojis.expired());
    lock_available = true;
    auto saved = d.display_;
    d.display_ = nullptr;
    assert(!d.PrepareForAssetUpdate());
    d.display_ = saved;
    assert(d.PrepareForAssetUpdate());
    assert(draw_finished && image_cache_dropped && header_cache_dropped);
    assert(timer_stopped && glyphs_cleared);
    assert(weak_font.expired() && weak_image.expired() && weak_emojis.expired());
    assert(!d.gif_controller_ && !d.preview_image_cached_);
    assert(!d.emoji_image_->source && !d.preview_image_->source);
    assert(!d.emotion_uses_image_ && d.preview_image_->hidden && d.emoji_image_->hidden);
    assert(!d.emoji_label_->hidden && !d.emoji_box_->hidden);
    for (auto* label : detached_labels) {
        assert(label->font == &BUILTIN_TEXT_FONT);
        assert(label->source == nullptr);
    }
    assert(d.PrepareForAssetUpdate());
}
''')

    def test_boot_events_are_dispatched_to_main_task_without_release_confirmation(self):
        board = (BOARD / "esp32-s3-touch-lcd-1.85c.cc").read_text()
        self.compile_and_run(r'''
#include <cassert>
#include <functional>
#include <vector>
#define CONFIG_WAVESHARE_1_85C_HOME_OFFLINE 1
#define ESP_LOGI(...)
struct Button {
    std::function<void()> click, hold, release, double_click;
    void OnClick(std::function<void()> f) { click = f; }
    void OnLongPress(std::function<void()> f) { hold = f; }
    void OnPressUp(std::function<void()> f) { release = f; }
    void OnDoubleClick(std::function<void()> f) { double_click = f; }
};
struct Application {
    int primary = 0, menu = 0, pause = 0;
    std::vector<std::function<void()>> queue;
    static Application& GetInstance() { static Application app; return app; }
    void Schedule(std::function<void()> f) { queue.push_back(f); }
    void PrimaryButtonPressed() { ++primary; }
    void NetworkMenuPressed() { ++menu; }
    void ToggleListeningPause() { ++pause; }
    void Drain() { auto events = std::move(queue); queue.clear(); for (auto& f : events) f(); }
};
struct Board {
    Button boot_button_;
''' + definition(board, "void InitializeButtons()") + r'''
};
int main() {
    Board b;
    b.InitializeButtons();
    auto& app = Application::GetInstance();
    b.boot_button_.hold();
    b.boot_button_.release();
    assert(app.menu == 0 && app.primary == 0 && app.pause == 0);
    app.Drain();
    assert(app.menu == 1 && app.primary == 0 && app.pause == 0);
    b.boot_button_.click();
    assert(app.primary == 0);
    app.Drain();
    assert(app.primary == 1 && app.menu == 1);
    b.boot_button_.double_click();
    assert(app.pause == 0);
    app.Drain();
    assert(app.pause == 1 && app.primary == 1 && app.menu == 1);
}
''')

    def test_asset_update_rebinds_subtitle_and_icons_with_the_real_theme_path(self):
        lcd = (ROOT / "main/display/lcd_display.cc").read_text()
        self.compile_and_run(display_production_source() + definition(
            lcd, "bool LcdDisplay::PrepareForAssetUpdate()"
        ) + r'''
int main() {
    LcdDisplay d;
    assert(d.PrepareForAssetUpdate());
    auto& themes = LvglThemeManager::GetInstance();
    for (int line_height : {30, 42}) {
        lv_font_t replacement{line_height, true};
        auto replacement_owner = std::make_shared<LvglBuiltInFont>(&replacement);
        assert(d.SetTextFont(replacement_owner));
        for (auto* theme : {&themes.light, &themes.dark}) {
            d.SetTheme(theme);
            auto* text = replacement_owner->font();
            auto* icon = line_height >= 40 ? theme->large_icon_font()->font()
                                          : theme->icon_font()->font();
            for (auto* label : {d.bottom_bar_, d.chat_message_label_, d.home_clock_label_,
                               d.volume_label_, d.status_label_, d.notification_label_}) {
                assert(label->font == text && "asset update left an old local font style");
            }
            assert(d.mute_label_->font == icon && d.network_label_->font == icon);
            assert(d.emoji_label_->font == theme->large_icon_font()->font());
            assert(d.battery_label_->font == text); // Unknown battery uses a text question mark.
            d.battery_status_unknown_ = false;
            d.home_status_is_icon_ = true;
            d.UpdateHomeLayout(theme);
            assert(d.battery_label_->font == icon);
            assert(d.status_label_->font == theme->large_icon_font()->font());
            d.battery_status_unknown_ = true;
            d.home_status_is_icon_ = false;
        }
    }
}
''')

    def test_non_home_display_does_not_claim_a_safe_resource_barrier(self):
        display = (ROOT / "main/display/lcd_display.cc").read_text()
        for home in (0, 1):
            with self.subTest(home=home):
                driver = display_production_source().replace(
                    "#define CONFIG_WAVESHARE_1_85C_HOME_OFFLINE 1",
                    "#define CONFIG_WAVESHARE_1_85C_HOME_OFFLINE 0",
                ).replace("#define CONFIG_WAVESHARE_1_85C_HOME 1",
                          f"#define CONFIG_WAVESHARE_1_85C_HOME {home}")
                self.compile_and_run(
                    driver + definition(display, "bool LcdDisplay::PrepareForAssetUpdate()")
                    + "\nint main() { LcdDisplay d; assert(!d.PrepareForAssetUpdate()); }"
                )


DISPLAY_DRIVER = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>
#include <string>
#include <vector>
#define CONFIG_WAVESHARE_1_85C_HOME_OFFLINE 1
#define CONFIG_WAVESHARE_1_85C_HOME 1
constexpr int LV_OPA_COVER = 255, LV_OBJ_FLAG_SCROLLABLE = 1, LV_OBJ_FLAG_HIDDEN = 2;
constexpr int LV_TEXT_ALIGN_CENTER = 0, LV_LABEL_LONG_SCROLL_CIRCULAR = 0;
constexpr int LV_OBJ_TREE_WALK_NEXT = 0, LV_PALETTE_RED = 1;
constexpr int LV_OPA_50 = 128, LV_ALIGN_TOP_MID = 0, LV_ALIGN_LEFT_MID = 1, LV_ALIGN_RIGHT_MID = 2;
constexpr int LV_COORD_MAX = 32767, LV_TEXT_FLAG_NONE = 0, LV_IMAGE_ALIGN_CONTAIN = 0;
using lv_coord_t = int;
struct lv_point_t { int x = 0, y = 0; };
const char* MATERIAL_SYMBOLS_VOLUME_UP = "volume";
const char* MATERIAL_SYMBOLS_WIFI = "wifi";
const char* MATERIAL_SYMBOLS_ROBOT_2 = "robot";
struct lv_font_t { int line_height = 30; bool unicode = false; };
lv_font_t BUILTIN_TEXT_FONT, BUILTIN_ICON_FONT, font_material_symbols_30_4, font_noto_emoji_30_4;
lv_font_t external_font;
struct lv_font_glyph_dsc_t {};
bool lv_font_get_glyph_dsc(const lv_font_t* font, lv_font_glyph_dsc_t*, char32_t ch, int) {
    return ch < 128 || font->unicode;
}
struct lv_area_t { int x1, y1, x2, y2; };
struct lv_obj_t {
    int x = 0, y = 0, width = 360, height = 360;
    bool hidden = false;
    const lv_font_t* font = &external_font;
    const void* source = nullptr;
    std::string text;
    std::vector<lv_obj_t*> children;
    lv_obj_t* parent = nullptr;
};
lv_obj_t screen_layer, top_layer, sys_layer, bottom_layer;
struct lv_display_t {};
lv_obj_t* lv_display_get_layer_top(lv_display_t*) { return &top_layer; }
lv_obj_t* lv_display_get_layer_sys(lv_display_t*) { return &sys_layer; }
lv_obj_t* lv_display_get_layer_bottom(lv_display_t*) { return &bottom_layer; }
lv_obj_t* lv_display_get_screen_active(lv_display_t*) { return &screen_layer; }
lv_obj_t* lv_obj_create(lv_obj_t* parent) {
    auto* obj = new lv_obj_t;
    parent->children.push_back(obj);
    obj->parent = parent;
    return obj;
}
lv_obj_t* lv_label_create(lv_obj_t* parent) { return lv_obj_create(parent); }
void lv_obj_clean(lv_obj_t* obj) {
    for (auto* child : obj->children) { lv_obj_clean(child); delete child; }
    obj->children.clear();
}
void lv_obj_delete(lv_obj_t* obj) { lv_obj_clean(obj); delete obj; }
void lv_obj_set_size(lv_obj_t* o, int w, int h) { o->width = w; o->height = h; }
void lv_obj_set_width(lv_obj_t* o, int w) { o->width = w; }
void lv_obj_set_height(lv_obj_t* o, int h) { o->height = h; }
lv_obj_t* lv_obj_get_parent(lv_obj_t* o) { return o->parent; }
lv_obj_t* lv_screen_active() { return &screen_layer; }
int lv_palette_main(int) { return 0xFF0000; }
void lv_obj_align(lv_obj_t*, int, int, int) {}
void lv_image_set_inner_align(lv_obj_t*, int) {}
void lv_text_get_size(lv_point_t* size, const char* text, const lv_font_t* font,
                      int, int, int, int) {
    *size = {static_cast<int>(std::strlen(text)) * 5, font->line_height};
}
void lv_obj_set_pos(lv_obj_t* o, int x, int y) { o->x = x; o->y = y; }
void lv_obj_remove_style_all(lv_obj_t*) {}
void lv_obj_set_style_pad_all(lv_obj_t*, int, int) {}
void lv_obj_set_style_border_width(lv_obj_t*, int, int) {}
void lv_obj_set_style_radius(lv_obj_t*, int, int) {}
void lv_obj_set_style_bg_opa(lv_obj_t*, int, int) {}
void lv_obj_set_style_bg_color(lv_obj_t*, int, int) {}
void lv_obj_set_style_text_color(lv_obj_t*, int, int) {}
void lv_obj_set_style_text_align(lv_obj_t*, int, int) {}
void lv_obj_set_style_text_font(lv_obj_t* o, const lv_font_t* f, int) { o->font = f; }
void lv_obj_set_style_bg_image_src(lv_obj_t* o, const void* src, int) { o->source = src; }
void lv_obj_remove_flag(lv_obj_t* o, int flag) { if (flag == LV_OBJ_FLAG_HIDDEN) o->hidden = false; }
void lv_obj_add_flag(lv_obj_t* o, int flag) { if (flag == LV_OBJ_FLAG_HIDDEN) o->hidden = true; }
int lv_color_hex(int color) { return color; }
void lv_label_set_long_mode(lv_obj_t*, int) {}
void lv_label_set_text(lv_obj_t* o, const char* s) { o->text = s; }
void lv_obj_center(lv_obj_t*) {}
void lv_obj_move_foreground(lv_obj_t*) {}
void lv_obj_update_layout(lv_obj_t*) {}
void lv_obj_get_coords(lv_obj_t* o, lv_area_t* a) {
    *a = {o->x, o->y, o->x + o->width - 1, o->y + o->height - 1};
}
template <typename Fn> void lv_obj_tree_walk(lv_obj_t* o, Fn fn, void* user) {
    fn(o, user);
    for (auto* child : o->children) lv_obj_tree_walk(child, fn, user);
}
bool lock_available = true, draw_finished = false, timer_stopped = false;
bool image_cache_dropped = false, header_cache_dropped = false, glyphs_cleared = false;
struct DisplayLockGuard {
    explicit DisplayLockGuard(void*) {}
    explicit operator bool() const { return lock_available; }
};
void lv_draw_wait_for_finish() { draw_finished = true; }
void lv_image_set_src(lv_obj_t* o, const void* src) { o->source = src; }
void lv_image_cache_drop(const void*) { image_cache_dropped = true; }
void lv_image_header_cache_drop(const void*) { header_cache_dropped = true; }
void esp_timer_stop(void*) { timer_stopped = true; }
struct LvglImage { const void* image_dsc() const { return this; } };
struct EmojiCollection {};
struct LvglFont {
    lv_font_t data;
    const lv_font_t* font() const { return &data; }
    void SetFallback(const lv_font_t*) {}
};
struct LvglBuiltInFont : LvglFont {
    explicit LvglBuiltInFont(const lv_font_t* f) { data = *f; }
};
struct Theme {};
struct LvglTheme : Theme {
    std::shared_ptr<LvglFont> text = std::make_shared<LvglBuiltInFont>(&BUILTIN_TEXT_FONT);
    std::shared_ptr<LvglFont> icon = std::make_shared<LvglBuiltInFont>(&BUILTIN_ICON_FONT);
    std::shared_ptr<LvglFont> large_icon = std::make_shared<LvglBuiltInFont>(&font_material_symbols_30_4);
    std::shared_ptr<LvglFont> emoji = std::make_shared<LvglBuiltInFont>(&font_noto_emoji_30_4);
    std::shared_ptr<LvglImage> background;
    std::shared_ptr<EmojiCollection> emojis;
    auto text_font() { return text; }
    auto icon_font() { return icon; }
    auto large_icon_font() { return large_icon; }
    auto background_image() { return background; }
    int text_color() const { return 0xFFFFFF; }
    int background_color() const { return 0; }
    int low_battery_color() const { return 0xFF0000; }
    void set_text_font(std::shared_ptr<LvglFont> f) { text = f; }
    void set_icon_font(std::shared_ptr<LvglFont> f) { icon = f; }
    void set_large_icon_font(std::shared_ptr<LvglFont> f) { large_icon = f; }
    void set_emoji_font(std::shared_ptr<LvglFont> f) { emoji = f; }
    void set_background_image(std::shared_ptr<LvglImage> i) { background = i; }
    void set_emoji_collection(std::shared_ptr<EmojiCollection> e) { emojis = e; }
};
struct LvglThemeManager {
    LvglTheme light, dark;
    static LvglThemeManager& GetInstance() { static LvglThemeManager manager; return manager; }
    LvglTheme* GetTheme(const std::string& name) { return name == "light" ? &light : &dark; }
};
struct LvglGif { void Stop() {} };
struct Display {
    Theme* current_theme_ = &LvglThemeManager::GetInstance().light;
    virtual void SetTheme(Theme* theme) { current_theme_ = theme; }
};
struct LvglDisplay : Display {
    bool home_status_is_icon_ = false;
    bool battery_status_supported_ = true, battery_status_unknown_ = true, battery_status_low_ = false;
    lv_obj_t* home_clock_label_ = nullptr;
    lv_obj_t* mute_label_ = nullptr;
    lv_obj_t* network_label_ = nullptr;
    lv_obj_t* volume_label_ = nullptr;
    lv_obj_t* battery_label_ = nullptr;
    lv_obj_t* status_label_ = nullptr;
    lv_obj_t* notification_label_ = nullptr;
    lv_obj_t* low_battery_popup_ = nullptr;
    void ApplyBatteryStatusStyle(LvglTheme*);
    bool SetTextFont(std::shared_ptr<LvglFont>);
};
struct LcdDisplay : LvglDisplay {
    int width_ = 360, height_ = 360;
    bool setup_ui_called_ = true, emotion_uses_image_ = true;
    lv_display_t device;
    lv_display_t* display_ = &device;
    lv_obj_t* network_menu_ = nullptr;
    std::vector<lv_obj_t*> network_menu_rows_;
    std::atomic<uint32_t> network_menu_generation_{0};
    lv_obj_t* container_ = lv_obj_create(&screen_layer);
    lv_obj_t* top_bar_ = lv_obj_create(container_);
    lv_obj_t* bottom_bar_ = lv_obj_create(container_);
    lv_obj_t* chat_message_label_ = lv_obj_create(bottom_bar_);
    lv_obj_t* status_bar_ = lv_obj_create(container_);
    lv_obj_t* home_media_box_ = lv_obj_create(container_);
    lv_obj_t* emoji_box_ = lv_obj_create(home_media_box_);
    lv_obj_t* emoji_image_ = lv_obj_create(emoji_box_);
    lv_obj_t* preview_image_ = lv_obj_create(home_media_box_);
    lv_obj_t* emoji_label_ = lv_obj_create(emoji_box_);
    void* preview_timer_ = this;
    std::unique_ptr<LvglGif> gif_controller_;
    std::unique_ptr<LvglImage> preview_image_cached_;
    LcdDisplay() {
        home_clock_label_ = lv_label_create(container_);
        mute_label_ = lv_label_create(lv_obj_create(top_bar_));
        network_label_ = lv_label_create(top_bar_);
        battery_label_ = lv_label_create(lv_obj_create(top_bar_));
        volume_label_ = lv_label_create(lv_obj_create(top_bar_));
        status_label_ = lv_label_create(status_bar_);
        notification_label_ = lv_label_create(status_bar_);
    }
    void ClearTextGlyphs() { glyphs_cleared = true; }
    void SetTheme(Theme*) override;
    void UpdateHomeLayout(LvglTheme*);
    bool PrepareForAssetUpdate();
    void ShowNetworkMenu(const std::vector<std::string>&, int);
    int NetworkMenuHitTest(int, int);
};
'''


def display_production_source():
    """Keep font/layout policy real; adapt only the LVGL and firmware boundaries."""
    lcd = (ROOT / "main/display/lcd_display.cc").read_text()
    common = (ROOT / "main/display/lvgl_display/lvgl_display.cc").read_text()
    return (DISPLAY_DRIVER
            + definition(lcd, "void LcdDisplay::UpdateHomeLayout(")
            + definition(lcd, "void LcdDisplay::SetTheme(")
            + definition(common, "void LvglDisplay::ApplyBatteryStatusStyle(")
            + definition(common, "bool LvglDisplay::SetTextFont("))
