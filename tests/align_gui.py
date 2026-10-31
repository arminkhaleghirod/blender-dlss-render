"""Render the showcase frame once per DLSS upscale mode (needs a window). align_cmp.py measures the offsets."""
import importlib
import json
import os
import sys
import tempfile
import time
import traceback

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import showcase  # noqa: E402

OUT = os.path.join(os.environ.get("DLSSPLUS_TEST_DIR", os.path.join(tempfile.gettempdir(), "dlssplus")), "align")
os.makedirs(OUT, exist_ok=True)
RES = tuple(int(v) for v in os.environ.get("DLSSPLUS_ALIGN_RES", "1280x720").split("x"))
PKG = "bl_ext.user_default.dlss_render_plus"
info = {}


def steps():
    sc = showcase.build()
    sc.render.resolution_x, sc.render.resolution_y = RES
    sc.frame_set(12)
    sc.dlss_render_plus.quality = "HIGH"
    render = importlib.import_module(PKG + ".render")
    modes = [i.identifier for i in sc.cycles.bl_rna.properties["preview_denoising_upscale_quality"].enum_items]
    if os.environ.get("DLSSPLUS_ALIGN_MODES"):
        modes = [m for m in modes if m in os.environ["DLSSPLUS_ALIGN_MODES"].split(",")]
    info["modes"] = modes
    yield 2.0
    for m in modes:
        sc.cycles.preview_denoising_upscale_quality = m
        sc.render.filepath = os.path.join(OUT, f"dlss_{m}_")
        win = bpy.context.window_manager.windows[0]
        area = max((a for a in win.screen.areas if a.type == "VIEW_3D"), key=lambda a: a.width * a.height)
        region = next(r for r in area.regions if r.type == "WINDOW")
        with bpy.context.temp_override(window=win, area=area, region=region, scene=sc):
            bpy.ops.render.dlss_render_plus("INVOKE_DEFAULT", animation=False)
        t0 = time.time()
        while render.ACTIVE is None and time.time() - t0 < 5:
            yield 0.2
        while render.ACTIVE is not None:
            yield 0.3
        yield 1.0


def driver():
    gen = steps()

    def tick():
        try:
            return next(gen)
        except StopIteration:
            pass
        except Exception:
            info["error"] = traceback.format_exc()
        with open(os.path.join(OUT, "align_gui.json"), "w") as fh:
            json.dump(info, fh, indent=1)
        bpy.ops.wm.quit_blender()
        return None
    bpy.app.timers.register(tick, first_interval=3.0)


driver()
