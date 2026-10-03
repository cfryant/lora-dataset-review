#!/usr/bin/env python3
"""
Regenerate the tiny PLACEHOLDER demo data in sample_data/ (synthetic labeled gradient frames
only; no real footage). Only needed if you want to change the samples; the output is committed.

Requires: Pillow (pip install pillow) and ffmpeg on PATH. A bold TTF font is used if found
(DejaVu / Arial), else Pillow's built-in font.
Usage:    python tools/make_sample_data.py [--out sample_data]

Creates
  sample_data/stills/demo_00N/demo_00N_original.png + demo_00N_sNN.png   (stills app input)
  sample_data/stills/selects.json  round.json                          (example state)
  sample_data/video_out/targets/demo_00N.mp4                             (video app "generated")
  sample_data/video_out/pairs/demo_00N_src.mp4                           (video app "source")
  sample_data/video_out/chosen_stills.json                               (optional still override)

Every stills candidate is the clip's "scene" with a different colour grade ("look"), so the
before/after slider has something visible to compare. Every generated video is the source
scene with a dark "night" grade, frame-for-frame aligned (same 48 frames @ 24 fps).
"""
import argparse
import json
import os
import shutil
import subprocess

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

W, H = 640, 360
FPS, FRAMES = 24, 48

# (clip, sky top, sky bottom, ground, number of candidates). demo_006 has no candidates -> "waiting".
CLIPS = [
    ("demo_001", "#1d3b6e", "#f2a65a", "#2f3b2a", 4),
    ("demo_002", "#0f5257", "#c9e4ca", "#3d405b", 4),
    ("demo_003", "#4a1942", "#f6c28b", "#253237", 6),
    ("demo_004", "#22223b", "#9a8c98", "#4a4e69", 3),
    ("demo_005", "#264653", "#e9c46a", "#2a9d8f", 4),
    ("demo_006", "#3c1518", "#d58936", "#69140e", 0),
]
# Candidate looks: (name, function). Cycled for s01, s02, ...
LOOKS = [
    ("night", lambda im: tint(ImageEnhance.Brightness(im).enhance(0.45), "#0b1a40", 0.35)),
    ("teal & orange", lambda im: tint(ImageEnhance.Contrast(im).enhance(1.25), "#00796b", 0.22)),
    ("mono", lambda im: ImageOps.colorize(ImageOps.grayscale(im), "#111111", "#f0f0f0")),
    ("warm", lambda im: tint(im, "#ff8f00", 0.25)),
    ("faded", lambda im: tint(ImageEnhance.Contrast(im).enhance(0.7), "#9e9e9e", 0.25)),
    ("blackout", lambda im: tint(ImageEnhance.Brightness(im).enhance(0.22), "#000814", 0.3)),
]


def font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf",
              "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


F_BIG, F_SMALL = font(22), font(14)


def tint(im, color, amount):
    return Image.blend(im, Image.new("RGB", im.size, color), amount)


def scene(top, bottom, ground, t=0.0):
    """Simple landscape: vertical sky gradient, sun, ground band, a 'subject' moving with t (0..1)."""
    sky = ImageOps.colorize(Image.linear_gradient("L").resize((W, H)), top, bottom)
    d = ImageDraw.Draw(sky)
    d.ellipse([W * 0.62, H * 0.30, W * 0.62 + 64, H * 0.30 + 64], fill="#fff3c4")
    d.rectangle([0, int(H * 0.72), W, H], fill=ground)
    x = int(40 + t * (W - 120))
    d.rounded_rectangle([x, int(H * 0.52), x + 40, int(H * 0.72)], radius=12, fill="#f5f5f5")
    d.ellipse([x + 8, int(H * 0.42), x + 32, int(H * 0.42) + 24], fill="#f5f5f5")
    return sky


def label(im, title, sub, right=False):
    """Caption box (bottom): left for originals/sources, right for candidates/generated, so both
    stay readable on either side of the before/after slider or wipe divider."""
    d = ImageDraw.Draw(im, "RGBA")
    w = max(d.textlength(title, F_BIG), d.textlength(sub, F_SMALL)) + 28
    x0 = W - 16 - w if right else 16
    y0 = H - 16 - 60  # bottom edge: keeps clear of the UI's own top-corner badges
    d.rounded_rectangle([x0, y0, x0 + w, y0 + 60], radius=9, fill=(0, 0, 0, 150))
    d.text((x0 + 14, y0 + 6), title, font=F_BIG, fill="white")
    d.text((x0 + 14, y0 + 36), sub, font=F_SMALL, fill=(220, 220, 220))
    return im


def save_png(im, path):
    im.quantize(colors=128, dither=Image.Dither.NONE).save(path, optimize=True)


def stills(root):
    for clip, top, bottom, ground, n in CLIPS:
        d = os.path.join(root, clip)
        os.makedirs(d, exist_ok=True)
        base = scene(top, bottom, ground, 0.0)
        save_png(label(base.copy(), clip, "original (placeholder)"), os.path.join(d, f"{clip}_original.png"))
        for k in range(1, n + 1):
            name, fn = LOOKS[(k - 1) % len(LOOKS)]
            save_png(label(fn(base.copy()), f"s{k:02d}", f"candidate: {name}", right=True),
                     os.path.join(d, f"{clip}_s{k:02d}.png"))
    with open(os.path.join(root, "selects.json"), "w") as fh:
        json.dump({
            "demo_001": {"select": 2, "rerun": False, "updated": "2026-01-01T12:00:00+00:00"},
            "demo_005": {"select": None, "rerun": False, "removed": True, "updated": "2026-01-01T12:00:00+00:00"},
        }, fh, indent=2, sort_keys=True)
        fh.write("\n")
    with open(os.path.join(root, "round.json"), "w") as fh:
        json.dump({"round": "demo-round-2", "clips": [c[0] for c in CLIPS],
                   "round_start_hidden": ["demo_001", "demo_005"]}, fh, indent=2)
        fh.write("\n")


def encode(frames, path):
    p = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
                          "-c:v", "libx264", "-preset", "veryslow", "-crf", "30", "-pix_fmt", "yuv420p",
                          "-movflags", "+faststart", path], stdin=subprocess.PIPE)
    for f in frames:
        p.stdin.write(f.tobytes())
    p.stdin.close()
    if p.wait() != 0:
        raise SystemExit("ffmpeg failed for " + path)


def videos(root):
    t, p = os.path.join(root, "targets"), os.path.join(root, "pairs")
    os.makedirs(t, exist_ok=True)
    os.makedirs(p, exist_ok=True)
    night = LOOKS[0][1]
    for clip, top, bottom, ground, _ in CLIPS[:4]:
        src = [scene(top, bottom, ground, i / (FRAMES - 1)) for i in range(FRAMES)]
        tgt = [label(night(f.copy()), "GENERATED", f"{clip} · frame {i + 1}/{FRAMES}", right=True) for i, f in enumerate(src)]
        src = [label(f, "SOURCE", f"{clip} · frame {i + 1}/{FRAMES}") for i, f in enumerate(src)]
        if clip != "demo_003":  # demo_003 has no source -> shows as "incomplete"
            encode(src, os.path.join(p, f"{clip}_src.mp4"))
        encode(tgt, os.path.join(t, f"{clip}.mp4"))
    with open(os.path.join(root, "chosen_stills.json"), "w") as fh:
        json.dump({"demo_002": 3, "demo_004": 1}, fh, indent=2)
        fh.write("\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample_data"))
    a = ap.parse_args()
    for sub in ("stills", "video_out"):
        shutil.rmtree(os.path.join(a.out, sub), ignore_errors=True)
    stills(os.path.join(a.out, "stills"))
    videos(os.path.join(a.out, "video_out"))
    print("sample data written to", a.out)


if __name__ == "__main__":
    main()
