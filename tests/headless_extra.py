"""More headless checks: save/reload, disable/enable, living next to the original add-on.
Run: blender -b --python headless_extra.py (the original DLSS Render may be installed as bl_ext.user_default.dlss_render)."""
import importlib
import json
import os
import tempfile
import traceback

import addon_utils
import bpy

OUT = os.path.join(os.environ.get("DLSSPLUS_TEST_DIR", os.path.join(tempfile.gettempdir(), "dlssplus")), "out_extra")
os.makedirs(OUT, exist_ok=True)
PKG = "bl_ext.user_default.dlss_render_plus"
ORIG = "bl_ext.user_default.dlss_render"
results = {}


def check(name, cond, info=""):
    results[name] = ("PASS" if cond else "FAIL") + (f" {info}" if info else "")


def run(name, fn):
    try:
        fn()
    except Exception:
        results[name] = "ERROR " + traceback.format_exc().splitlines()[-1]
        traceback.print_exc()


def t_toggle():
    addon_utils.enable(PKG, default_set=True)
    for _ in range(3):
        addon_utils.disable(PKG, default_set=True)
        gone = not hasattr(bpy.types.Scene, "dlss_render_plus") and not hasattr(bpy.types, "DLSS_UL_queue")
        addon_utils.enable(PKG, default_set=True)
    check("extra.disable_enable_x3", gone and hasattr(bpy.types.Scene, "dlss_render_plus"))


def t_save_reload():
    sc = bpy.context.scene
    s = sc.dlss_render_plus
    s.quality, s.use_steady, s.steady_strength = "HIGH", True, 0.45
    s.passes = {"Z", "CRYPTO"}
    sc2 = bpy.data.scenes.new("Other")
    bpy.ops.object.camera_add()
    cam = bpy.context.object
    s.queue.clear()
    it = s.queue.add()
    it.uid, it.scene, it.camera, it.use_scene_range, it.frame_start, it.frame_end = "u1", sc2, cam, False, 3, 9
    path = os.path.join(OUT, "reload_test.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path)
    bpy.ops.wm.read_homefile(use_empty=True)
    bpy.ops.wm.open_mainfile(filepath=path)
    sc = bpy.context.scene
    s = sc.dlss_render_plus
    it = s.queue[0] if len(s.queue) else None
    check("extra.save_reload", it is not None and it.scene and it.scene.name == "Other" and it.camera is not None
          and (it.frame_start, it.frame_end) == (3, 9) and s.quality == "HIGH" and abs(s.steady_strength - 0.45) < 1e-6
          and s.passes == {"Z", "CRYPTO"}, f"queue {len(s.queue)}, passes {s.passes}")
    rq = importlib.import_module(PKG + ".render_queue")
    check("extra.store_next_to_blend", rq.store_path() == os.path.join(OUT, "reload_test.dlss_queue.json"),
          rq.store_path())


def t_original_side_by_side():
    mods = {m.__name__ for m in addon_utils.modules()}
    if ORIG not in mods:
        check("extra.next_to_original", False, "original not installed in the test config")
        return
    addon_utils.enable(ORIG, default_set=True)
    addon_utils.enable(PKG, default_set=True)
    both = hasattr(bpy.types.Scene, "dlss_render") and hasattr(bpy.types.Scene, "dlss_render_plus") \
        and hasattr(bpy.ops.render, "dlss_render") and hasattr(bpy.ops.render, "dlss_render_plus")
    addon_utils.disable(ORIG, default_set=True)
    still = hasattr(bpy.types.Scene, "dlss_render_plus") and hasattr(bpy.ops.render, "dlss_render_plus")
    check("extra.next_to_original", both and still, f"both registered {both}, ours intact after disabling original {still}")


def t_blocker_messages():
    render = importlib.import_module(PKG + ".render")
    sc = bpy.context.scene
    sc.render.engine = "BLENDER_EEVEE" if "BLENDER_EEVEE" in [e.identifier for e in sc.render.bl_rna.properties["engine"].enum_items] else "BLENDER_WORKBENCH"
    b = render.blocker(bpy.context, sc, None)
    check("extra.blocker_engine", b is not None and b[0] == "ENGINE", str(b))
    sc.render.engine = "CYCLES"


def t_movie_paths():
    movie = importlib.import_module(PKG + ".movie")
    sc = bpy.context.scene
    sc.render.ffmpeg.format = "MPEG4"
    sc.render.use_file_extension = True
    cases = {r"C:\a#b\out_": r"C:\a#b\out_0001-0250.mp4",
             r"C:\shots\shot_###": r"C:\shots\shot_001-250.mp4",
             r"C:\shots\shot_##_v2": r"C:\shots\shot_01-250_v2.mp4",
             r"C:\shots\plain": r"C:\shots\plain0001-0250.mp4"}
    got = {}
    for src, want in cases.items():
        sc.render.filepath = src
        got[src] = movie.movie_path(sc, 1, 250)
    check("extra.movie_path_hashes", all(os.path.normcase(got[k]) == os.path.normcase(v) for k, v in cases.items()),
          str(got))


def t_relative_output_blocked():
    render = importlib.import_module(PKG + ".render")
    bpy.ops.wm.read_homefile(use_empty=False)       # unsaved file
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.render.filepath = "//renders/x_"
    prefs = bpy.context.preferences.addons["cycles"].preferences
    prefs.compute_device_type = "OPTIX"
    prefs.refresh_devices()
    for d in prefs.devices:
        d.use = d.type == "OPTIX"
    sc.cycles.device = "GPU"
    b = render.blocker(bpy.context, sc, None)
    check("extra.relative_output_blocked", b is not None and b[0] in {"OUTPUT", "DLSS"}, str(b))


def t_queue_copies_and_clash():
    rq = importlib.import_module(PKG + ".render_queue")
    render = importlib.import_module(PKG + ".render")
    sc = bpy.context.scene
    sc.render.filepath = os.path.join(OUT, "clash", "c_")
    s = sc.dlss_render_plus
    s.queue.clear()
    it = s.queue.add()
    it.uid, it.scene = "same", sc
    copy = sc.copy()                 # like Scene > Full Copy: the queue entry comes along with the same id
    fake = type("Ctx", (), {})()
    fake.window = type("W", (), {"scene": sc})()
    # two entries, same scene, same output: refused before anything renders
    it2 = s.queue.add()
    it2.uid, it2.scene = "other", sc
    try:
        rq.QueueJob(fake, resume=False)
        clash = "not refused"
    except render.JobError as e:
        clash = str(e)
    check("extra.queue_same_output_refused", "same files" in clash, clash)
    # this queue's entry no longer shares its id (and so its progress) with the copied scene's
    check("extra.queue_copy_gets_own_id", copy.dlss_render_plus.queue[0].uid == "same" and s.queue[0].uid != "same",
          f"copy {copy.dlss_render_plus.queue[0].uid}, original {s.queue[0].uid}")
    # with its own output the second entry is accepted
    it2.output = os.path.join(OUT, "clash", "other_")
    try:
        rq.QueueJob(fake, resume=False)
        accepted = "accepted"
    except render.JobError as e:
        accepted = str(e)
    check("extra.queue_own_output_accepted", accepted == "accepted", accepted)
    bpy.data.scenes.remove(copy)


def t_render_queue_clears_movie_frames():
    rq = importlib.import_module(PKG + ".render_queue")
    movie = importlib.import_module(PKG + ".movie")
    sc = bpy.context.scene
    sc.render.filepath = os.path.join(OUT, "mclear", "m_")
    sc.render.image_settings.media_type = "VIDEO"
    s = sc.dlss_render_plus
    s.queue.clear()
    it = s.queue.add()
    it.uid, it.scene, it.use_scene_range, it.frame_start, it.frame_end = "mv", sc, False, 1, 3
    folder = movie.frames_dir(sc, [1, 2, 3])
    os.makedirs(folder, exist_ok=True)
    open(movie.frame_file(folder, 1), "w").close()
    fake = type("Ctx", (), {})()
    fake.window = type("W", (), {"scene": sc})()
    job = rq.QueueJob(fake, resume=True)
    resume_todo = job.plan[0][5]
    job = rq.QueueJob(fake, resume=False)
    fresh_todo = job.plan[0][5]
    check("extra.render_queue_clears_old_frames", resume_todo == [2, 3] and fresh_todo == [1, 2, 3]
          and not os.path.exists(movie.frame_file(folder, 1)), f"resume {resume_todo}, fresh {fresh_todo}")
    sc.render.image_settings.media_type = "IMAGE"


def t_overwrite_off():
    render = importlib.import_module(PKG + ".render")
    sc = bpy.context.scene
    sc.render.filepath = os.path.join(OUT, "ow", "o_")
    sc.render.image_settings.file_format = "PNG"
    sc.render.use_overwrite = False
    os.makedirs(os.path.join(OUT, "ow"), exist_ok=True)
    open(os.path.join(OUT, "ow", "o_0002.png"), "w").close()
    job = render.Job.__new__(render.Job)       # only the frame selection, no viewport
    job.scene, job.passes = sc, set()
    keep = [f for f in (1, 2, 3) if not os.path.exists(job.output_path(f))]
    check("extra.overwrite_off_keeps_files", keep == [1, 3], str(keep))
    sc.render.use_overwrite = True


for name, fn in [("toggle", t_toggle), ("side", t_original_side_by_side), ("blocker", t_blocker_messages),
                 ("reload", t_save_reload), ("movie_paths", t_movie_paths), ("overwrite", t_overwrite_off),
                 ("mclear", t_render_queue_clears_movie_frames), ("clash", t_queue_copies_and_clash),
                 ("relative", t_relative_output_blocked)]:
    run(name, fn)
with open(os.path.join(OUT, "results.json"), "w") as fh:
    json.dump(results, fh, indent=1)
print("=== EXTRA ===")
for k, v in results.items():
    print(f"{k}: {v}")
