"""Exercise real audio volume methods with NVS and codec-device boundary adapters."""

from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from test_checked_settings import NVS_PLATFORM, ROOT, compiler_command, settings_source, without_includes
from test_audio_capture_pause import method


AUDIO_PLATFORM = r'''
#include <functional>
#include <mutex>
#include <cstdlib>
#define ESP_IDF_VERSION 60000
#define ESP_IDF_VERSION_VAL(a, b, c) ((a) * 10000 + (b) * 100 + (c))
#define ESP_CODEC_DEV_OK 0
#define ESP_CODEC_DEV_NOT_SUPPORT ESP_ERR_NOT_SUPPORTED
#define ESP_CODEC_DEV_INVALID_ARG ESP_ERR_INVALID_ARG
using i2s_chan_handle_t = void*;
'''

CODEC_PLATFORM = r'''
using audio_codec_data_if_t = int;
using audio_codec_ctrl_if_t = int;
struct audio_codec_if_t {
    int (*set_vol)(const audio_codec_if_t*, float);
    int (*enable)(const audio_codec_if_t*, bool);
};
using audio_codec_gpio_if_t = int;
using esp_codec_dev_handle_t = void*;
using gpio_num_t = int;
struct esp_codec_dev_sample_info_t {
    int bits_per_sample, channel, channel_mask;
    uint32_t sample_rate;
    int mclk_multiple;
};
#define ESP_CODEC_DEV_MAKE_CHANNEL_MASK(n) (1 << (n))
std::deque<int> volume_results, driver_results, output_open_results, output_close_results, write_results;
int checked_writes = 0;
int hardware_volume = -1, volume_calls = 0, output_opens = 0;
bool hardware_open = false;
#if NATIVE_CLOSE
#define ESP_LOGD(...) ((void)0)
#define ESP_CODEC_DEV_WRONG_STATE ESP_ERR_INVALID_STATE
#define ESP_CODEC_DEV_WRITE_FAIL 87
#define ESP_CODEC_DEV_WORK_MODE_DAC 1
#define ESP_CODEC_DEV_WORK_MODE_BOTH 3
#define CODEC_REF_STAGE_ENABLE 1
#define ES_PA_ENABLE true
#define ES_PA_DISABLE false
struct audio_codec_ctrl_info_t { int identity; };
struct NativeControl { int (*get_info)(const NativeControl*, audio_codec_ctrl_info_t*); };
int native_get_info(const NativeControl*, audio_codec_ctrl_info_t*) { return ESP_OK; }
NativeControl native_control{native_get_info};
struct audio_codec_es8311_t {
    bool is_open = true;
    bool enabled = false;
    struct { int codec_mode; const NativeControl* ctrl_if; } cfg{1, &native_control};
};
audio_codec_es8311_t native_codec;
std::deque<int> native_start_results, native_suspend_results;
int native_refs = 0, native_data_refs = 0, native_data_closes = 0;
bool native_pa_on = false;
int codec_ref_acquire(const audio_codec_ctrl_info_t*, int) { return ++native_refs; }
int codec_ref_release(const audio_codec_ctrl_info_t*, int) {
    if (native_refs > 0) --native_refs;
    return native_refs;
}
int es8311_start(audio_codec_es8311_t*) { return result(native_start_results); }
int es8311_suspend(audio_codec_es8311_t*) { return result(native_suspend_results); }
void es8311_pa_power(audio_codec_es8311_t*, bool on) { native_pa_on = on; }
int es8311_set_mute(const audio_codec_if_t*, bool) { return ESP_OK; }
static int es8311_enable(const audio_codec_if_t*, bool);
#endif
int hardware_set_enabled(const audio_codec_if_t*, bool enabled) {
#if NATIVE_CLOSE
    return es8311_enable(reinterpret_cast<const audio_codec_if_t*>(&native_codec), enabled);
#else
    return ESP_OK;
#endif
}
std::vector<float> driver_db;
int hardware_set_volume(const audio_codec_if_t*, float db) {
    assert(db == -96.0f || (db >= -49.5f && db <= 0.0f));
    hardware_volume = db == -96.0f ? 0 : static_cast<int>((db + 50.0f) * 2.0f);
    driver_db.push_back(db);
    return result(driver_results);
}
audio_codec_if_t hardware_codec{hardware_set_volume, hardware_set_enabled};
struct SoftwareVolume { int (*set_vol)(const SoftwareVolume*, float); };
struct codec_dev_t {
    const audio_codec_if_t* codec_if = &hardware_codec;
    const SoftwareVolume* sw_vol = nullptr;
    esp_codec_dev_vol_curve_t vol_curve{};
    int volume = 0;
};
codec_dev_t hardware_device;
int _verify_codec_setting(codec_dev_t*, bool) {
    ++volume_calls;
    assert(hardware_open);
    return result(volume_results);
}
int esp_codec_dev_open(void*, esp_codec_dev_sample_info_t*) {
    ++output_opens;
    if (hardware_open) return ESP_OK;
    int err = result(output_open_results);
    // The real dependency can leave this set when later initialization fails.
    hardware_open = true;
#if NATIVE_CLOSE
    ++native_data_refs;
    int enable_err = hardware_codec.enable(&hardware_codec, true);
    if (err == ESP_OK) err = enable_err;
#endif
    return err;
}
int esp_codec_dev_close(void*) {
    int err = result(output_close_results);
    if (err == ESP_OK) {
#if NATIVE_CLOSE
        // Match esp_codec_dev_close: discard enable(false)'s result and clear opened.
        // Data-interface release occurs once; the checked bridge must not replay it.
        if (hardware_open) {
            hardware_codec.enable(&hardware_codec, false);
            ++native_data_closes;
            assert(--native_data_refs == 0);
        }
#endif
        hardware_open = false;
    }
    return err;
}
int esp_codec_dev_set_in_channel_gain(void*, int, float) { return ESP_OK; }
int esp_codec_dev_read(void*, void*, int) { return ESP_OK; }
int esp_codec_dev_write(void*, void*, int) { ++checked_writes; return result(write_results); }
'''


def box_source():
    header = without_includes((ROOT / "main/audio/codecs/box_audio_codec.h").read_text(encoding="utf-8"))
    # The constructor/destructor own board I2S/I2C allocation. Bypass only that platform
    # setup, retaining the real class members and all volume/output production methods.
    header = re.sub(r"    BoxAudioCodec\(void\*.*?;",
                    "    BoxAudioCodec() { out_codec_if_ = &hardware_codec; "
                    "output_dev_ = &hardware_device; "
                    "assert(_get_default_vol_curve(&hardware_device.vol_curve) == ESP_OK); }", header,
                    flags=re.S)
    header = header.replace("virtual ~BoxAudioCodec();", "virtual ~BoxAudioCodec() = default;")
    methods = []
    path = "main/audio/codecs/box_audio_codec.cc"
    for signature in ("void BoxAudioCodec::Start(", "void BoxAudioCodec::SetOutputVolume(",
                      "OutputVolumeStatus BoxAudioCodec::SetOutputVolumeChecked(",
                      "esp_err_t BoxAudioCodec::ApplyOutputVolumeChecked(",
                      "OutputVolumeStatus BoxAudioCodec::GetOutputVolumeStatus(",
                      "void BoxAudioCodec::EnableInput(", "void BoxAudioCodec::EnableOutput(",
                      "int BoxAudioCodec::Read(", "int BoxAudioCodec::Write(",
                      "bool BoxAudioCodec::SupportsCheckedOutput(",
                      "bool BoxAudioCodec::OutputDataChecked("):
        methods.append(method(path, signature))
    if "esp_err_t BoxAudioCodec::CloseOutputChecked(" in (ROOT / path).read_text(encoding="utf-8"):
        methods.append(method(path, "esp_err_t BoxAudioCodec::CloseOutputChecked("))
    return header + "\n".join(methods)


def vendor_volume_source():
    # Use the installed dependency's real default-curve allocation/interpolation;
    # comparing two copies of the bridge formula would not detect mapping drift.
    vendor = "managed_components/espressif__esp_codec_dev/"
    header = without_includes((ROOT / vendor / "include/esp_codec_dev_vol.h").read_text(encoding="utf-8"))
    return (header + method(vendor + "esp_codec_dev.c", "static int _get_default_vol_curve(") +
            method(vendor + "esp_codec_dev.c", "static float _get_vol_db("))


def audio_source():
    return "\n".join(without_includes((ROOT / path).read_text(encoding="utf-8"))
                     for path in ("main/audio/audio_codec.h", "main/audio/audio_codec.cc"))


class HomeVolumeRestoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        build = Path(cls.temp.name)
        checks = r'''
class HostCodec : public AudioCodec {
    int Read(int16_t*, int n) override { return n; }
    int Write(const int16_t*, int n) override { return n; }
};
int main() {
    saved["output_volume"] = 0;
    HostCodec codec;
    codec.Start();
#if CONFIG_WAVESHARE_1_85C_HOME
    assert(codec.output_volume() == 0);
    for (int invalid : {-1, 101}) {
        saved["output_volume"] = invalid;
        codec.Start();
        assert(codec.output_volume() == 10);
    }
#else
    assert(codec.output_volume() == 10);
#endif
}
'''
        source = build / "restore.cc"
        source.write_text(NVS_PLATFORM + AUDIO_PLATFORM + settings_source() + audio_source() + checks,
                          encoding="utf-8")
        cls.binaries = []
        for home in (0, 1):
            binary = build / f"restore-{home}"
            compiled = subprocess.run(compiler_command() + ["-std=c++17",
                                      f"-DCONFIG_WAVESHARE_1_85C_HOME={home}", str(source),
                                      "-o", str(binary)], capture_output=True, text=True, timeout=30)
            if compiled.returncode:
                raise AssertionError(compiled.stdout + compiled.stderr)
            cls.binaries.append(binary)

    def test_home_restores_zero_and_rejects_historical_invalid_values(self):
        result = subprocess.run([str(self.binaries[1])], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_non_home_retains_legacy_zero_policy(self):
        result = subprocess.run([str(self.binaries[0])], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class HomeCheckedVolumeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        build = Path(cls.temp.name)
        checks = r'''
void status(BoxAudioCodec& codec, int volume, bool persisted, bool applied, int error) {
    auto state = codec.GetOutputVolumeStatus();
    assert(state.volume == volume && state.persisted == persisted);
    assert(state.applied == applied && state.error == error);
    assert(codec.output_volume() == volume);
    if (applied) assert(hardware_device.volume == volume && hardware_volume == volume);
}
int main(int argc, char** argv) {
    assert(argc == 2);
    std::string scenario = argv[1];
    saved["output_volume"] = 37;
    if (scenario == "missing") saved.clear();
    if (scenario == "historical-invalid") saved["output_volume"] = 101;
    BoxAudioCodec codec;
    codec.Start();
#if !CONFIG_WAVESHARE_1_85C_HOME
    auto unsupported = codec.SetOutputVolumeChecked(9);
    assert(unsupported.error == ESP_ERR_NOT_SUPPORTED);
    assert(saved.at("output_volume") == 37 && volume_calls == 0);
    // Preserve the old setter's immediate hardware/persistence contract.
    hardware_open = true;
    codec.SetOutputVolume(29);
    assert(saved.at("output_volume") == 29 && hardware_volume == 29);
    return 0;
#endif
    if (scenario == "missing") {
        status(codec, 70, false, false, ESP_OK);
        return 0;
    }
    if (scenario == "historical-invalid") {
        status(codec, 10, false, false, ESP_OK);
        return 0;
    }
    status(codec, 37, true, false, ESP_OK);
    if (scenario == "invalid") {
        for (int invalid : {-1, 101}) {
            auto state = codec.SetOutputVolumeChecked(invalid);
            assert(state.error == ESP_ERR_INVALID_ARG && state.volume == 37);
            assert(state.persisted && !state.applied);
        }
        assert(sets == 0 && volume_calls == 0);
        return 0;
    }
    if (scenario == "deferred-zero") {
        auto state = codec.SetOutputVolumeChecked(0);
        assert(state.error == ESP_OK && state.persisted && !state.applied && state.volume == 0);
        assert(volume_calls == 0 && saved.at("output_volume") == 0);
        codec.EnableOutput(true);
        status(codec, 0, true, true, ESP_OK);
        assert(hardware_volume == 0 && codec.output_enabled());
        return 0;
    }
    if (scenario == "last-value") {
        for (int value : {0, 100, 41}) {
            assert(codec.SetOutputVolumeChecked(value).error == ESP_OK);
        }
        assert(saved.at("output_volume") == 41);
        codec.EnableOutput(true);
        status(codec, 41, true, true, ESP_OK);
        assert(hardware_volume == 41);
        return 0;
    }
    if (scenario == "deferred-save-failure") {
        set_results = {91, ESP_OK};
        assert(codec.SetOutputVolumeChecked(0).error == 91);
        status(codec, 37, true, false, 91);
        assert(saved.at("output_volume") == 37 && volume_calls == 0);
        return 0;
    }
    if (scenario == "open-failure" || scenario == "open-cleanup-failure") {
        output_open_results = {83};
        if (scenario == "open-cleanup-failure") output_close_results = {84, 85, ESP_OK};
        codec.EnableOutput(true);
        status(codec, 37, true, false, 83);
        assert(!codec.output_enabled() && volume_calls == 0);
        if (scenario == "open-cleanup-failure") {
            codec.EnableOutput(true);
            status(codec, 37, true, false, 85);
            assert(!codec.output_enabled() && volume_calls == 0);
        }
        codec.EnableOutput(true);
        status(codec, 37, true, true, ESP_OK);
        return 0;
    }
    if (scenario == "enable-volume-failure" || scenario == "enable-close-failure" ||
        scenario == "enable-driver-failure") {
        if (scenario == "enable-driver-failure") driver_results = {80, 81};
        else volume_results = {81};
        if (scenario == "enable-close-failure") output_close_results = {82};
        codec.EnableOutput(true);
        status(codec, 37, true, false, 81);
        assert(!codec.output_enabled());
        codec.EnableOutput(true);
        status(codec, 37, true, true, ESP_OK);
        assert(hardware_volume == 37);
        return 0;
    }
    codec.EnableOutput(true);
    status(codec, 37, true, true, ESP_OK);
    if (scenario == "curve") {
        for (int value : {0, 1, 2, 10, 37, 70, 99, 100}) {
            driver_db.clear();
            assert(codec.SetOutputVolumeChecked(value).error == ESP_OK);
            assert(driver_db.size() == 2 && driver_db[0] == driver_db[1]);
            assert(hardware_volume == value);
        }
    } else if (scenario == "success" || scenario == "void-compatibility") {
        if (scenario == "success") {
            auto state = codec.SetOutputVolumeChecked(0);
            assert(state.error == ESP_OK && state.persisted && state.applied);
        } else {
            codec.SetOutputVolume(0);
        }
        status(codec, 0, true, true, ESP_OK);
        assert(saved.at("output_volume") == 0 && hardware_volume == 0);
    } else if (scenario == "driver-failure") {
        driver_results = {80, 81, ESP_OK, ESP_OK};
        auto state = codec.SetOutputVolumeChecked(0);
        assert(state.error == 81);
        status(codec, 37, true, true, 81);
        assert(saved.at("output_volume") == 37 && hardware_volume == 37 && sets == 0);
    } else if (scenario == "hardware-failure" || scenario == "hardware-rollback-failure") {
        volume_results = {81, scenario == "hardware-failure" ? ESP_OK : 82};
        auto state = codec.SetOutputVolumeChecked(0);
        assert(state.error == 81);
        status(codec, 37, true, scenario == "hardware-failure", 81);
        assert(saved.at("output_volume") == 37 && sets == 0);
    } else if (scenario == "set-failure" || scenario == "commit-failure" ||
               scenario == "save-rollback-failure" || scenario == "rollback-hardware-failure") {
        if (scenario == "set-failure") {
            set_results = {91, ESP_OK};
        } else {
            commit_results = {92, ESP_OK};
            commit_before_failure = true;
        }
        if (scenario == "save-rollback-failure") set_results = {ESP_OK, 91};
        if (scenario == "rollback-hardware-failure") volume_results = {ESP_OK, 81};
        auto state = codec.SetOutputVolumeChecked(0);
        int expected_error = scenario == "set-failure" ? 91 : 92;
        assert(state.error == expected_error);
        status(codec, 37, scenario != "save-rollback-failure",
               scenario != "rollback-hardware-failure", expected_error);
        assert(saved.at("output_volume") == (scenario == "save-rollback-failure" ? 0 : 37));
        // A subsequent success must heal flags and clear the previous error.
        assert(codec.SetOutputVolumeChecked(24).error == ESP_OK);
        status(codec, 24, true, true, ESP_OK);
        assert(saved.at("output_volume") == 24 && hardware_volume == 24);
    } else if (scenario == "disable") {
        codec.EnableOutput(false);
        status(codec, 37, true, false, ESP_OK);
        assert(!codec.output_enabled() && !hardware_open);
    } else if (scenario == "disable-failure") {
        output_close_results = {84};
        codec.EnableOutput(false);
        status(codec, 37, true, false, 84);
        assert(!codec.output_enabled());
        codec.EnableOutput(false);
        status(codec, 37, true, false, ESP_OK);
        assert(!codec.output_enabled());
    } else {
        assert(false);
    }
}
'''
        source = build / "checked_volume.cc"
        vendor_set = method("managed_components/espressif__esp_codec_dev/esp_codec_dev.c",
                            "int esp_codec_dev_set_out_vol(")
        source.write_text("\n".join((NVS_PLATFORM, AUDIO_PLATFORM, vendor_volume_source(),
                                     CODEC_PLATFORM, vendor_set, settings_source(),
                                     audio_source(), box_source(), checks)), encoding="utf-8")
        cls.binaries = {}
        for home in (0, 1):
            binary = build / f"checked-{home}"
            compiled = subprocess.run(compiler_command() + ["-std=c++17", "-pthread",
                                      f"-DCONFIG_WAVESHARE_1_85C_HOME={home}", str(source),
                                      "-o", str(binary)], capture_output=True, text=True, timeout=30)
            if compiled.returncode:
                raise AssertionError(compiled.stdout + compiled.stderr)
            cls.binaries[home] = binary

    def test_home_output_volume_transactions(self):
        for scenario in ("missing", "historical-invalid", "invalid", "deferred-zero", "last-value",
                         "deferred-save-failure", "open-failure", "open-cleanup-failure",
                         "enable-volume-failure", "enable-driver-failure", "enable-close-failure",
                         "curve", "success", "void-compatibility", "driver-failure", "hardware-failure",
                         "hardware-rollback-failure", "set-failure", "commit-failure",
                         "save-rollback-failure", "rollback-hardware-failure", "disable", "disable-failure"):
            with self.subTest(scenario=scenario):
                result = subprocess.run([str(self.binaries[1]), scenario], capture_output=True,
                                        text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_non_home_checked_volume_is_unsupported_and_legacy_still_works(self):
        result = subprocess.run([str(self.binaries[0]), "legacy"], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class HomeNativeCloseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        build = Path(cls.temp.name)
        checks = r'''
void applied(BoxAudioCodec& codec, bool value, int error) {
    auto status = codec.GetOutputVolumeStatus();
    assert(status.applied == value && status.error == error);
    if (value) assert(hardware_open && native_codec.enabled && native_pa_on);
}
int main(int argc, char** argv) {
    assert(argc == 2);
    std::string scenario = argv[1];
    saved["output_volume"] = 37;
    BoxAudioCodec codec;
    codec.Start();
    if (scenario == "partial-start") {
        native_start_results = {86};
        codec.EnableOutput(true);
        applied(codec, false, 86);
        assert(!native_codec.enabled && native_pa_on && !hardware_open);
        // disable(false) is a no-op after a failed start; never infer that PA is off.
        codec.EnableOutput(true);
        applied(codec, true, ESP_OK);
        return 0;
    }
    if (scenario == "open-cleanup") {
        output_open_results = {83};
        native_suspend_results = {87, 88, 89, ESP_OK};
        codec.EnableOutput(true);
        applied(codec, false, 83);
    } else if (scenario == "volume-cleanup") {
        volume_results = {81};
        native_suspend_results = {87, 88, 89, ESP_OK};
        codec.EnableOutput(true);
        applied(codec, false, 81);
    } else {
        codec.EnableOutput(true);
        applied(codec, true, ESP_OK);
        native_suspend_results = scenario == "transient" ? std::deque<int>{87} :
                                  std::deque<int>{87, 88, 89, ESP_OK};
        codec.EnableOutput(false);
        if (scenario == "transient") {
            // Regression: native close returns success with enabled=true/PA=false.
            // The next native open then skips enable(true), leaving playback silent.
            codec.EnableOutput(true);
            applied(codec, true, ESP_OK);
            assert(native_data_closes == 1 && native_data_refs == 1 && native_refs == 1);
            return 0;
        }
        applied(codec, false, 88);
        if (scenario == "persistent") {
            auto pending = codec.SetOutputVolumeChecked(44);
            assert(pending.persisted && !pending.applied && pending.error == 88);
        }
    }
    assert(!codec.output_enabled() && !hardware_open && native_codec.enabled && !native_pa_on);
    assert(native_data_closes == 1 && native_data_refs == 0 && native_refs == 0);
    codec.EnableOutput(true);
    applied(codec, false, 89);
    assert(!codec.output_enabled() && !hardware_open && !native_pa_on);
    // Native opened is already false. Retry only the codec disable, then reopen.
    codec.EnableOutput(true);
    applied(codec, true, ESP_OK);
    assert(native_data_closes == 1 && native_data_refs == 1 && native_refs == 1);
}
'''
        vendor = "managed_components/espressif__esp_codec_dev/"
        vendor_set = method(vendor + "esp_codec_dev.c", "int esp_codec_dev_set_out_vol(")
        vendor_enable = method(vendor + "device/es8311/es8311.c", "static int es8311_enable(")
        source = build / "native_close.cc"
        source.write_text("\n".join((NVS_PLATFORM, AUDIO_PLATFORM, vendor_volume_source(),
                                     CODEC_PLATFORM, vendor_enable, vendor_set, settings_source(),
                                     audio_source(), box_source(), checks)), encoding="utf-8")
        cls.binary = build / "native_close"
        compiled = subprocess.run(compiler_command() + ["-std=c++17", "-pthread",
                                  "-DCONFIG_WAVESHARE_1_85C_HOME=1", "-DNATIVE_CLOSE=1",
                                  str(source), "-o", str(cls.binary)],
                                  capture_output=True, text=True, timeout=30)
        if compiled.returncode:
            raise AssertionError(compiled.stdout + compiled.stderr)

    def test_native_close_suspend_failure_recovers_pa_and_preserves_errors(self):
        for scenario in ("transient", "persistent", "open-cleanup", "volume-cleanup", "partial-start"):
            with self.subTest(scenario=scenario):
                result = subprocess.run([str(self.binary), scenario], capture_output=True,
                                        text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
