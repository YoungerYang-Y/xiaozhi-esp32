#!/usr/bin/env python3
"""Package HOME girl's artwork and custom phrase, preserving the existing font."""

import argparse
import io
import json
import os
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

import build_default_assets as pack


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "main/boards/waveshare/esp32-s3-touch-lcd-1.85c/assets/home-girl/profile.json"


def _portrait(source_dir, name, size):
    if not isinstance(name, str) or not name.replace("_", "").isalnum():
        raise ValueError("Invalid portrait name")
    with Image.open(Path(source_dir) / f"{name}.png") as original:
        # Generated artwork is kept intact; this is the device's texture-size conversion.
        rgba = original.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "black")
        background.alpha_composite(rgba)
        return ImageOps.contain(background.convert("RGB"), (size, size), Image.Resampling.LANCZOS)


def _encode_portrait(image):
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _blink_animation(source_dir, size):
    neutral = _portrait(source_dir, "neutral", size)
    blink = _portrait(source_dir, "blink", size)
    if blink.size != neutral.size:
        raise ValueError("Blink frames must have identical dimensions")
    combined = Image.new("RGB", (neutral.width * 2, neutral.height))
    combined.paste(neutral, (0, 0))
    combined.paste(blink, (neutral.width, 0))
    palette = combined.quantize(colors=256)
    first = neutral.quantize(palette=palette)
    frames = [first, blink.quantize(palette=palette), first.copy()]
    output = io.BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:],
                   duration=[3800, 120, 180], loop=0, disposal=2, optimize=False)
    return output.getvalue(), neutral.size


def compile_bundle(base_assets, profile, source_dir, model_data, max_size=0x800000):
    files = pack.read_assets_entries(base_assets)
    index = json.loads(files["index.json"][0])
    if index.get("version", 1) != 1 or not index.get("text_font"):
        raise ValueError("A version-1 base bundle with a text font is required")
    protected = {"index.json", index["text_font"]}
    if index["text_font"] not in files:
        raise ValueError("The base text font is missing")
    size = profile["image_size"]
    if not isinstance(size, int) or not 1 <= size <= 160:
        raise ValueError("HOME portrait size must be between 1 and 160 pixels")
    wake = profile["wake_word"]
    if (wake.get("language") != "cn" or not 0 < wake.get("threshold", 0) < 1
            or not 0 < wake.get("duration", 0) <= 5000 or not wake.get("commands")):
        raise ValueError("Invalid HOME wake-word configuration")
    for command in wake["commands"]:
        if (command.get("action") != "wake" or not command.get("text")
                or not command.get("command")):
            raise ValueError("Invalid HOME wake phrase")
    if not model_data:
        raise ValueError("The packed MultiNet models are empty")
    for entry in index.get("emoji_collection", []):
        filename = entry["file"]
        if filename in protected:
            raise ValueError("An emoji must not overwrite the font or index")
        files.pop(filename, None)
    old_model = index.get("srmodels")
    if old_model in protected:
        raise ValueError("An SR model must not overwrite the font or index")
    files.pop(old_model, None)
    files["srmodels.bin"] = (model_data, 0, 0)
    index["srmodels"] = "srmodels.bin"
    index["multinet_model"] = wake
    index["emoji_collection"] = []
    for emotion, source in profile["emotions"].items():
        filename = f"girl-{source}.gif" if source == "neutral" else f"girl-{source}.png"
        if filename not in files:
            if source == "neutral":
                data, dimensions = _blink_animation(source_dir, size)
            else:
                image = _portrait(source_dir, source, size)
                data, dimensions = _encode_portrait(image), image.size
            files[filename] = (data, *dimensions)
        index["emoji_collection"].append({"name": emotion, "file": filename})
    files["index.json"] = (json.dumps(index, ensure_ascii=False).encode("utf-8"), 0, 0)
    result = pack.pack_assets_entries(files)
    if len(result) > max_size:
        raise ValueError(f"HOME assets {len(result)} bytes exceed partition limit {max_size}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-assets", type=Path, default=ROOT / "build/generated_assets.bin")
    parser.add_argument("--output", type=Path, default=ROOT / "build/home-girl-assets.bin")
    parser.add_argument("--profile", type=Path, default=PROFILE)
    parser.add_argument("--sdkconfig", type=Path, default=ROOT / "sdkconfig")
    args = parser.parse_args()
    config_text = args.sdkconfig.read_text(encoding="utf-8")
    if ('CONFIG_WAVESHARE_1_85C_HOME=y' not in config_text.splitlines()
            or 'CONFIG_IDF_TARGET="esp32s3"' not in config_text.splitlines()):
        parser.error("Build the ESP32-S3 HOME variant before generating its resources")
    if args.base_assets.resolve() == args.output.resolve():
        parser.error("Use a separate output; preserve the default resource bundle")
    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    model_root = ROOT / "managed_components/espressif__esp-sr/model/multinet_model"
    models = [str(model_root / "mn7_cn"), str(model_root / "fst")]
    if not all(Path(model).is_dir() for model in models):
        parser.error("Installed ESP-SR mn7_cn and fst model directories are required")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="home-girl-", dir=args.output.parent) as directory:
        model_name = pack.process_sr_models([], models, directory, directory, idf_target="esp32s3")
        if not model_name:
            raise ValueError("Could not pack the Chinese MultiNet model")
        model_data = (Path(directory) / model_name).read_bytes()
        result = compile_bundle(args.base_assets, profile, args.profile.parent / "source", model_data)
        temporary = Path(directory) / "assets.bin"
        temporary.write_bytes(result)
        os.replace(temporary, args.output)
    print(f"HOME assets: {args.output} ({len(result)} / {0x800000} bytes)")
    print("Wake phrase: " + profile["wake_word"]["commands"][0]["text"])


if __name__ == "__main__":
    main()
