"""Live DLSS test for DLSS Render Plus. Run: blender.exe --python gui_test.py (with a window, not -b)."""
import importlib
import json
import math
import os
import tempfile
import shutil
import subprocess
import time
import traceback

import bpy
import numpy as np

OUT = os.path.join(os.environ.get("DLSSPLUS_TEST_DIR", os.path.join(tempfile.gettempdir(), "dlssplus")), "out_gui")
shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT)
PKG = "bl_ext.user_default.dlss_render_plus"
results = {}
log_lines = []


def log(*a):
    s = " ".join(str(x) for x in a)
    log_lines.append(s)
    print("GUITEST", s)


def check(name, cond, info=""):
    results[name] = ("PASS" if cond else "FAIL") + (f" {info}" if info else "")
    log(name, results[name])


def save():
    with open(os.path.join(OUT, "results.json"), "w") as fh:
        json.dump({"results": results, "log": log_lines}, fh, indent=1)


def build_scene():
    sc = bpy.context.scene
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    bpy.ops.mesh.primitive_plane_add(size=30, location=(0, 0, -1))
    bpy.ops.mesh.primitive_cube_add(location=(-1.5, 0, 0))
    cube = bpy.context.object
    cube.rotation_euler = (0, 0, 0)
    cube.keyframe_insert("rotation_euler", frame=1)
    cube.rotation_euler = (0, 0, math.radians(180))
    cube.keyframe_insert("rotation_euler", frame=40)
    m = bpy.data.materials.new("red")
    m.diffuse_color = (0.8, 0.1, 0.05, 1)
    m.use_nodes = True
    m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.8, 0.1, 0.05, 1)
    cube.data.materials.append(m)
    bpy.ops.mesh.primitive_uv_sphere_add(location=(1.5, 0, 0), segments=64, ring_count=32)
    bpy.ops.object.shade_smooth()
    glass = bpy.data.materials.new("glass")
    glass.use_nodes = True
    p = glass.node_tree.nodes["Principled BSDF"]
    p.inputs["Transmission Weight"].default_value = 1.0
    p.inputs["Roughness"].default_value = 0.02
    bpy.context.object.data.materials.append(glass)
    bpy.ops.object.light_add(type="AREA", location=(2, -3, 4))
    bpy.context.object.data.energy = 800
    bpy.context.object.data.size = 2
    world = sc.world or bpy.data.worlds.new("w")
    sc.world = world
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.05, 0.07, 0.1, 1)
    # camera 1 dollies, camera 2 is a different angle; a marker cuts to it at frame 13
    bpy.ops.object.camera_add(location=(0, -8, 2), rotation=(math.radians(80), 0, 0))
    cam1 = bpy.context.object
    cam1.name = "Cam1"
    cam1.keyframe_insert("location", frame=1)
    cam1.location = (1.0, -7, 2)
    cam1.keyframe_insert("location", frame=40)
    bpy.ops.object.camera_add(location=(7, -2, 3), rotation=(math.radians(72), 0, math.radians(70)))
    cam2 = bpy.context.object
    cam2.name = "Cam2"
    sc.camera = cam1
    mk = sc.timeline_markers.new("A", frame=1)
    mk.camera = cam1
    mk = sc.timeline_markers.new("B", frame=13)
    mk.camera = cam2
    sc.render.engine = "CYCLES"
    prefs = bpy.context.preferences.addons["cycles"].preferences
    prefs.compute_device_type = "OPTIX"
    prefs.refresh_devices()
    for d in prefs.devices:
        d.use = d.type == "OPTIX"
    sc.cycles.device = "GPU"
    sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage = 640, 360, 100
    sc.frame_start, sc.frame_end, sc.frame_step = 1, 24, 1
    sc.render.fps = 24
    sc.render.image_settings.media_type = "IMAGE"
    sc.render.image_settings.file_format = "PNG"
    sc.view_settings.view_transform = "AgX"
    return sc


def win_ctx():
    win = bpy.context.window_manager.windows[0]
    area = max((a for a in win.screen.areas if a.type == "VIEW_3D"), key=lambda a: a.width * a.height)
    return win, area


def start_op(op, **kw):
    win, area = win_ctx()
    region = next(r for r in area.regions if r.type == "WINDOW")
    with bpy.context.temp_override(window=win, area=area, region=region, scene=win.scene):
        return op("INVOKE_DEFAULT", **kw)


def wait_job(timeout=900, cancel_after=None):
    """generator: wait for the running job to end; cancel_after: frames saved before Esc is simulated"""
    render = importlib.import_module(PKG + ".render")
    t0 = time.time()
    while render.ACTIVE is None and time.time() - t0 < 5:
        yield 0.2
    while render.ACTIVE is not None:
        job = render.ACTIVE
        if cancel_after is not None:
            cur = getattr(job, "cur", job)
            if cur is not None and getattr(cur, "saved", 0) >= cancel_after:
                job.cancel = True
        if time.time() - t0 > timeout:
            job.cancel = True
            log("timeout, cancelling")
        yield 0.5
    yield 1.0


def read_img(path):
    img = bpy.data.images.load(path, check_existing=False)
    w, h = img.size
    px = np.empty(w * h * 4, np.float32)
    img.pixels.foreach_get(px)
    bpy.data.images.remove(img)
    return px.reshape(h, w, 4)


def ffprobe_frames(path):
    exe = shutil.which("ffprobe")
    out = subprocess.run([exe, "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
                          "stream=nb_read_frames,codec_name,width,height", "-of", "json", path],
                         capture_output=True, text=True)
    return json.loads(out.stdout)["streams"][0]


def steps():
    sc = build_scene()
    s = sc.dlss_render_plus
    s.quality = "BALANCED"
    render = importlib.import_module(PKG + ".render")
    rq = importlib.import_module(PKG + ".render_queue")
    yield 2.0
    block = render.blocker(bpy.context, sc, win_ctx()[0])
    check("gui.ready", block is None, str(block))
    if block:
        return

    # --- 1. still
    sc.frame_set(5)
    sc.render.filepath = os.path.join(OUT, "still", "img_")
    t = time.time()
    start_op(bpy.ops.render.dlss_render_plus, animation=False)
    yield from wait_job()
    files = sorted(os.listdir(os.path.join(OUT, "still"))) if os.path.isdir(os.path.join(OUT, "still")) else []
    ok = len(files) == 1
    if ok:
        px = read_img(os.path.join(OUT, "still", files[0]))
        ok = px.shape[:2] == (360, 640) and px[..., :3].mean() > 0.02
        info = f"{files[0]} {px.shape[1]}x{px.shape[0]} mean {px[..., :3].mean():.3f} in {time.time() - t:.1f}s"
    else:
        info = str(files)
    check("gui.still", ok, info)
    save()

    # --- 2. animation with Steady, camera cut at 13
    s.use_steady = True
    sc.render.filepath = os.path.join(OUT, "anim", "f_")
    t = time.time()
    start_op(bpy.ops.render.dlss_render_plus, animation=True)
    yield from wait_job()
    d = os.path.join(OUT, "anim")
    files = sorted(os.listdir(d)) if os.path.isdir(d) else []
    means = [float(read_img(os.path.join(d, f))[..., :3].mean()) for f in files]
    check("gui.anim_frames", len(files) == 24, f"{len(files)} files in {time.time() - t:.1f}s")
    check("gui.anim_no_black", means and min(means) > 0.02, f"min mean {min(means) if means else 0:.3f}")
    # cut: frame 12 -> 13 must change a lot, 13 -> 14 little (no leftovers of the old shot)
    if len(files) == 24:
        a, b, c = (read_img(os.path.join(d, files[i]))[..., :3] for i in (11, 12, 13))
        check("gui.cut_shot_change", float(np.abs(a - b).mean()) > 3 * float(np.abs(b - c).mean()),
              f"12->13 {float(np.abs(a - b).mean()):.4f}, 13->14 {float(np.abs(b - c).mean()):.4f}")
    s.use_steady = False
    save()

    # --- 3. movie
    sc.frame_end = 12
    sc.render.filepath = os.path.join(OUT, "movie", "shot_")
    sc.render.image_settings.media_type = "VIDEO"
    sc.render.ffmpeg.format = "MPEG4"
    sc.render.ffmpeg.codec = "H264"
    sc.render.ffmpeg.constant_rate_factor = "HIGH"
    im = sc.render.image_settings
    before = (im.media_type, im.file_format, im.color_depth, im.color_mode)
    start_op(bpy.ops.render.dlss_render_plus, animation=True)
    yield from wait_job()
    mp4 = os.path.join(OUT, "movie", "shot_0001-0012.mp4")
    info = ffprobe_frames(mp4) if os.path.exists(mp4) else {}
    check("gui.movie", info.get("nb_read_frames") == "12" and info.get("width") == 640,
          f"{info} frames dir left: {os.path.isdir(os.path.join(OUT, 'movie', 'shot_0001-0012_frames'))}")
    after = (im.media_type, im.file_format, im.color_depth, im.color_mode)
    check("gui.movie_settings_restored", before == after, f"{before} -> {after}")
    sc.render.image_settings.media_type = "IMAGE"
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_depth = "8"
    sc.render.image_settings.color_mode = "RGBA"
    save()

    # --- 4. passes
    sc.frame_start, sc.frame_end = 1, 6
    sc.render.filepath = os.path.join(OUT, "plain", "q_")
    start_op(bpy.ops.render.dlss_render_plus, animation=True)
    yield from wait_job()
    im = sc.render.image_settings
    before = (im.media_type, im.file_format, im.color_depth, im.color_mode)
    s.passes = {"Z", "NORMAL", "CRYPTO", "VECTOR"}
    s.pass_samples = 8
    sc.render.filepath = os.path.join(OUT, "passes", "p_")
    t = time.time()
    start_op(bpy.ops.render.dlss_render_plus, animation=True)
    yield from wait_job()
    d = os.path.join(OUT, "passes")
    files = sorted(os.listdir(d)) if os.path.isdir(d) else []
    ok = len(files) == 6 and all(f.endswith(".exr") for f in files)
    info = f"{files} in {time.time() - t:.1f}s"
    if ok:
        import OpenImageIO as oiio
        inp = oiio.ImageInput.open(os.path.join(d, files[0]))
        parts = 0
        while inp.seek_subimage(parts, 0):
            parts += 1
        inp.close()
        info += f" {parts} EXR parts"
        ok = parts >= 5     # Combined, Depth, Normal, Vector and the Cryptomatte layers
    check("gui.passes", ok, info)
    after = (im.media_type, im.file_format, im.color_depth, im.color_mode)
    check("gui.passes_settings_restored", before == after and sc.view_layers[0].use_pass_z is False
          and sc.cycles.samples != 8, f"{before} -> {after}")
    # does the per-frame data render reset DLSS's history? compare frame-to-frame change with/without passes
    import OpenImageIO as oiio
    def combined_as_png(path):
        """The EXR's Combined (scene linear) saved through the scene's color management, like the plain PNGs."""
        inp = oiio.ImageInput.open(path)
        sp = inp.spec()
        a = np.array(inp.read_image(0, 0, 0, sp.nchannels, oiio.FLOAT)).reshape(sp.height, sp.width, sp.nchannels)
        inp.close()
        img = bpy.data.images.new(".cmp", sp.width, sp.height, alpha=True, float_buffer=True)
        img.pixels.foreach_set(np.ascontiguousarray(a[::-1, :, :4], np.float32).ravel())
        png = path[:-4] + "_view.png"
        img.save_render(filepath=png, scene=sc)
        bpy.data.images.remove(img)
        return read_img(png)[..., :3]
    plain = [read_img(os.path.join(OUT, "plain", f"q_{k:04d}.png"))[..., :3] for k in range(1, 7)]
    withp = [combined_as_png(os.path.join(d, f"p_{k:04d}.exr")) for k in range(1, 7)]

    def flick(fr):
        return float(np.mean([np.abs(fr[k + 1] - fr[k]).mean() for k in range(len(fr) - 1)]))
    fp, fw = flick(plain), flick(withp)
    same = float(np.mean([np.abs(p - w).mean() for p, w in zip(plain, withp)]))
    check("gui.passes_keep_dlss_history", fw < 1.3 * fp and same < 0.01,
          f"change per frame: plain {fp:.4f}, with passes {fw:.4f}; plain vs passes {same:.4f}")
    s.passes = set()
    save()

    # --- 5. 4K output in tiles
    sc.frame_set(5)
    sc.render.resolution_x, sc.render.resolution_y = 3840, 2160
    sc.render.filepath = os.path.join(OUT, "tiles", "big_")
    t = time.time()
    start_op(bpy.ops.render.dlss_render_plus, animation=False)
    yield from wait_job()
    d = os.path.join(OUT, "tiles")
    files = sorted(os.listdir(d)) if os.path.isdir(d) else []
    ok, info = False, str(files)
    if files:
        px = read_img(os.path.join(d, files[0]))
        ok = px.shape[:2] == (2160, 3840) and px[..., :3].mean() > 0.02
        info = f"{px.shape[1]}x{px.shape[0]} mean {px[..., :3].mean():.3f} in {time.time() - t:.1f}s"
    check("gui.tiles_4k", ok, info)
    sc.render.resolution_x, sc.render.resolution_y = 640, 360
    save()

    # --- 6. queue: stop after 3 frames, then Resume
    sc.frame_start, sc.frame_end = 1, 8
    sc.render.filepath = os.path.join(OUT, "queue_a", "a_")
    s.queue.clear()
    for cam, rng in ((bpy.data.objects["Cam1"], (1, 8)), (bpy.data.objects["Cam2"], (20, 23))):
        it = s.queue.add()
        it.uid = f"test{len(s.queue)}"
        it.scene, it.camera = sc, cam
        it.use_scene_range = False
        it.frame_start, it.frame_end = rng
    # the second entry renders into the same scene output: give it its own folder through a second scene copy
    sc2 = sc.copy()
    sc2.name = "QueueScene2"
    sc2.render.filepath = os.path.join(OUT, "queue_b", "b_")
    s.queue[1].scene = sc2
    s.queue[1].camera = bpy.data.objects["Cam2"]
    sc2.timeline_markers.clear()
    start_op(bpy.ops.render.dlss_queue_run, resume=False)
    yield from wait_job(cancel_after=3)
    da, db = os.path.join(OUT, "queue_a"), os.path.join(OUT, "queue_b")
    first = sorted(os.listdir(da)) if os.path.isdir(da) else []
    mtimes = {f: os.path.getmtime(os.path.join(da, f)) for f in first}
    check("gui.queue_stopped_early", 1 <= len(first) < 8, f"{len(first)} saved: {first}")
    store = rq.Store()
    check("gui.queue_store", len(store.done("test1")) == len(first), f"store {sorted(store.done('test1'))}")
    yield 1.0
    start_op(bpy.ops.render.dlss_queue_run, resume=True)
    yield from wait_job()
    a = sorted(os.listdir(da)) if os.path.isdir(da) else []
    b = sorted(os.listdir(db)) if os.path.isdir(db) else []
    untouched = all(abs(os.path.getmtime(os.path.join(da, f)) - m) < 1e-6 for f, m in mtimes.items())
    check("gui.queue_resume", len(a) == 8 and len(b) == 4 and untouched,
          f"a {len(a)}, b {len(b)}, earlier frames untouched {untouched}, status {[i.status for i in s.queue]}")
    check("gui.queue_scene_back", bpy.context.window_manager.windows[0].scene == sc)
    check("gui.queue_camera_back", sc.camera is not None)
    save()

    # --- 7. movie in the queue: stop after 4 frames, Resume finishes the movie
    sc.render.resolution_x, sc.render.resolution_y = 640, 360
    sc.render.filepath = os.path.join(OUT, "qmovie", "m_")
    sc.render.image_settings.media_type = "VIDEO"
    sc.render.ffmpeg.format = "MPEG4"
    sc.render.ffmpeg.codec = "H264"
    s.queue.clear()
    it = s.queue.add()
    it.uid = "movie1"
    it.scene = sc
    it.use_scene_range = False
    it.frame_start, it.frame_end = 1, 10
    start_op(bpy.ops.render.dlss_queue_run, resume=False)
    yield from wait_job(cancel_after=4)
    fdir = os.path.join(OUT, "qmovie", "m_0001-0010_frames")
    n_first = len([f for f in os.listdir(fdir) if f.endswith(".png")]) if os.path.isdir(fdir) else 0
    os.remove(os.path.join(fdir, "frame_000002.png")) if n_first >= 2 else None     # a lost frame must come back
    yield 1.0
    start_op(bpy.ops.render.dlss_queue_run, resume=True)
    yield from wait_job()
    mp4 = os.path.join(OUT, "qmovie", "m_0001-0010.mp4")
    info = ffprobe_frames(mp4) if os.path.exists(mp4) else {}
    check("gui.queue_movie_resume", 1 <= n_first < 10 and info.get("nb_read_frames") == "10",
          f"first run saved {n_first}, movie {info}, status {s.queue[0].status}")
    sc.render.image_settings.media_type = "IMAGE"
    save()


def driver():
    gen = steps()

    def tick():
        try:
            return next(gen)
        except StopIteration:
            pass
        except Exception:
            results["gui.exception"] = traceback.format_exc()
            log(traceback.format_exc())
        save()
        log("DONE")
        bpy.ops.wm.quit_blender()
        return None
    bpy.app.timers.register(tick, first_interval=3.0)


driver()
