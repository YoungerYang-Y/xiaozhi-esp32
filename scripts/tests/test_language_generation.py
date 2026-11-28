"""Exercise generated language headers from separate build directories."""

from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class LanguageGenerationTests(unittest.TestCase):
    def test_idf_dependency_scan_does_not_generate_in_the_wrong_directory(self):
        cmake = shutil.which("cmake")
        if not cmake:
            self.skipTest("CMake unavailable")
        source = (ROOT / "main/CMakeLists.txt").read_text()
        generation = source.split("# Define generation path\n", 1)[1].split(
            "# Collect current language audio files", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scan = root / "scan.cmake"
            scan.write_text(
                'set(CMAKE_BUILD_EARLY_EXPANSION 1)\n'
                f'set(CMAKE_CURRENT_SOURCE_DIR "{root}/not-main")\n'
                f'set(CMAKE_CURRENT_BINARY_DIR "{root}/scan-output")\n'
                + generation
            )
            result = subprocess.run([cmake, "-P", str(scan)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((root / "not-main").exists())
            self.assertFalse((root / "scan-output").exists())

    def generate(self, locale, output):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/gen_lang.py"),
             "--language", locale, "--output", str(output)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_header_can_be_generated_outside_source_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            header = Path(directory) / "generated/xiaozhi_lang_config.h"
            self.generate("zh-CN", header)
            self.assertIn('CONNECTING = "连接中..."', header.read_text())
            self.assertIn('CODE = "zh-CN"', header.read_text())

    def test_second_locale_cannot_replace_first_build_language(self):
        compiler = shutil.which("c++")
        if not compiler:
            self.skipTest("host compiler unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chinese = root / "home/generated"
            english = root / "other/generated"
            self.generate("zh-CN", chinese / "xiaozhi_lang_config.h")
            original = (chinese / "xiaozhi_lang_config.h").read_bytes()
            self.generate("en-US", english / "xiaozhi_lang_config.h")
            self.assertEqual(original, (chinese / "xiaozhi_lang_config.h").read_bytes())
            source = root / "probe.cc"
            audio_symbols = set(re.findall(r'asm\("([^"]+)"\)', original.decode()))
            source.write_text(
                'extern "C" {\n' + ''.join(
                    f'extern const char {name}[] = {{0}};\n' for name in sorted(audio_symbols)
                ) + '}\n' +
                '#include "assets/lang_config.h"\n'
                '#include <iostream>\n'
                'int main() { std::cout << Lang::CODE << "|" << Lang::Strings::CONNECTING; }\n'
            )
            includes = root / "source/assets"
            includes.mkdir(parents=True)
            shutil.copyfile(ROOT / "main/assets/lang_config.h.in", includes / "lang_config.h")
            for generated, expected in ((chinese, "zh-CN|连接中..."), (english, "en-US|Connecting...")):
                binary = generated.parent / "probe"
                compiled = subprocess.run(
                    [compiler, "-std=c++17", "-I", str(root / "source"), "-I", str(generated),
                     str(source), "-o", str(binary)], capture_output=True, text=True,
                )
                self.assertEqual(compiled.returncode, 0, compiled.stderr)
                executed = subprocess.run([str(binary)], capture_output=True, text=True)
                self.assertEqual(executed.returncode, 0, executed.stderr)
                self.assertEqual(executed.stdout, expected)
