"""Compile and exercise the production HOME touch recognizer on the host."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
BOARD = ROOT / "main/boards/waveshare/esp32-s3-touch-lcd-1.85c"


class HomeTouchGestureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not compiler or not shutil.which(compiler[0]):
            raise unittest.SkipTest("A host C++ compiler is required")
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        build = Path(cls.temp.name)
        source = build / "home_touch_gesture.cc"
        source.write_text(r'''
#include <cassert>
#include <string>
#include "home_touch_gesture.h"

bool tap(HomeTouchGesture& gesture, uint64_t down, uint64_t up,
         uint16_t x = 100, uint16_t y = 100) {
    assert(!gesture.Update(down, true, 1, x, y));
    return gesture.Update(up, true, 0);
}

int main(int argc, char** argv) {
    assert(argc == 2);
    std::string scenario = argv[1];
    HomeTouchGesture gesture;
    if (scenario == "double") {
        assert(!tap(gesture, 0, 80));
        assert(tap(gesture, 180, 260, 110, 110));
        assert(!gesture.Update(280, true, 0));
    } else if (scenario == "single") {
        assert(!tap(gesture, 0, 80));
        assert(!gesture.Update(381, true, 0));
    } else if (scenario == "interval") {
        assert(!tap(gesture, 0, 80));
        assert(!tap(gesture, 381, 460));
    } else if (scenario == "interval-boundary") {
        assert(!tap(gesture, 0, 80));
        assert(tap(gesture, 380, 460));
    } else if (scenario == "distance") {
        assert(!tap(gesture, 0, 80));
        assert(!tap(gesture, 180, 260, 131, 100));
    } else if (scenario == "distance-boundary") {
        assert(!tap(gesture, 0, 80));
        assert(tap(gesture, 180, 260, 130, 100));
    } else if (scenario == "diagonal-distance") {
        assert(!tap(gesture, 0, 80));
        assert(!tap(gesture, 180, 260, 122, 122));
    } else if (scenario == "swipe-return") {
        assert(!gesture.Update(0, true, 1, 100, 100));
        assert(!gesture.Update(40, true, 1, 140, 100));
        assert(!gesture.Update(60, true, 1, 100, 100));
        assert(!gesture.Update(80, true, 0));
        assert(!tap(gesture, 180, 260));
    } else if (scenario == "long") {
        assert(!tap(gesture, 0, 301));
        assert(!tap(gesture, 380, 460));
    } else if (scenario == "second-long") {
        assert(!tap(gesture, 0, 80));
        assert(!tap(gesture, 180, 481));
        assert(!tap(gesture, 580, 660));
    } else if (scenario == "multitouch") {
        assert(!tap(gesture, 0, 80));
        assert(!gesture.Update(180, true, 2, 100, 100));
        assert(!gesture.Update(200, true, 1, 100, 100));
        assert(!gesture.Update(260, true, 0));
        assert(!tap(gesture, 350, 430));
        assert(tap(gesture, 520, 600));
    } else if (scenario == "read-error") {
        assert(!tap(gesture, 0, 80));
        assert(!gesture.Update(180, true, 1, 100, 100));
        assert(!gesture.Update(200, false, 0));
        assert(!gesture.Update(220, true, 1, 100, 100));
        assert(!gesture.Update(260, true, 0));
        assert(!tap(gesture, 350, 430));
        assert(tap(gesture, 520, 600));
    } else if (scenario == "held-at-start") {
        assert(!gesture.Update(0, false, 0));
        assert(!gesture.Update(20, true, 1, 100, 100));
        assert(!gesture.Update(40, true, 0));
        assert(!tap(gesture, 100, 180));
        assert(tap(gesture, 280, 360));
    } else if (scenario == "polling-window") {
        assert(!gesture.NeedsPolling());
        assert(!tap(gesture, 0, 80));
        assert(gesture.NeedsPolling());
        assert(!gesture.Update(381, true, 0));
        assert(!gesture.NeedsPolling());
    } else if (scenario == "screen-off") {
        bool display_off = false;
        assert(!tap(gesture, 0, 80));
        if (tap(gesture, 180, 260)) display_off = !display_off;
        assert(display_off);
        assert(!gesture.Update(561, true, 0));
        assert(!tap(gesture, 600, 680));
        if (tap(gesture, 780, 860)) display_off = !display_off;
        assert(!display_off);
    } else if (scenario == "burst") {
        assert(!tap(gesture, 0, 80));
        assert(tap(gesture, 180, 260));
        assert(!tap(gesture, 300, 360));
        assert(!tap(gesture, 400, 460));
        assert(!gesture.Update(761, true, 0));
        assert(!tap(gesture, 800, 860));
        assert(tap(gesture, 950, 1000));
    } else {
        assert(false);
    }
}
''', encoding="utf-8")
        cls.binary = build / "home_touch_gesture"
        compiled = subprocess.run(
            compiler + ["-std=c++17", "-I", str(BOARD), str(source), "-o", str(cls.binary)],
            capture_output=True, text=True, timeout=30,
        )
        if compiled.returncode:
            raise AssertionError(compiled.stdout + compiled.stderr)

    def test_production_gesture_rules(self):
        for scenario in (
            "double", "single", "interval", "interval-boundary", "distance",
            "distance-boundary", "diagonal-distance", "swipe-return", "long",
            "second-long", "multitouch", "read-error", "held-at-start",
            "polling-window", "screen-off", "burst",
        ):
            with self.subTest(scenario=scenario):
                result = subprocess.run([str(self.binary), scenario], capture_output=True,
                                        text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
