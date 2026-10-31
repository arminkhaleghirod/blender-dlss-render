"""README benchmark, Cycles side: Blender's default final-render settings on the same scene and frames."""
import json
import os
import tempfile
import sys
import time

import bpy

T = os.environ.get("DLSSPLUS_TEST_DIR", os.path.join(tempfile.gettempdir(), "dlssplus"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import showcase  # noqa: E402

OUT = os.path.join(T, "bench")
sc = showcase.build()
c = sc.cycles
c.samples, c.use_adaptive_sampling, c.adaptive_threshold, c.use_denoising = 4096, True, 0.01, True
res = {"settings": "4096 max samples, adaptive threshold 0.01, OpenImageDenoise (Blender defaults)", "frames": {}}
bpy.ops.render.render()     # warm-up: kernel load and BVH build are not counted
for f in (1, 6, 12):
    sc.frame_set(f)
    sc.render.filepath = os.path.join(OUT, "cycles", f"c_{f:04d}")
    t0 = time.perf_counter()
    bpy.ops.render.render(write_still=True)
    res["frames"][f] = time.perf_counter() - t0
    print("CYCLES frame", f, round(res["frames"][f], 2), "s")
# glass close-up reference for the Steady figure
sc.camera = bpy.data.objects["CamGlass"]
sc.frame_set(8)
sc.render.filepath = os.path.join(OUT, "cycles", "glass_0008")
bpy.ops.render.render(write_still=True)
with open(os.path.join(OUT, "cycles.json"), "w") as fh:
    json.dump(res, fh, indent=1)
