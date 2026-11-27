"""Compile the production HOME battery filter and sampler at system boundaries."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
BOARD = ROOT / "main/boards/waveshare/esp32-s3-touch-lcd-1.85c"
COMMON = ROOT / "main/boards/common"

# The IDF ADC, clock and FreeRTOS scheduler are the only test doubles. The
# production sampler .cc and production filter are compiled without alteration.
FAKE_IDF = r'''
#pragma once
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

using esp_err_t = int;
constexpr int ESP_OK = 0;
constexpr int ESP_FAIL = -1;
inline const char* esp_err_to_name(int error) { return error ? "failure" : "ESP_OK"; }
enum gpio_num_t { GPIO_NUM_8 = 8, GPIO_NUM_9 = 9 };
enum adc_unit_t { ADC_UNIT_1, ADC_UNIT_2 };
enum adc_channel_t { ADC_CHANNEL_6 = 6, ADC_CHANNEL_7 = 7 };
enum adc_atten_t { ADC_ATTEN_DB_12 };
enum adc_bitwidth_t { ADC_BITWIDTH_DEFAULT };
enum adc_ulp_mode_t { ADC_ULP_MODE_DISABLE };
struct FakeAdc { bool deleted = false; };
struct FakeCalibration { bool deleted = false; };
using adc_oneshot_unit_handle_t = FakeAdc*;
using adc_cali_handle_t = FakeCalibration*;
struct adc_oneshot_unit_init_cfg_t { adc_unit_t unit_id; int clk_src; adc_ulp_mode_t ulp_mode; };
struct adc_oneshot_chan_cfg_t { adc_atten_t atten; adc_bitwidth_t bitwidth; };
struct adc_cali_curve_fitting_config_t {
    adc_unit_t unit_id;
    adc_channel_t chan;
    adc_atten_t atten;
    adc_bitwidth_t bitwidth;
};
struct FakeTask {
    std::thread worker;
    unsigned notifications = 0;
    bool returned = false;
};
struct FakeSemaphore {
    std::mutex lock;
    std::condition_variable changed;
    bool given = false;
};
using TaskHandle_t = FakeTask*;
using SemaphoreHandle_t = FakeSemaphore*;
using portMUX_TYPE = std::mutex;
#define portMUX_INITIALIZER_UNLOCKED {}
#define portENTER_CRITICAL(mux) ((mux)->lock())
#define portEXIT_CRITICAL(mux) ((mux)->unlock())
constexpr unsigned portMAX_DELAY = ~0U;
constexpr int pdTRUE = 1;
constexpr int pdPASS = 1;
constexpr int tskIDLE_PRIORITY = 0;
#define pdMS_TO_TICKS(ms) (ms)

namespace Fake {
inline std::atomic<int64_t> now{0};
inline std::atomic<int> stage{0}, read_error{0}, conversion_error{0}, pin_mv{1267};
inline std::atomic<int> adc_open{0}, calibration_open{0}, semaphore_open{0}, conversions{0};
inline std::vector<std::unique_ptr<FakeAdc>> adcs;
inline std::vector<std::unique_ptr<FakeCalibration>> calibrations;
inline std::vector<std::unique_ptr<FakeSemaphore>> semaphores;
inline std::vector<std::unique_ptr<FakeTask>> tasks;
inline thread_local FakeTask* current = nullptr;
inline FakeTask* active = nullptr;
inline std::mutex control, log_lock;
inline std::condition_variable changed;
inline size_t completed_samples = 0;
inline bool block_read = false, read_entered = false;
inline std::vector<std::string> info, warnings;

inline void Log(bool warning, const char* format, ...) {
    char message[256];
    va_list args;
    va_start(args, format);
    vsnprintf(message, sizeof(message), format, args);
    va_end(args);
    std::lock_guard<std::mutex> lock(log_lock);
    (warning ? warnings : info).emplace_back(message);
}

inline void Wake(FakeTask* task) {
    std::lock_guard<std::mutex> lock(control);
    ++task->notifications;
    changed.notify_all();
}

inline void WaitSamples(size_t expected) {
    std::unique_lock<std::mutex> lock(control);
    assert(changed.wait_for(lock, std::chrono::seconds(2), [=] {
        return completed_samples >= expected;
    }));
}

inline void Step(int mv = 1267, int read_failure = 0, int conversion_failure = 0) {
    pin_mv = mv;
    read_error = read_failure;
    conversion_error = conversion_failure;
    now += 1000000;
    size_t expected;
    {
        std::lock_guard<std::mutex> lock(control);
        expected = completed_samples + 1;
    }
    Wake(active);
    WaitSamples(expected);
}

inline void Join() {
    for (auto& task : tasks) {
        if (task->worker.joinable()) task->worker.join();
        assert(task->returned);
    }
    assert(adc_open == 0 && calibration_open == 0 && semaphore_open == 0);
}
}  // namespace Fake

#define ESP_LOGI(tag, ...) ((void)(tag), Fake::Log(false, __VA_ARGS__))
#define ESP_LOGW(tag, ...) ((void)(tag), Fake::Log(true, __VA_ARGS__))
inline int64_t esp_timer_get_time() { return Fake::now.load(); }

inline int adc_oneshot_io_to_channel(int, adc_unit_t* unit, adc_channel_t* channel) {
    if (Fake::stage == 1) return ESP_FAIL;
    *unit = Fake::stage == 2 ? ADC_UNIT_2 : ADC_UNIT_1;
    *channel = Fake::stage == 3 ? ADC_CHANNEL_6 : ADC_CHANNEL_7;
    return ESP_OK;
}
inline int adc_oneshot_new_unit(const adc_oneshot_unit_init_cfg_t* config,
                                adc_oneshot_unit_handle_t* handle) {
    assert(config->unit_id == ADC_UNIT_1 && config->ulp_mode == ADC_ULP_MODE_DISABLE);
    if (Fake::stage == 4) return ESP_FAIL;
    Fake::adcs.emplace_back(std::make_unique<FakeAdc>());
    *handle = Fake::adcs.back().get();
    ++Fake::adc_open;
    return ESP_OK;
}
inline int adc_oneshot_config_channel(adc_oneshot_unit_handle_t handle, adc_channel_t channel,
                                      const adc_oneshot_chan_cfg_t* config) {
    assert(handle && !handle->deleted && channel == ADC_CHANNEL_7);
    assert(config->atten == ADC_ATTEN_DB_12 && config->bitwidth == ADC_BITWIDTH_DEFAULT);
    return Fake::stage == 5 ? ESP_FAIL : ESP_OK;
}
inline int adc_cali_create_scheme_curve_fitting(const adc_cali_curve_fitting_config_t* config,
                                               adc_cali_handle_t* handle) {
    assert(config->unit_id == ADC_UNIT_1 && config->chan == ADC_CHANNEL_7);
    assert(config->atten == ADC_ATTEN_DB_12 && config->bitwidth == ADC_BITWIDTH_DEFAULT);
    if (Fake::stage == 6) return ESP_FAIL;
    Fake::calibrations.emplace_back(std::make_unique<FakeCalibration>());
    *handle = Fake::calibrations.back().get();
    ++Fake::calibration_open;
    return ESP_OK;
}
inline int adc_oneshot_read(adc_oneshot_unit_handle_t handle, adc_channel_t channel, int* raw) {
    std::unique_lock<std::mutex> lock(Fake::control);
    Fake::read_entered = true;
    Fake::changed.notify_all();
    Fake::changed.wait(lock, [] { return !Fake::block_read; });
    assert(handle && !handle->deleted && channel == ADC_CHANNEL_7);
    *raw = 777;
    return Fake::read_error;
}
inline int adc_cali_raw_to_voltage(adc_cali_handle_t handle, int raw, int* voltage) {
    assert(handle && !handle->deleted && raw == 777);
    ++Fake::conversions;
    *voltage = Fake::pin_mv;
    return Fake::conversion_error;
}
inline int adc_cali_delete_scheme_curve_fitting(adc_cali_handle_t handle) {
    assert(handle && !handle->deleted);
    handle->deleted = true;
    --Fake::calibration_open;
    return ESP_OK;
}
inline int adc_oneshot_del_unit(adc_oneshot_unit_handle_t handle) {
    assert(handle && !handle->deleted && Fake::calibration_open == 0);
    handle->deleted = true;
    --Fake::adc_open;
    return ESP_OK;
}
inline SemaphoreHandle_t xSemaphoreCreateBinary() {
    if (Fake::stage == 7) return nullptr;
    Fake::semaphores.emplace_back(std::make_unique<FakeSemaphore>());
    ++Fake::semaphore_open;
    return Fake::semaphores.back().get();
}
inline int xSemaphoreGive(SemaphoreHandle_t semaphore) {
    std::lock_guard<std::mutex> lock(semaphore->lock);
    semaphore->given = true;
    semaphore->changed.notify_all();
    return pdTRUE;
}
inline int xSemaphoreTake(SemaphoreHandle_t semaphore, unsigned timeout) {
    assert(timeout == portMAX_DELAY);
    std::unique_lock<std::mutex> lock(semaphore->lock);
    semaphore->changed.wait(lock, [=] { return semaphore->given; });
    semaphore->given = false;
    return pdTRUE;
}
inline void vSemaphoreDelete(SemaphoreHandle_t) { --Fake::semaphore_open; }
inline int xTaskCreate(void (*fn)(void*), const char* name, uint32_t stack_size, void* context,
                       unsigned priority, TaskHandle_t* handle) {
    assert(std::string(name) == "home_battery" && priority == 1 && stack_size >= 2048);
    if (Fake::stage == 8) return 0;
    Fake::tasks.emplace_back(std::make_unique<FakeTask>());
    auto* task = Fake::tasks.back().get();
    *handle = Fake::active = task;
    task->worker = std::thread([=] {
        Fake::current = task;
        fn(context);
        std::lock_guard<std::mutex> lock(Fake::control);
        task->returned = true;
        Fake::changed.notify_all();
    });
    return pdPASS;
}
inline unsigned ulTaskNotifyTake(int clear, unsigned timeout) {
    assert(clear == pdTRUE && timeout == 1000);
    std::unique_lock<std::mutex> lock(Fake::control);
    ++Fake::completed_samples;
    Fake::changed.notify_all();
    Fake::changed.wait(lock, [] { return Fake::current->notifications > 0; });
    Fake::current->notifications = 0;
    return 1;
}
inline int xTaskNotifyGive(TaskHandle_t handle) { Fake::Wake(handle); return pdPASS; }
inline void vTaskDelete(TaskHandle_t handle) { assert(handle == nullptr); }
'''

SAMPLER_TEST = r'''
#include <climits>
#include "home_battery_status.h"

void unknown(const HomeBatteryStatus& sampler) {
    const auto status = sampler.GetStatus();
    assert(status.supported && status.state == BatteryState::Unknown);
    assert(status.band == BatteryBand::Unknown);
}
void known(const HomeBatteryStatus& sampler) {
    const auto status = sampler.GetStatus();
    assert(status.supported && status.state == BatteryState::Unclassified);
    assert(status.band == BatteryBand::TwoBars);
}
void recover(HomeBatteryStatus& sampler) {
    for (int i = 0; i < 4; ++i) {
        Fake::Step();
        unknown(sampler);
    }
    Fake::Step();
    known(sampler);
}
void start(HomeBatteryStatus& sampler) {
    unknown(sampler);
    assert(sampler.Start(GPIO_NUM_8));
    Fake::WaitSamples(1);
    unknown(sampler);
    for (int i = 0; i < 3; ++i) {
        Fake::Step();
        unknown(sampler);
    }
    Fake::Step();
    known(sampler);
}
int main(int argc, char** argv) {
    assert(argc == 2);
    const std::string scenario = argv[1];
    if (scenario == "initialization-cleanup") {
        {
            HomeBatteryStatus sampler;
            assert(!sampler.Start(GPIO_NUM_9));
            unknown(sampler);
        }
        for (int stage = 1; stage <= 8; ++stage) {
            Fake::stage = stage;
            {
                HomeBatteryStatus sampler;
                assert(!sampler.Start(GPIO_NUM_8));
                unknown(sampler);
                assert(Fake::adc_open == 0 && Fake::calibration_open == 0);
                assert(Fake::semaphore_open == 0);
            }
        }
        Fake::stage = 5;
        {
            HomeBatteryStatus sampler;
            assert(!sampler.Start(GPIO_NUM_8));
            Fake::stage = 0;
            start(sampler);  // A failed setup leaves no handles preventing retry.
            assert(!sampler.Start(GPIO_NUM_8));
            known(sampler);  // Repeated Start must not reset an active sampler.
        }
    } else if (scenario == "sampling-recovery") {
        {
            HomeBatteryStatus sampler;
            start(sampler);
            assert(Fake::info.size() == 1);
            assert(Fake::info[0].find("median=3801 mV") != std::string::npos);
            const auto converted = Fake::conversions.load();
            Fake::Step(1267, ESP_FAIL);
            unknown(sampler);
            assert(Fake::conversions == converted);  // Never calibrate a failed read.
            const auto warned = Fake::warnings.size();
            Fake::Step(1267, ESP_FAIL);
            assert(Fake::warnings.size() == warned);  // Limit repeated failure logs.
            for (int i = 0; i < 4; ++i) {
                Fake::Step();
                unknown(sampler);
            }
            Fake::Step(1267, ESP_OK, ESP_FAIL);  // Interrupt recovery at sample five.
            unknown(sampler);
            recover(sampler);
            assert(Fake::info.back().find("recovered") != std::string::npos);
            for (int invalid : {0, -1, 833, 1467, INT_MAX}) {
                Fake::Step(invalid);
                unknown(sampler);
                recover(sampler);
            }
        }
    } else if (scenario == "stalled-task") {
        {
            HomeBatteryStatus sampler;
            start(sampler);
            Fake::now += 2999999;
            known(sampler);
            Fake::now += 1;
            unknown(sampler);  // The task is stalled; no SampleOnce invalidated it.
            recover(sampler);
            // A second gap is handled on ingestion even without any reader.
            Fake::now += 3000000;
            recover(sampler);
        }
    } else if (scenario == "concurrent-snapshots") {
        {
            HomeBatteryStatus sampler;
            start(sampler);
            std::atomic<bool> stop{false};
            auto reader = [&] {
                while (!stop) {
                    const auto status = sampler.GetStatus();
                    assert(status.supported);
                    assert(status.state == BatteryState::Unknown ||
                           status.state == BatteryState::Unclassified);
                    assert((status.state == BatteryState::Unknown) ==
                           (status.band == BatteryBand::Unknown));
                }
            };
            std::thread ui(reader), mcp(reader);
            for (int i = 0; i < 35; ++i) {
                Fake::Step(1267, i % 7 == 0 ? ESP_FAIL : ESP_OK);
            }
            stop = true;
            ui.join();
            mcp.join();
        }
    } else if (scenario == "calibrated-policy") {
        // Synthetic test parameters only; no value here is a device recommendation.
        constexpr HomeBatteryCalibration calibration{
            true, 3600, 3900, 20, 3500, 3700, 3000, 4000};
        Fake::pin_mv = 1100;  // 3300 mV synthetic battery rail after x3 scaling.
        {
            HomeBatteryStatus sampler(calibration);
            unknown(sampler);
            assert(sampler.Start(GPIO_NUM_8));
            Fake::WaitSamples(1);
            unknown(sampler);
            for (int i = 0; i < 4; ++i) Fake::Step(1100);
            auto status = sampler.GetStatus();
            assert(status.state == BatteryState::Normal);
            assert(status.band == BatteryBand::OneBar);
            Fake::Step(1100);
            Fake::Step(1100);
            for (int i = 0; i < 20; ++i) {
                status = sampler.GetStatus();
                assert(status.state == BatteryState::Normal);
            }
            Fake::Step(1100);
            assert(sampler.GetStatus().state == BatteryState::Low);

            Fake::Step(1100, ESP_FAIL);
            unknown(sampler);  // Fault resets the window and dwell, but keeps low latched.
            for (int i = 0; i < 5; ++i) Fake::Step(1100);
            assert(sampler.GetStatus().state == BatteryState::Low);

            for (int i = 0; i < 5; ++i) Fake::Step(1334);  // 4002 mV synthetic rail.
            assert(sampler.GetStatus().state == BatteryState::Low);
            Fake::Step(1334);
            Fake::Step(1334);
            assert(sampler.GetStatus().state == BatteryState::Normal);
        }
    } else if (scenario == "calibration-disabled") {
        constexpr HomeBatteryCalibration calibration{
            true, 3600, 3900, 20, 3500, 3700, 3000, 4000};
        Fake::pin_mv = 1100;
        {
            HomeBatteryStatus sampler(calibration);
            assert(sampler.Start(GPIO_NUM_8));
            Fake::WaitSamples(1);
            for (int i = 0; i < 4; ++i) Fake::Step(1100);
            const auto status = sampler.GetStatus();
            assert(status.state == BatteryState::Unclassified);
            assert(status.band == BatteryBand::OneBar);  // Temporary band remains available.
        }
    } else if (scenario == "strict-low-threshold") {
        // Synthetic 3501 mV is exactly reachable through the x3 ADC scaling.
        constexpr HomeBatteryCalibration calibration{
            true, 3600, 3900, 20, 3501, 3700, 3000, 4000};
        Fake::pin_mv = 1167;  // 3501 mV synthetic battery rail.
        {
            HomeBatteryStatus sampler(calibration);
            assert(sampler.Start(GPIO_NUM_8));
            Fake::WaitSamples(1);
            for (int i = 0; i < 4; ++i) Fake::Step(1167);
            for (int i = 0; i < 8; ++i) {
                Fake::Step(1167);
                assert(sampler.GetStatus().state == BatteryState::Normal);
            }

            Fake::Step(1166);  // 3498 mV: first lower input, median still at threshold.
            Fake::Step(1166);
            Fake::Step(1166);  // The third lower input starts entry dwell.
            Fake::Step(1167);
            Fake::Step(1167);
            Fake::Step(1167);  // Median reaches equality and must reset dwell.
            assert(sampler.GetStatus().state == BatteryState::Normal);
            Fake::Step(1166);
            Fake::Step(1166);
            Fake::Step(1166);  // Median falls below threshold and starts a fresh dwell.
            Fake::Step(1166);
            Fake::Step(1166);
            assert(sampler.GetStatus().state == BatteryState::Normal);
            Fake::Step(1166);
            assert(sampler.GetStatus().state == BatteryState::Low);
        }
    } else if (scenario == "stop-during-read") {
        Fake::block_read = true;
        auto sampler = std::make_unique<HomeBatteryStatus>();
        assert(sampler->Start(GPIO_NUM_8));
        {
            std::unique_lock<std::mutex> lock(Fake::control);
            assert(Fake::changed.wait_for(lock, std::chrono::seconds(2), [] {
                return Fake::read_entered;
            }));
        }
        std::atomic<bool> destroyed{false};
        std::thread destroyer([&] { sampler.reset(); destroyed = true; });
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
        assert(!destroyed && Fake::adc_open == 1 && Fake::calibration_open == 1);
        {
            std::lock_guard<std::mutex> lock(Fake::control);
            Fake::block_read = false;
            Fake::changed.notify_all();
        }
        destroyer.join();
        assert(destroyed);
    } else {
        assert(false);
    }
    Fake::Join();
}
'''


class HomeBatteryStatusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not cls.compiler or not shutil.which(cls.compiler[0]):
            raise unittest.SkipTest("A host C++ compiler is required")
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.build = Path(cls.temp.name)
        source = cls.build / "battery_filter.cc"
        source.write_text(r'''
#include <cassert>
#include <limits>
#include <string>
#include "home_battery_filter.h"

void unknown(HomeBatteryFilter& filter, int64_t now) {
    const auto status = filter.GetStatus(now);
    assert(status.supported);
    assert(status.state == BatteryState::Unknown);
    assert(status.band == BatteryBand::Unknown);
}

void band(HomeBatteryFilter& filter, int64_t now, BatteryBand expected) {
    const auto status = filter.GetStatus(now);
    assert(status.supported);
    assert(status.state == BatteryState::Unclassified);
    assert(status.band == expected);
}

void fill(HomeBatteryFilter& filter, int mv, int64_t& now) {
    for (int i = 0; i < 5; ++i) {
        filter.AddSample(mv, now);
        now += 1000000;
    }
}

int main(int argc, char** argv) {
    assert(argc == 2);
    std::string scenario = argv[1];
    HomeBatteryFilter filter;
    int64_t now = 0;
    unknown(filter, now);
    if (scenario == "startup-median") {
        const int samples[] = {4400, 3700, 2500, 4000, 3800};
        for (int i = 0; i < 4; ++i) {
            filter.AddSample(samples[i], i * 1000000LL);
            unknown(filter, i * 1000000LL);
        }
        filter.AddSample(samples[4], 4000000);
        band(filter, 4000000, BatteryBand::TwoBars);
        assert(filter.GetMedianMillivolts() == 3800);
        // The median uses a sliding fixed-size window, resisting one outlier.
        filter.AddSample(2500, 5000000);
        band(filter, 5000000, BatteryBand::TwoBars);
        assert(filter.GetMedianMillivolts() == 3700);
    } else if (scenario == "range") {
        for (int invalid : {2499, 4401, -1, std::numeric_limits<int>::max()}) {
            fill(filter, 3800, now);
            filter.AddSample(invalid, now);
            unknown(filter, now++);
            // A valid reading after the fault starts a new five-sample window.
            for (int i = 0; i < 4; ++i) {
                filter.AddSample(3800, now);
                unknown(filter, now);
                now += 1000000;
            }
            filter.AddSample(3800, now);
            band(filter, now, BatteryBand::TwoBars);
            now += 1000000;
        }
        fill(filter, 2500, now);
        band(filter, now, BatteryBand::OneBar);
        fill(filter, 4400, now);
        band(filter, now, BatteryBand::ThreeBars);
    } else if (scenario == "stale") {
        fill(filter, 3800, now);
        const auto last_sample = now - 1000000;
        band(filter, last_sample + 2999999, BatteryBand::TwoBars);
        unknown(filter, last_sample + 3000000);
        now = last_sample + 3000000;
        for (int i = 0; i < 4; ++i) {
            filter.AddSample(3800, now);
            unknown(filter, now);
            now += 1000000;
        }
        filter.AddSample(3800, now);
        band(filter, now, BatteryBand::TwoBars);
        // Expiry must also happen on ingestion when no reader noticed the gap.
        filter.AddSample(3800, now + 3000000);
        unknown(filter, now + 3000000);
    } else if (scenario == "partial-stale") {
        for (int i = 0; i < 4; ++i) {
            filter.AddSample(3800, i * 1000000LL);
        }
        unknown(filter, 6000000);
        filter.AddSample(3800, 6000000);
        unknown(filter, 6000000);
        filter.Invalidate();
        for (int i = 0; i < 4; ++i) {
            filter.AddSample(3800, 7000000 + i * 1000000LL);
            unknown(filter, 7000000 + i * 1000000LL);
        }
        // A backwards clock cannot resurrect a previous window.
        filter.AddSample(3800, 1);
        unknown(filter, 1);
    } else if (scenario == "interrupted-recovery") {
        fill(filter, 3800, now);
        for (int repetition = 0; repetition < 3; ++repetition) {
            filter.Invalidate();
            unknown(filter, now);
            for (int i = 0; i < 4; ++i) {
                filter.AddSample(3800, now);
                unknown(filter, now);
                now += 1000000;
            }
        }
        filter.AddSample(3800, now);
        band(filter, now, BatteryBand::TwoBars);
    } else if (scenario == "initial-boundaries") {
        for (int mv : {3649, 3650, 3949, 3950}) {
            filter.Invalidate();
            fill(filter, mv, now);
            band(filter, now, mv < 3650 ? BatteryBand::OneBar :
                 mv < 3950 ? BatteryBand::TwoBars : BatteryBand::ThreeBars);
        }
    } else if (scenario == "hysteresis") {
        fill(filter, 3600, now);
        fill(filter, 3679, now);
        band(filter, now, BatteryBand::OneBar);
        fill(filter, 3680, now);
        band(filter, now, BatteryBand::TwoBars);
        fill(filter, 3621, now);
        band(filter, now, BatteryBand::TwoBars);
        fill(filter, 3620, now);
        band(filter, now, BatteryBand::OneBar);
        fill(filter, 4000, now);
        band(filter, now, BatteryBand::ThreeBars);
        fill(filter, 3921, now);
        band(filter, now, BatteryBand::ThreeBars);
        fill(filter, 3920, now);
        band(filter, now, BatteryBand::TwoBars);
        fill(filter, 3979, now);
        band(filter, now, BatteryBand::TwoBars);
        fill(filter, 3980, now);
        band(filter, now, BatteryBand::ThreeBars);
        fill(filter, 3500, now);
        band(filter, now, BatteryBand::OneBar);
        // Rebuilding a window also discards the old hysteresis anchor.
        filter.Invalidate();
        fill(filter, 3650, now);
        band(filter, now, BatteryBand::TwoBars);
    } else {
        assert(false);
    }
}
''', encoding="utf-8")
        cls.binary = cls.build / "battery_filter"
        cls.compile(source, cls.binary)
        (cls.build / "fake_idf.h").write_text(FAKE_IDF, encoding="utf-8")
        for header in ("driver/gpio.h", "esp_adc/adc_cali.h", "esp_adc/adc_oneshot.h",
                       "esp_adc/adc_cali_scheme.h", "freertos/FreeRTOS.h",
                       "freertos/task.h", "freertos/semphr.h", "esp_log.h", "esp_timer.h"):
            path = cls.build / header
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_text('#include "fake_idf.h"\n', encoding="utf-8")
        (cls.build / "sdkconfig.h").write_text(
            "#define CONFIG_WAVESHARE_1_85C_HOME 1\n"
            "#define CONFIG_WAVESHARE_1_85C_HOME_LOW_BATTERY_ALERTS 1\n", encoding="utf-8")
        source = cls.build / "battery_sampler.cc"
        source.write_text(SAMPLER_TEST, encoding="utf-8")
        cls.sampler_binary = cls.build / "battery_sampler"
        cls.compile(source, cls.sampler_binary, str(BOARD / "home_battery_status.cc"),
                    "-I", str(cls.build), "-pthread")
        flag_off = cls.build / "flag_off"
        flag_off.mkdir()
        (flag_off / "sdkconfig.h").write_text(
            "#define CONFIG_WAVESHARE_1_85C_HOME 1\n", encoding="utf-8")
        disabled_source = flag_off / "battery_sampler.cc"
        disabled_source.write_text(SAMPLER_TEST, encoding="utf-8")
        cls.flag_off_binary = flag_off / "battery_sampler"
        cls.compile(disabled_source, cls.flag_off_binary,
                    str(BOARD / "home_battery_status.cc"), "-I", str(flag_off),
                    "-I", str(cls.build), "-pthread")

    @classmethod
    def compile(cls, source, binary, *extra):
        result = subprocess.run(
            cls.compiler + ["-std=c++17", "-Wall", "-Wextra", "-I", str(BOARD),
                            "-I", str(COMMON), str(source), "-o", str(binary), *extra],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def test_filter_policy(self):
        for scenario in ("startup-median", "range", "stale", "partial-stale",
                         "interrupted-recovery", "initial-boundaries", "hysteresis"):
            with self.subTest(scenario=scenario):
                self.run_scenario(self.binary, scenario)

    def test_sampler_system_boundaries(self):
        for scenario in ("initialization-cleanup", "sampling-recovery", "stalled-task",
                         "concurrent-snapshots", "calibrated-policy",
                         "strict-low-threshold", "stop-during-read"):
            with self.subTest(scenario=scenario):
                self.run_scenario(self.sampler_binary, scenario)

    def test_sampler_low_battery_compile_flag_defaults_off(self):
        self.run_scenario(self.flag_off_binary, "calibration-disabled")

    def test_non_home_variant_has_no_adc_dependency(self):
        # The ordinary board compiles with no ADC/FreeRTOS headers available.
        ordinary = self.build / "ordinary"
        ordinary.mkdir()
        (ordinary / "sdkconfig.h").write_text("", encoding="utf-8")
        result = subprocess.run(
            self.compiler + ["-std=c++17", "-Werror=undef", "-I", str(ordinary),
                             "-c", str(BOARD / "home_battery_status.cc"),
                             "-o", str(ordinary / "sampler.o")],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def run_scenario(self, binary, scenario):
        result = subprocess.run([str(binary), scenario], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
