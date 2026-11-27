"""Verify the shipped alert assets fit the bounded local OGG request contract."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class LocalSoundAssetsTest(unittest.TestCase):
    def test_production_demuxer_accepts_the_actual_alert_resources(self):
        compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not compiler or not shutil.which(compiler[0]):
            self.skipTest("requires a host C++ compiler")
        driver = r'''
#include <cassert>
#include <cstdio>
#include <fstream>
#include <iterator>
#include <vector>
#include "ogg_demuxer.h"
int main(int argc, char** argv) {
    assert(argc == 3);
    for (int index = 1; index < argc; ++index) {
        std::ifstream file(argv[index], std::ios::binary);
        const std::vector<uint8_t> bytes((std::istreambuf_iterator<char>(file)), {});
        assert(!bytes.empty() && bytes.size() <= 64 * 1024);
        size_t packets = 0;
        int duration_ms = 0;
        OggDemuxer demuxer;
        demuxer.OnPacket([&](const uint8_t*, int rate, int duration, size_t size) {
            assert(rate == 8000 || rate == 12000 || rate == 16000 || rate == 24000 || rate == 48000);
            bool supported_duration = false;
            for (int supported : {5, 10, 20, 40, 60, 80, 100, 120})
                supported_duration |= duration == supported;
            assert(supported_duration && size != 0);
            ++packets;
            duration_ms += duration;
        });
        assert(demuxer.Process(bytes.data(), bytes.size()) == bytes.size());
        assert(demuxer.Finish() && !demuxer.HasError());
        assert(packets > 0 && packets <= 2000 && duration_ms <= 10000);
        std::printf("asset %d: %zu bytes, %zu packets, %d ms\n", index, bytes.size(), packets, duration_ms);
    }
}
'''
        demuxer = ROOT / "main/audio/demuxer"
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / "esp_log.h").write_text(
                '#define ESP_LOGE(...) ((void)0)\n#define ESP_LOGW(...) ((void)0)\n'
                '#define ESP_LOGD(...) ((void)0)\n', encoding="utf-8")
            source = directory / "assets.cc"
            source.write_text(driver, encoding="utf-8")
            binary = directory / "assets"
            compiled = subprocess.run(compiler + ["-std=c++17", "-I", str(directory),
                                      "-I", str(demuxer), str(source),
                                      str(demuxer / "ogg_demuxer.cc"), "-o", str(binary)],
                                      capture_output=True, text=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            result = subprocess.run([str(binary),
                                     str(ROOT / "main/assets/common/low_battery.ogg"),
                                     str(ROOT / "main/assets/common/exclamation.ogg")],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            print(result.stdout.strip())
