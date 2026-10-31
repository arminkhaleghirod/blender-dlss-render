"""Edge-case live tests for DLSS Render Plus (needs a window, RTX and DLSS). Run: blender --python gui_edge.py"""
import importlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

import bpy
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.environ.get("DLSSPLUS_TEST_DIR", os.path.join(tempfile.gettempdir(), "dlssplus")), "out_edge")
shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT)
PKG = "bl_ext.user_default.dlss_render_plus"

# reuse the scene and helpers of the main live test without running it
src = open(os.path.join(HERE, "gui_test.py"), encoding="utf-8").read().split("\ndef driver():")[0]
src = src.replace("shutil.rmtree(OUT, ignore_errors=True)", "").replace("os.makedirs(OUT)", "")
g = {"__name__": "gui_test_lib", "__file__": os.path.join(HERE, "gui_test.py")}
exec(src, g)
g["OUT"] = OUT
build_scene, start_op, wait_job, read_img, win_ctx = (g[k] for k in ("build_scene", "start_op", "wait_job", "read_img",
                                                                      "win_ctx"))
results = g["results"]
check, save, log = g["check"], g["save"], g["log"]


def state_ok(sc, tag):
    """Everything the job changes must be back after it ends, however it ended."""
    render = importlib.import_module(PKG + ".render")
    win, area = win_ctx()
    sp = area.spaces.active
    problems = []
    if render.ACTIVE is not None:
        problems.append("ACTIVE still set")
    if win.screen.show_fullscreen:
        problems.append("still full screen")
    if sp.shading.type == "RENDERED":
        problems.append("viewport left in Rendered")
    if not sp.overlay.show_overlays:
        problems.append("overlays left off")
    if sc.cycles.preview_samples == 0:
        problems.append("viewport sample cap left at 0")
    check(f"edge.state_restored.{tag}", not problems, ", ".join(problems))


def files(d):
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def steps():
    sc = build_scene()
    s = sc.dlss_render_plus
    sc.cycles.preview_samples = 1024
    render = importlib.import_module(PKG + ".render")
    yield 2.0
    win, area = win_ctx()
    area.spaces.active.shading.type = "SOLID"
    area.spaces.active.overlay.show_overlays = True

    # 1. transparent PNG: background alpha 0, objects opaque
    sc.render.film_transparent = True
    sc.render.image_settings.color_mode = "RGBA"
    sc.frame_set(5)
    sc.render.filepath = os.path.join(OUT, "alpha", "a_")
    start_op(bpy.ops.render.dlss_render_plus, animation=False)
    yield from wait_job()
    f = files(os.path.join(OUT, "alpha"))
    if f:
        img = bpy.data.images.load(os.path.join(OUT, "alpha", f[0]))
        w, h = img.size
        px = np.empty(w * h * 4, np.float32)
        img.pixels.foreach_get(px)
        a = px.reshape(h, w, 4)[..., 3]
        bpy.data.images.remove(img)
        check("edge.transparent_png", a.min() < 0.01 and a.max() > 0.99 and img is not None,
              f"alpha min {a.min():.3f} max {a.max():.3f}, transparent share {(a < 0.01).mean():.2f}")
    else:
        check("edge.transparent_png", False, "no file")
    sc.render.film_transparent = False
    state_ok(sc, "transparent")

    # 2. JPEG, 50% of an odd resolution, frame step 2 with negative frames
    sc.render.image_settings.file_format = "JPEG"
    sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage = 1001, 777, 50
    sc.frame_start, sc.frame_end, sc.frame_step = 0, 4, 2     # Blender's render range can't go below 0
    sc.render.filepath = os.path.join(OUT, "jpeg", "j_")
    sc.cycles.use_preview_adaptive_sampling = True      # Blender's default; it used to stall small frames
    start_op(bpy.ops.render.dlss_render_plus, animation=True)
    job_spf = {}
    t0 = time.time()
    while render.ACTIVE is None and time.time() - t0 < 5:
        yield 0.1
    while render.ACTIVE is not None:
        if len(render.ACTIVE.saved_at) > 1:
            job_spf["spf"] = render.ACTIVE.seconds_per_frame()
        yield 0.3
    yield 1.0
    check("edge.adaptive_setting_restored", sc.cycles.use_preview_adaptive_sampling is True)
    f = files(os.path.join(OUT, "jpeg"))
    size = tuple(bpy.data.images.load(os.path.join(OUT, "jpeg", f[0])).size) if f else None
    check("edge.jpeg_step_and_size", f == ["j_0000.jpg", "j_0002.jpg", "j_0004.jpg"] and size == (500, 388),
          f"{f} size {size}")
    check("edge.small_frames_fast", job_spf.get("spf", 99) < 5, f"{job_spf}")
    sc.frame_step = 1
    sc.render.resolution_percentage = 100
    state_ok(sc, "jpeg")

    # 3. single-layer EXR (scene linear, no view transform baked in)
    sc.render.image_settings.file_format = "OPEN_EXR"
    sc.render.resolution_x, sc.render.resolution_y = 640, 360
    sc.frame_set(5)
    sc.render.filepath = os.path.join(OUT, "exr", "e_")
    start_op(bpy.ops.render.dlss_render_plus, animation=False)
    yield from wait_job()
    f = files(os.path.join(OUT, "exr"))
    check("edge.exr_single", len(f) == 1 and f[0].endswith(".exr"), str(f))
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGB"

    # 4. movie with an odd size and frame step 2 (ffmpeg needs even sizes for yuv420p)
    sc.render.resolution_x, sc.render.resolution_y = 641, 361
    sc.frame_start, sc.frame_end, sc.frame_step = 1, 9, 2
    sc.render.filepath = os.path.join(OUT, "movie_odd", "m_")
    sc.render.image_settings.media_type = "VIDEO"
    sc.render.ffmpeg.format, sc.render.ffmpeg.codec = "MPEG4", "H264"
    start_op(bpy.ops.render.dlss_render_plus, animation=True)
    yield from wait_job()
    mp4 = os.path.join(OUT, "movie_odd", "m_0001-0009.mp4")
    info = g["ffprobe_frames"](mp4) if os.path.exists(mp4) else {}
    check("edge.movie_odd_size_step", info.get("nb_read_frames") == "5" and info.get("width") == 642,
          f"{info} message: {render.LAST_MESSAGE if hasattr(render, 'LAST_MESSAGE') else ''}")
    sc.frame_step = 1
    state_ok(sc, "movie_odd")

    # 5. Esc while ffmpeg runs: the movie step must stop cleanly
    sc.render.resolution_x, sc.render.resolution_y = 640, 360
    sc.frame_start, sc.frame_end = 1, 6
    sc.render.ffmpeg.codec = "AV1"      # slow enough to catch
    sc.render.filepath = os.path.join(OUT, "movie_cancel", "c_")
    start_op(bpy.ops.render.dlss_render_plus, animation=True)
    t0 = time.time()
    while render.ACTIVE is None and time.time() - t0 < 5:
        yield 0.1
    caught = False
    while render.ACTIVE is not None:
        if render.ACTIVE.phase == "Making the movie":
            render.ACTIVE.cancel = True
            caught = True
        yield 0.05
    yield 1.0
    leftover = [p for p in files(os.path.join(OUT, "movie_cancel")) if p.endswith(".mp4")]
    procs = subprocess.run(["tasklist"], capture_output=True, text=True).stdout.lower().count("ffmpeg.exe")
    check("edge.cancel_during_movie", caught and procs == 0, f"caught {caught}, ffmpeg running {procs}, files {leftover}")
    sc.render.image_settings.media_type = "IMAGE"
    sc.render.image_settings.file_format = "PNG"
    state_ok(sc, "movie_cancel")

    # 6. ffmpeg missing
    prefs = bpy.context.preferences.addons[PKG].preferences
    old_path, old_env = prefs.ffmpeg_path, os.environ.get("PATH", "")
    prefs.ffmpeg_path = r"Q:\nowhere\ffmpeg.exe"
    os.environ["PATH"] = os.pathsep.join(p for p in old_env.split(os.pathsep) if "ffmpeg" not in p.lower())
    sc.render.image_settings.media_type = "VIDEO"
    win, area = win_ctx()
    region = next(r for r in area.regions if r.type == "WINDOW")
    try:
        with bpy.context.temp_override(window=win, area=area, region=region):
            ret = str(bpy.ops.render.dlss_render_plus("EXEC_DEFAULT", animation=True))
    except RuntimeError as e:      # an operator's error report becomes an exception when called from Python
        ret = str(e)
    check("edge.no_ffmpeg_refuses", "needs ffmpeg" in ret and render.ACTIVE is None, ret)
    prefs.ffmpeg_path, os.environ["PATH"] = old_path, old_env
    sc.render.image_settings.media_type = "IMAGE"
    sc.render.image_settings.file_format = "PNG"
    yield 0.5

    # 7. output folder that can't be written: error, everything put back
    sc.frame_start, sc.frame_end = 1, 3
    sc.render.filepath = "Q:\\does\\not\\exist\\x_"
    try:
        with bpy.context.temp_override(window=win, area=area, region=region):
            ret = str(bpy.ops.render.dlss_render_plus("EXEC_DEFAULT", animation=True))
    except RuntimeError as e:
        ret = str(e)
    check("edge.bad_output_refused_early", "output folder" in ret and render.ACTIVE is None, ret)
    yield 0.5
    state_ok(sc, "bad_output")

    # 8. Esc during a passes render
    sc.render.filepath = os.path.join(OUT, "passes_cancel", "p_")
    sc.frame_start, sc.frame_end = 1, 8
    s.passes = {"Z", "CRYPTO"}
    start_op(bpy.ops.render.dlss_render_plus, animation=True)
    yield from wait_job(cancel_after=2)
    im = sc.render.image_settings
    check("edge.cancel_during_passes", im.file_format == "PNG" and not sc.view_layers[0].use_pass_z
          and not any(p.endswith(".part.exr") for p in files(os.path.join(OUT, "passes_cancel"))),
          f"format {im.file_format}, z {sc.view_layers[0].use_pass_z}, files {files(os.path.join(OUT, 'passes_cancel'))}")
    s.passes = set()
    state_ok(sc, "passes_cancel")

    # 9. forced tiles at 1080p (pretend the viewport is 800 x 500)
    os.environ["DLSS_RENDER_MAX_REGION"] = "800x500"
    sc.render.resolution_x, sc.render.resolution_y = 1920, 1080
    sc.frame_set(5)
    sc.render.filepath = os.path.join(OUT, "tiles", "t_")
    start_op(bpy.ops.render.dlss_render_plus, animation=False)
    yield from wait_job()
    del os.environ["DLSS_RENDER_MAX_REGION"]
    f = files(os.path.join(OUT, "tiles"))
    check("edge.forced_tiles_written", len(f) == 1, str(f))
    sc.render.filepath = os.path.join(OUT, "notiles", "n_")
    start_op(bpy.ops.render.dlss_render_plus, animation=False)
    yield from wait_job()
    f2 = files(os.path.join(OUT, "notiles"))
    if f and f2:
        a = read_img(os.path.join(OUT, "tiles", f[0]))[..., :3]
        b = read_img(os.path.join(OUT, "notiles", f2[0]))[..., :3]
        d = np.abs(a - b).mean(axis=2)
        # a seam shows as a line of larger differences; compare the worst column/row band with the average
        cols, rows = d.mean(axis=0), d.mean(axis=1)
        check("edge.tiles_match_single", float(d.mean()) < 0.01 and cols.max() < 6 * d.mean() + 0.01
              and rows.max() < 6 * d.mean() + 0.01,
              f"mean diff {d.mean():.4f}, worst column {cols.max():.4f}, worst row {rows.max():.4f}")
    sc.render.resolution_x, sc.render.resolution_y = 640, 360

    # 10. 60 frames: crosses the 50-frame restart and the marker cut at 13
    sc.frame_start, sc.frame_end = 1, 60
    sc.render.filepath = os.path.join(OUT, "long", "l_")
    t0 = time.time()
    start_op(bpy.ops.render.dlss_render_plus, animation=True)
    yield from wait_job()
    f = files(os.path.join(OUT, "long"))
    means = [float(read_img(os.path.join(OUT, "long", x))[..., :3].mean()) for x in f]
    jumps = [abs(means[i + 1] - means[i]) for i in range(len(means) - 1)]
    check("edge.long_60_frames", len(f) == 60 and min(means) > 0.02 and max(jumps[20:]) < 0.02,
          f"{len(f)} frames in {time.time() - t0:.0f}s, min mean {min(means) if means else 0:.3f}, "
          f"largest brightness jump after frame 20: {max(jumps[20:]) if len(jumps) > 20 else 0:.4f}")
    state_ok(sc, "long")

    # 11. a second view layer that hides the cube; render with it active
    vl2 = sc.view_layers.new("Second")
    cube = next(o for o in sc.objects if o.type == "MESH" and o.dimensions.z > 1.9 and o.location.x < 0)
    win.view_layer = vl2
    lc = vl2.layer_collection
    col = cube.users_collection[0]
    target = next((c for c in lc.children if c.collection == col), None)
    hidden_cube = False
    if target is not None:
        target.exclude = False
    cube_hide = cube.hide_get(view_layer=vl2)
    sc.frame_set(5)
    sc.render.filepath = os.path.join(OUT, "layer2", "v_")
    start_op(bpy.ops.render.dlss_render_plus, animation=False)
    yield from wait_job()
    check("edge.second_view_layer", len(files(os.path.join(OUT, "layer2"))) == 1
          and cube.hide_get(view_layer=vl2) == cube_hide, str(files(os.path.join(OUT, "layer2"))))
    win.view_layer = sc.view_layers[0]
    state_ok(sc, "view_layer")
    save()

    # 12. Ctrl+S during a render: the file must be saved with the user's settings, and the render stops
    blend = os.path.join(OUT, "saved_mid_render.blend")
    sc.frame_start, sc.frame_end = 1, 30
    sc.render.filepath = os.path.join(OUT, "midsave", "s_")
    sc.render.image_settings.color_depth = "8"
    sc.cycles.samples = 77
    s.passes = {"Z"}
    start_op(bpy.ops.render.dlss_render_plus, animation=True)
    t0 = time.time()
    while (render.ACTIVE is None or render.ACTIVE.saved < 2) and time.time() - t0 < 120:
        yield 0.2
    bpy.ops.wm.save_as_mainfile(filepath=blend, copy=True)
    yield from wait_job()
    s.passes = set()
    import subprocess as sp_
    probe = sp_.run([bpy.app.binary_path, "-b", "--factory-startup", blend, "--python-expr",
                     "import bpy; sc=bpy.context.scene; print('SAVED', sc.render.image_settings.file_format, "
                     "sc.cycles.samples, sc.cycles.preview_samples, sc.view_layers[0].use_pass_z)"],
                    capture_output=True, text=True, timeout=300)
    line = next((ln for ln in probe.stdout.splitlines() if ln.startswith("SAVED")), probe.stdout[-300:])
    check("edge.save_mid_render_clean", line == "SAVED PNG 77 1024 False", line)
    check("edge.save_mid_render_stopped", len(files(os.path.join(OUT, "midsave"))) < 30,
          f"{len(files(os.path.join(OUT, 'midsave')))} frames saved")
    state_ok(sc, "midsave")

    # 13. autosave every minute during a long render must not stop it (autosave doesn't save the user's file)
    bpy.context.preferences.filepaths.use_auto_save_temporary_files = True
    bpy.context.preferences.filepaths.auto_save_time = 1
    sc.frame_start, sc.frame_end = 1, 75
    sc.render.filepath = os.path.join(OUT, "autosave", "a_")
    t0 = time.time()
    start_op(bpy.ops.render.dlss_render_plus, animation=True)
    yield from wait_job()
    n = len(files(os.path.join(OUT, "autosave")))
    check("edge.autosave_does_not_stop", n == 75, f"{n} of 75 frames in {time.time() - t0:.0f}s (autosave every 60 s)")
    bpy.context.preferences.filepaths.auto_save_time = 2

    # 14. disabling the add-on during a render
    import addon_utils
    sc.frame_start, sc.frame_end = 1, 30
    sc.render.filepath = os.path.join(OUT, "disable", "d_")
    start_op(bpy.ops.render.dlss_render_plus, animation=True)
    t0 = time.time()
    while (render.ACTIVE is None or render.ACTIVE.saved < 2) and time.time() - t0 < 120:
        yield 0.2
    addon_utils.disable(PKG, default_set=False)
    yield 2.0
    count = len(files(os.path.join(OUT, "disable")))
    yield 2.0
    still_writing = len(files(os.path.join(OUT, "disable"))) != count
    sp = win_ctx()[1].spaces.active
    check("edge.disable_mid_render", not still_writing and sp.shading.type != "RENDERED"
          and sc.cycles.preview_samples == 1024, f"frames {count}, still writing {still_writing}, "
          f"shading {sp.shading.type}, preview samples {sc.cycles.preview_samples}")
    addon_utils.enable(PKG, default_set=False)
    render = importlib.import_module(PKG + ".render")
    if win_ctx()[0].screen.show_fullscreen:
        area = win_ctx()[1]
        with bpy.context.temp_override(window=win_ctx()[0], area=area):
            bpy.ops.screen.back_to_previous()
    yield 1.0

    # 15. opening another file during a render (last: this scene is gone afterwards)
    sc.render.filepath = os.path.join(OUT, "load", "l_")
    start_op(bpy.ops.render.dlss_render_plus, animation=True)
    t0 = time.time()
    while (render.ACTIVE is None or render.ACTIVE.saved < 2) and time.time() - t0 < 120:
        yield 0.2
    bpy.ops.wm.read_homefile(use_empty=False)
    yield 2.0
    count = len(files(os.path.join(OUT, "load")))
    yield 2.0
    check("edge.load_mid_render", render.ACTIVE is None and len(files(os.path.join(OUT, "load"))) == count
          and hasattr(bpy.ops.render, "dlss_render_plus") and bpy.ops.render.dlss_render_plus.poll(),
          f"ACTIVE {render.ACTIVE}, frames {count}")
    save()


def driver():
    gen = steps()

    def tick():
        try:
            return next(gen)
        except StopIteration:
            pass
        except Exception:
            results["edge.exception"] = traceback.format_exc()
            log(traceback.format_exc())
        with open(os.path.join(OUT, "results.json"), "w") as fh:
            json.dump(results, fh, indent=1)
        log("DONE")
        bpy.ops.wm.quit_blender()
        return None
    bpy.app.timers.register(tick, first_interval=3.0, persistent=True)    # survives the file load in step 15


driver()
