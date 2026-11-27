"""Exercise the production HOME low-battery policy with synthetic inputs."""

from pathlib import Path
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
BOARD = ROOT / "main/boards/waveshare/esp32-s3-touch-lcd-1.85c"
COMMON = ROOT / "main/boards/common"

POLICY_TEST = r'''
#include <cassert>
#include "home_battery_filter.h"

constexpr HomeBatteryCalibration kMeasuredExample{
    true, 3600, 3900, 20, 3501, 3700, 3000, 4000};

void state(HomeLowBatteryFilter& filter, int64_t now, BatteryState expected) {
    assert(filter.GetState(now) == expected);
}

int main() {
    // These are synthetic test values only; they are not device calibration.
    HomeLowBatteryFilter uncalibrated;
    assert(!uncalibrated.IsEnabled());
    state(uncalibrated, 0, BatteryState::Unclassified);
    HomeLowBatteryFilter flag_off(kMeasuredExample, false);
    assert(!flag_off.IsEnabled());
    flag_off.Observe(3400, 0);
    state(flag_off, 0, BatteryState::Unclassified);

    auto invalid = kMeasuredExample;
    invalid.low_mv = invalid.recovery_mv;
    HomeLowBatteryFilter rejected(invalid, true);
    assert(!rejected.IsEnabled());
    rejected.Observe(3400, 0);
    state(rejected, 0, BatteryState::Unclassified);
    invalid = kMeasuredExample;
    invalid.two_to_three_mv = 4401;
    assert(!invalid.IsValid());
    invalid = kMeasuredExample;
    invalid.two_to_three_mv = invalid.one_to_two_mv;
    assert(!invalid.IsValid());
    invalid = kMeasuredExample;
    invalid.enter_low_ms = 0;
    assert(!invalid.IsValid());

    HomeLowBatteryFilter filter(kMeasuredExample, true);
    filter.Observe(3800, 0);
    state(filter, 0, BatteryState::Normal);
    filter.Observe(3400, 1000000);
    state(filter, 1000000, BatteryState::Normal); // transient dip
    filter.Observe(3800, 2000000);
    filter.Observe(3400, 3000000);
    filter.Observe(3400, 4000000);
    filter.Observe(3400, 5000000);
    state(filter, 5999999, BatteryState::Normal);
    state(filter, 5999999, BatteryState::Normal); // reads do not advance dwell
    filter.Invalidate(); // unknown during entry clears dwell
    filter.Observe(3400, 6000000);
    filter.Observe(3400, 7000000);
    filter.Observe(3400, 8000000);
    state(filter, 8999999, BatteryState::Normal);
    filter.Observe(3400, 9000000);
    state(filter, 9000000, BatteryState::Low); // exact restarted entry boundary

    // The recovery gray zone keeps the confirmed-low state latched.
    filter.Observe(3600, 10000000);
    state(filter, 10000000, BatteryState::Low);
    filter.Observe(3700, 11000000);
    filter.Observe(3700, 12000000);
    filter.Observe(3700, 13000000);
    filter.Observe(3700, 14000000);
    state(filter, 14999999, BatteryState::Low);
    filter.Observe(3700, 15000000);
    state(filter, 15000000, BatteryState::Normal); // exact recovery boundary

    // Unknown interrupts dwell but preserves a previously confirmed low latch.
    filter.Observe(3400, 16000000);
    filter.Observe(3400, 17000000);
    filter.Observe(3400, 18000000);
    filter.Observe(3400, 19000000);
    assert(filter.GetState(19000000) == BatteryState::Low);
    filter.Invalidate();
    filter.Observe(3800, 20000000);
    state(filter, 20000000, BatteryState::Low);
    filter.Observe(3800, 21000000);
    filter.Observe(3800, 22000000);
    filter.Invalidate(); // interrupted recovery must restart its dwell
    filter.Observe(3800, 23000000);
    filter.Observe(3800, 24000000);
    filter.Observe(3800, 25000000);
    filter.Observe(3800, 26000000);
    state(filter, 26999999, BatteryState::Low);
    filter.Observe(3800, 27000000);
    state(filter, 27000000, BatteryState::Normal);

    // A clock rollback and an overlong sample gap cannot complete a dwell.
    filter.Observe(3400, 28000000);
    filter.Observe(3400, 29000000);
    filter.Observe(3400, 30000000);
    filter.Observe(3400, 1000000);
    state(filter, 1000000, BatteryState::Normal);
    filter.Observe(3400, 5000000);
    filter.Observe(3400, 9000000);
    state(filter, 9000000, BatteryState::Normal);

    HomeLowBatteryFilter strict_low(kMeasuredExample, true);
    for (int64_t now = 0; now <= 9000000; now += 1000000) {
        strict_low.Observe(3501, now);
        state(strict_low, now, BatteryState::Normal); // equality is not below low
    }
    strict_low.Observe(3500, 10000000);
    strict_low.Observe(3500, 11000000);
    strict_low.Observe(3501, 12000000); // equality interrupts the in-progress dwell
    state(strict_low, 12000000, BatteryState::Normal);
    strict_low.Observe(3500, 13000000);
    strict_low.Observe(3500, 14000000);
    strict_low.Observe(3500, 15000000);
    state(strict_low, 15999999, BatteryState::Normal);
    strict_low.Observe(3500, 16000000);
    state(strict_low, 16000000, BatteryState::Low); // full dwell after equality reset

    auto band_calibration = kMeasuredExample;
    band_calibration.two_to_three_mv = 4100;
    HomeBatteryFilter bands(band_calibration, true);
    for (int i = 0; i < 5; ++i) bands.AddSample(4000, i * 1000000LL);
    auto snapshot = bands.GetStatus(4000000);
    assert(snapshot.state == BatteryState::Normal);
    assert(snapshot.band == BatteryBand::TwoBars); // configured band, not temporary 3 bars
}
'''


class HomeLowBatteryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not compiler or not shutil.which(compiler[0]):
            raise unittest.SkipTest("A host C++ compiler is required")
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        build = Path(cls.temp.name)
        source = build / "policy.cc"
        source.write_text(POLICY_TEST, encoding="utf-8")
        cls.binary = build / "policy"
        result = subprocess.run(
            compiler + ["-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(BOARD),
                        "-I", str(COMMON), str(source), "-o", str(cls.binary)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def test_policy_boundaries_and_hysteresis(self):
        result = subprocess.run([str(self.binary)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
