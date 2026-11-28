"""Exercise HOME custom resources through the real mmap packer and image codecs."""

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import build_default_assets as pack

spec = importlib.util.spec_from_file_location("home_girl_assets", ROOT / "scripts/build_home_girl_assets.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class HomeGirlAssetsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.profile = {
            "image_size": 160,
            "wake_word": {"language": "cn", "duration": 3000, "threshold": 0.2,
                          "commands": [{"command": "ni hao xiao mei mei", "text": "你好小妹妹", "action": "wake"}]},
            "emotions": {"neutral": "neutral", "happy": "happy", "sad": "sad"},
        }
        for name, color in (("neutral", "pink"), ("blink", "black"), ("happy", "salmon"), ("sad", "gray")):
            Image.new("RGB", (320, 320), color).save(self.root / f"{name}.png")
        self.font = b"original common font"
        self.meta = {"charset": "common", "size": 16, "bpp": 4, "bundle": "noto-v1"}
        index = {"version": 1, "srmodels": "old-model.bin", "text_font": "font.bin",
                 "text_font_meta": self.meta, "emoji_collection": [{"name": "neutral", "file": "old.png"}]}
        self.base = self.root / "base.bin"
        self.base.write_bytes(pack.pack_assets_entries({
            "index.json": (json.dumps(index).encode(), 0, 0), "font.bin": (self.font, 0, 0),
            "old-model.bin": (b"old model", 0, 0), "old.png": (b"old emoji", 64, 64),
            "retained.bin": (b"unrelated resource", 0, 0),
        }))

    def compile(self, **kwargs):
        result = builder.compile_bundle(self.base, self.profile, self.root, b"new multinet", **kwargs)
        output = self.root / "result.bin"
        output.write_bytes(result)
        return pack.read_assets_entries(output)

    def test_full_home_profile_contains_fixed_actions_and_phrases(self):
        profile = json.loads(builder.PROFILE.read_text())
        commands = profile["wake_word"]["commands"]
        self.assertEqual({c["action"]: c["text"] for c in commands}, {
            "wake": "你好小妹妹", "screen_on": "打开屏幕", "screen_off": "关闭屏幕",
            "volume_up": "音量大一点", "volume_down": "音量小一点",
            "brightness_up": "屏幕亮一点", "brightness_down": "屏幕暗一点",
            "device_status": "查看设备状态", "cancel": "取消操作"})

    def test_accepts_fixed_actions_and_rejects_unknown_or_duplicate_commands(self):
        command = {"command": "da kai ping mu", "text": "打开屏幕", "action": "screen_on"}
        self.profile["wake_word"]["commands"].append(command)
        self.compile()
        for bad in ({**command, "action": "reboot"}, command,
                    {**command, "command": "other", "action": "screen_off"}):
            with self.subTest(bad=bad):
                self.profile["wake_word"]["commands"].append(bad)
                with self.assertRaisesRegex(ValueError, "action|Duplicate|duplicate"):
                    self.compile()
                self.profile["wake_word"]["commands"].pop()

    def test_replaces_phrase_and_models_without_losing_font_or_other_resources(self):
        files = self.compile()
        index = json.loads(files["index.json"][0])
        self.assertEqual(index.get("multinet_model"), self.profile["wake_word"])
        self.assertEqual(files[index["srmodels"]][0], b"new multinet")
        self.assertEqual(files[index["text_font"]][0], self.font)
        self.assertEqual(index["text_font_meta"], self.meta)
        self.assertEqual(files["retained.bin"][0], b"unrelated resource")
        self.assertNotIn("old-model.bin", files)
        self.assertNotIn("old.png", files)

    def test_all_emotions_use_bounded_girl_images_and_neutral_blinks(self):
        self.profile["emotions"]["relaxed"] = "neutral"
        files = self.compile()
        index = json.loads(files["index.json"][0])
        entries = {item["name"]: item["file"] for item in index["emoji_collection"]}
        self.assertEqual(set(entries), set(self.profile["emotions"]))
        self.assertEqual(entries["neutral"], entries["relaxed"])
        for filename in set(entries.values()):
            data, width, height = files[filename]
            self.assertEqual((width, height), (160, 160))
            with Image.open(io.BytesIO(data)) as image:
                self.assertEqual(image.size, (160, 160))
                if filename.endswith(".gif"):
                    self.assertEqual(image.info["loop"], 0)
                    self.assertEqual(image.n_frames, 3)
                    durations, colors = [], []
                    for frame in range(image.n_frames):
                        image.seek(frame)
                        durations.append(image.info["duration"])
                        colors.append(image.convert("RGB").getpixel((80, 80)))
                    self.assertEqual(durations, [3800, 120, 180])
                    self.assertEqual(colors[0], colors[2])
                    self.assertNotEqual(colors[0], colors[1])

    def test_rejects_oversize_before_emitting_a_bundle(self):
        with self.assertRaisesRegex(ValueError, "exceed partition"):
            self.compile(max_size=100)
        self.assertFalse((self.root / "result.bin").exists())

    def test_rejects_missing_model_and_oversized_portrait(self):
        with self.assertRaisesRegex(ValueError, "models are empty"):
            builder.compile_bundle(self.base, self.profile, self.root, b"")
        self.profile["image_size"] = 1280
        with self.assertRaisesRegex(ValueError, "160 pixels"):
            self.compile()

    def test_font_cannot_be_removed_through_an_old_emoji_reference(self):
        files = pack.read_assets_entries(self.base)
        index = json.loads(files["index.json"][0])
        index["emoji_collection"][0]["file"] = "font.bin"
        files["index.json"] = (json.dumps(index).encode(), 0, 0)
        self.base.write_bytes(pack.pack_assets_entries(files))
        with self.assertRaisesRegex(ValueError, "must not overwrite"):
            self.compile()


if __name__ == "__main__":
    unittest.main()
