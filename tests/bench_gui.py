"""README benchmark + images, DLSS side (needs a window)."""
import importlib
import json
import os
import tempfile
import shutil
import sys
import time
import traceback

import bpy

T = os.environ.get("DLSSPLUS_TEST_DIR", os.path.join(tempfile.gettempdir(), "dlssplus"))
OUT = os.path.join(T, "bench")
shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import showcase  # noqa: E402

PKG = "bl_ext.user_default.dlss_render_plus"
res = {}


def save():
    with open(os.path.join(OUT, "dlss.json"), "w") as fh:
        json.dump(res, fh, indent=1)


def win_ctx():
    win = bpy.context.window_manager.windows[0]
    area = max((a for a in win.screen.areas if a.type == "VIEW_3D"), key=lambda a: a.width * a.height)
    return win, area


def start(animation):
    win, area = win_ctx()
    region = next(r for r in area.regions if r.type == "WINDOW")
    with bpy.context.temp_override(window=win, area=area, region=region, scene=win.scene):
        bpy.ops.render.dlss_render_plus("INVOKE_DEFAULT", animation=animation)


def wait(stats):
    render = importlib.import_module(PKG + ".render")
    t0 = time.time()
    while render.ACTIVE is None and time.time() - t0 < 5:
        yield 0.2
    while render.ACTIVE is not None:
        job = render.ACTIVE
        if len(job.saved_at) > 1:
            stats["spf"] = job.seconds_per_frame()
            stats["tiles"] = getattr(job, "tile_count", 1)
        yield 0.3
    stats["total"] = time.time() - t0
    yield 1.0


def steps():
    sc = showcase.build()
    s = sc.dlss_render_plus
    s.quality = "BALANCED"
    yield 2.0

    # A. main benchmark: 1080p, Balanced, 12 frames of the dolly
    sc.frame_start, sc.frame_end = 1, 12
    sc.render.filepath = os.path.join(OUT, "anim", "a_")
    st = {}
    start(True)
    yield from wait(st)
    res["anim_balanced"] = st
    save()

    # B. still, High quality, for the hero image
    s.quality = "HIGH"
    sc.frame_set(12)
    sc.render.filepath = os.path.join(OUT, "hero", "hero_")
    st = {}
    start(False)
    yield from wait(st)
    res["still_high"] = st
    s.quality = "BALANCED"
    save()

    # C. Steady: static close-up of the glass, 16 frames, off and on
    sc.camera = bpy.data.objects["CamGlass"]
    sc.frame_start, sc.frame_end = 1, 16
    for on in (False, True):
        s.use_steady = on
        sc.render.filepath = os.path.join(OUT, "steady_on" if on else "steady_off", "g_")
        st = {}
        start(True)
        yield from wait(st)
        res["steady_" + ("on" if on else "off")] = st
        save()
    s.use_steady = False
    sc.camera = bpy.data.objects["Cam"]

    # D. passes on one frame
    s.passes = {"Z", "NORMAL", "CRYPTO", "AO", "VECTOR", "ALBEDO"}
    s.pass_samples = 16
    sc.frame_start = sc.frame_end = 12
    sc.render.filepath = os.path.join(OUT, "passes", "p_")
    st = {}
    start(True)
    yield from wait(st)
    res["passes"] = st
    save()

    # E. screenshot of the panel (passes ticked, quality Balanced)
    pkg = importlib.import_module(PKG)
    win = bpy.context.window_manager.windows[0]
    props = max((a for a in win.screen.areas if a.type == "PROPERTIES"), key=lambda a: a.height)
    props.spaces.active.context = "RENDER"
    sc.frame_start, sc.frame_end = 1, 48
    yield 1.0
    with bpy.context.temp_override(window=win, area=props):
        bpy.ops.screen.screenshot_area(filepath=os.path.join(OUT, "panel.png"))
    res["panel_size"] = (props.width, props.height)
    save()


def driver():
    gen = steps()

    def tick():
        try:
            return next(gen)
        except StopIteration:
            pass
        except Exception:
            res["error"] = traceback.format_exc()
        save()
        bpy.ops.wm.quit_blender()
        return None
    bpy.app.timers.register(tick, first_interval=3.0)


driver()
