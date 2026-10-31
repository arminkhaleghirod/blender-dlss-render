"""Cycles reference of the showcase frame, then the sub-pixel offset of each DLSS upscale mode against it.
Run headless after align_gui.py: blender -b --python align_cmp.py"""
import glob
import json
import os
import sys
import tempfile

import bpy
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import showcase  # noqa: E402

OUT = os.path.join(os.environ.get("DLSSPLUS_TEST_DIR", os.path.join(tempfile.gettempdir(), "dlssplus")), "align")
RES = tuple(int(v) for v in os.environ.get("DLSSPLUS_ALIGN_RES", "1280x720").split("x"))
sc = showcase.build()
sc.render.resolution_x, sc.render.resolution_y = RES
sc.frame_set(12)
sc.cycles.samples, sc.cycles.use_denoising = 512, True
ref_path = os.path.join(OUT, "cycles_ref.png")
if not os.path.exists(ref_path):
    sc.render.filepath = ref_path
    bpy.ops.render.render(write_still=True)


def lum(path):
    img = bpy.data.images.load(path)
    w, h = img.size
    px = np.empty(w * h * 4, np.float32)
    img.pixels.foreach_get(px)
    bpy.data.images.remove(img)
    a = px.reshape(h, w, 4)[..., :3] @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    return a.astype(np.float64)


def shift(a, b):
    """Sub-pixel translation of b relative to a (phase correlation with a parabolic peak fit). x right, y up."""
    win = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1]))
    fa, fb = np.fft.fft2((a - a.mean()) * win), np.fft.fft2((b - b.mean()) * win)
    r = fa.conj() * fb
    r /= np.abs(r) + 1e-12
    c = np.real(np.fft.ifft2(r))
    py, px = np.unravel_index(np.argmax(c), c.shape)

    def sub(cm, c0, cp):
        d = cm - 2 * c0 + cp
        return 0.0 if abs(d) < 1e-12 else 0.5 * (cm - cp) / d
    h, w = c.shape
    dy = py + sub(c[(py - 1) % h, px], c[py, px], c[(py + 1) % h, px])
    dx = px + sub(c[py, (px - 1) % w], c[py, px], c[py, (px + 1) % w])
    dy = dy - h if dy > h / 2 else dy
    dx = dx - w if dx > w / 2 else dx
    return float(dx), float(dy)


ref = lum(ref_path)
res = {}
for p in sorted(glob.glob(os.path.join(OUT, "dlss_*_*.png"))):
    mode = os.path.basename(p)[5:].rsplit("_", 1)[0]
    d = lum(p)
    dx, dy = shift(ref, d)
    res[mode] = {"dx": round(dx, 3), "dy": round(dy, 3), "mean_abs_diff": round(float(np.abs(d - ref).mean()), 4)}
    print("ALIGN", mode, res[mode])
with open(os.path.join(OUT, "align.json"), "w") as fh:
    json.dump(res, fh, indent=1)
