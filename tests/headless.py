"""Headless tests for DLSS Render Plus (everything except the DLSS viewport loop)."""
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from types import SimpleNamespace

import bpy
import numpy as np

OUT = os.path.join(os.environ.get("DLSSPLUS_TEST_DIR", os.path.join(tempfile.gettempdir(), "dlssplus")), "out_headless")
shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT)
results = {}


def check(name, cond, info=""):
    results[name] = ("PASS" if cond else "FAIL") + (f" {info}" if info else "")


def run(name, fn):
    try:
        fn()
    except Exception:
        results[name] = "ERROR " + traceback.format_exc().splitlines()[-1]
        traceback.print_exc()


PKG = "bl_ext.user_default.dlss_render_plus"
import addon_utils
addon_utils.enable(PKG, default_set=True)
pkg = importlib.import_module(PKG)
steady = importlib.import_module(PKG + ".steady")
movie = importlib.import_module(PKG + ".movie")
passes = importlib.import_module(PKG + ".passes")
rq = importlib.import_module(PKG + ".render_queue")
render = importlib.import_module(PKG + ".render")


def t_register():
    check("register.scene_prop", hasattr(bpy.types.Scene, "dlss_render_plus"))
    for op in ("dlss_render_plus", "dlss_render_plus_setup", "dlss_queue_run", "dlss_queue_add"):
        check(f"register.op.{op}", hasattr(bpy.ops.render, op))
    s = bpy.context.scene.dlss_render_plus
    s.passes = {"Z", "CRYPTO"}
    check("register.passes_enum", s.passes == {"Z", "CRYPTO"})
    s.passes = set()


def t_steady():
    rng = np.random.default_rng(1)
    H, W = 120, 160
    base = np.zeros((H, W, 4), np.float32)
    base[..., :3] = 0.5
    base[..., 3] = 1.0
    # 1) static grainy glass: flicker = std over time of each pixel
    st = steady.Steady(0.6)
    raw, out = [], []
    for i in range(30):
        f = base.copy()
        f[..., :3] *= 1.0 + rng.normal(0, 0.15, (H, W, 1)).astype(np.float32)
        raw.append(f[..., 0])
        out.append(st.apply(f)[..., 0])
    fr = np.std(np.stack(raw[5:]), axis=0).mean()
    fo = np.std(np.stack(out[5:]), axis=0).mean()
    check("steady.flicker_reduction", fo < 0.6 * fr, f"raw {fr:.4f} -> {fo:.4f} ({100 * (1 - fo / fr):.0f}% less)")
    # 1b) blotchy grain (DLSS leaves soft, few-pixel blobs rather than single-pixel noise)
    st = steady.Steady(0.6)
    raw, out = [], []
    for i in range(30):
        n = rng.normal(0, 0.15, (H // 4, W // 4)).astype(np.float32)
        n = np.kron(n, np.ones((4, 4), np.float32))[..., None]
        f = base.copy()
        f[..., :3] *= 1.0 + n
        raw.append(f[..., 0])
        out.append(st.apply(f)[..., 0])
    fr = np.std(np.stack(raw[5:]), axis=0).mean()
    fo = np.std(np.stack(out[5:]), axis=0).mean()
    check("steady.flicker_blotchy", fo < 0.8 * fr, f"raw {fr:.4f} -> {fo:.4f} ({100 * (1 - fo / fr):.0f}% less)")
    # 2) moving bright square on dark: no ghost trail
    st = steady.Steady(0.6)
    ghost = 0.0
    for i in range(20):
        f = np.zeros((H, W, 4), np.float32)
        f[..., 3] = 1
        x = 10 + 6 * i
        f[40:70, x:x + 20, :3] = 4.0
        o = st.apply(f)
        if i > 0:
            behind = o[40:70, max(0, x - 6):x, :3]
            ghost = max(ghost, float(behind.max()))
    check("steady.no_ghost", ghost < 0.05, f"max value left behind {ghost:.4f}")
    # 3) cut: completely different frame is passed through untouched
    st = steady.Steady(0.6)
    a = base.copy()
    st.apply(a)
    b = base.copy()
    b[..., :3] = 3.0
    check("steady.cut_reset", np.allclose(st.apply(b), b))
    # 4) speed at 4K
    import time
    big = np.random.default_rng(2).random((2160, 3840, 4), dtype=np.float32)
    st = steady.Steady(0.6)
    st.apply(big)
    t0 = time.perf_counter()
    st.apply(big * 1.01)
    dt = time.perf_counter() - t0
    check("steady.4k_speed", dt < 3.0, f"{dt:.2f} s per 4K frame")


def make_frames(mv, frames, W=320, H=180, alpha=False):
    os.makedirs(mv.dir, exist_ok=True)
    sc = bpy.context.scene
    im = sc.render.image_settings
    im.media_type = "IMAGE"
    im.file_format = "PNG"
    im.color_depth = "16"
    im.color_mode = "RGBA" if alpha else "RGB"
    img = bpy.data.images.new("t", W, H, alpha=True, float_buffer=True)
    for f in frames:
        px = np.zeros((H, W, 4), np.float32)
        px[..., 0] = (f % 10) / 10
        px[..., 1] = np.linspace(0, 1, W)[None, :]
        px[..., 3] = 0.5 if alpha else 1.0
        img.pixels.foreach_set(px.ravel())
        img.save_render(filepath=mv.frame_file(f), scene=sc)
    bpy.data.images.remove(img)


def probe(path):
    ffprobe = os.path.join(os.path.dirname(movie.find_ffmpeg()), "ffprobe.exe")
    out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
                          "stream=codec_name,nb_read_frames,r_frame_rate,pix_fmt,width,height", "-of", "json", path],
                         capture_output=True, text=True)
    return json.loads(out.stdout)["streams"][0]


def t_movie():
    sc = bpy.context.scene
    r = sc.render
    r.fps, r.fps_base = 24, 1.0
    cases = [("MPEG4", "H264", "MEDIUM", False, "h264", "yuv420p"),
             ("QUICKTIME", "PRORES", "NONE", False, "prores", "yuv422p10le"),
             ("QUICKTIME", "PRORES", "NONE", True, "prores", "yuv422p10le"),  # Blender 5.3 offers no RGBA for ProRes,
             ("MKV", "FFV1", "NONE", False, "ffv1", "gbrp16le"),
             ("WEBM", "WEBM", "HIGH", True, "vp9", None),
             ("MKV", "H265", "HIGH", False, "hevc", "yuv420p"),
             ("MKV", "AV1", "HIGH", False, "av1", None)]
    for fmt, codec, crf, alpha, want, pix in cases:
        name = f"{fmt}_{codec}_{'a' if alpha else 'o'}"
        try:
            r.filepath = os.path.join(OUT, name, "shot_")
            r.film_transparent = alpha
            r.image_settings.media_type = "VIDEO"
            r.ffmpeg.format = fmt
            r.ffmpeg.codec = codec
            r.ffmpeg.constant_rate_factor = crf
            try:
                r.image_settings.color_mode = "RGBA" if alpha else "RGB"
            except TypeError:
                r.image_settings.color_mode = "RGB"   # Blender allows no alpha for this codec
            frames = list(range(1, 25, 2))     # step 2: frame numbers have gaps
            mv = movie.Movie(sc, frames, movie.find_ffmpeg())
            make_frames(mv, frames, alpha=alpha)
            check(f"movie.{name}.missing_none", mv.missing() == [])
            p = mv.start()
            p.wait(120)
            mv.finish(keep_frames=False)
            info = probe(mv.path)
            ok = info["codec_name"] == want and int(info["nb_read_frames"]) == len(frames) \
                and (pix is None or info["pix_fmt"] == pix)
            check(f"movie.{name}", ok and not os.path.isdir(mv.dir),
                  f"{os.path.basename(mv.path)} {info}")
        except Exception as e:
            log = getattr(locals().get("mv"), "log_path", "")
            txt = open(log).read()[-300:] if log and os.path.exists(log) else ""
            results[f"movie.{name}"] = f"ERROR {e} {txt}"
    # path naming like Blender's
    r.filepath = "//renders/shot_####"
    r.ffmpeg.format = "MPEG4"
    p = movie.movie_path(sc, 1, 250)
    check("movie.path_naming", p.endswith(os.path.normpath("renders/shot_0001-0250.mp4")), p)
    # missing ffmpeg
    try:
        movie.Movie(sc, [1], None)
        check("movie.no_ffmpeg_error", False)
    except movie.MovieError:
        check("movie.no_ffmpeg_error", True)
    r.image_settings.media_type = "IMAGE"
    r.film_transparent = False


def build_scene():
    sc = bpy.context.scene
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
    bpy.ops.mesh.primitive_uv_sphere_add(location=(2.2, 0, 0))
    bpy.ops.mesh.primitive_plane_add(size=20, location=(0, 0, -1))
    bpy.ops.object.light_add(type="SUN", location=(3, -3, 5))
    bpy.ops.object.camera_add(location=(7, -7, 4.5), rotation=(1.1, 0, 0.785))
    sc.camera = bpy.context.object
    sc.render.engine = "CYCLES"
    p = bpy.context.preferences.addons["cycles"].preferences
    p.compute_device_type = "OPTIX"
    for d in p.get_devices_for_type("OPTIX"):
        d.use = d.type == "OPTIX"
    sc.cycles.device = "GPU"
    sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage = 320, 180, 100
    return sc


def t_passes():
    sc = build_scene()
    kept = []

    def keep_safe(obj, attr, value):
        kept.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)
    job = SimpleNamespace(scene=sc, win=SimpleNamespace(view_layer=sc.view_layers[0]) , keep_safe=keep_safe)
    passes.prepare(job, {"Z", "NORMAL", "CRYPTO", "AO", "VECTOR"}, 4)
    job.win = None
    passes.render(job)
    rgba = np.zeros((180, 320, 4), np.float32)
    rgba[..., 0] = np.linspace(0, 1, 180)[:, None]    # rows bottom-up: red grows towards the top
    rgba[..., 3] = 1.0
    path = os.path.join(OUT, "passes_0001.exr")
    passes.merge(job.pass_tmp, rgba, path, job.pass_layer)
    import OpenImageIO as oiio
    inp = oiio.ImageInput.open(path)
    names, chans, meta, nparts = [], {}, [], 0
    while inp.seek_subimage(nparts, 0):
        sp = inp.spec()
        d = np.array(inp.read_image(nparts, 0, 0, sp.nchannels, oiio.FLOAT)).reshape(sp.height, sp.width, sp.nchannels)
        for k, c in enumerate(sp.channelnames):
            names.append(c)
            chans[c] = d[..., k]
        meta += [a.name for a in sp.extra_attribs if "cryptomatte" in a.name.lower()]
        nparts += 1
    inp.close()
    data = np.stack([chans[c] for c in names], -1)
    results["passes.parts"] = str(nparts)
    r_idx = names.index(f"{job.pass_layer}.Combined.R")
    top, bottom = data[0, 0, r_idx], data[-1, 0, r_idx]
    check("passes.combined_is_dlss_and_flipped", abs(top - 1.0) < 1e-4 and abs(bottom) < 1e-4,
          f"top {top:.3f} bottom {bottom:.3f}")
    want = ["Depth.Z", "Normal.X", "CryptoObject00.r", "Ambient Occlusion.R", "Vector.X"]
    have = {w: any(n.endswith(w) for n in names) for w in want}
    check("passes.channels", all(have.values()), str(have))
    results["passes.names"] = str(len(names)) + " channels"
    check("passes.crypto_manifest_kept", any("manifest" in m for m in meta), str(meta[:4]))
    z = data[..., names.index([n for n in names if n.endswith("Depth.Z")][0])]
    check("passes.depth_sane", 3 < float(np.median(z)) < 100, f"median depth {float(np.median(z)):.2f}")
    check("passes.no_part_file", not os.path.exists(os.path.splitext(path)[0] + ".part.exr"))
    for obj, attr, v in reversed(kept):
        setattr(obj, attr, v)


def t_store():
    path = os.path.join(OUT, "q.json")
    s = rq.Store(path)
    s.mark("a", 1)
    s.mark("a", 2)
    s.mark("a", 2)
    s2 = rq.Store(path)
    check("store.persist", s2.done("a") == {1, 2} and not s2.complete("a"))
    s2.finish("a")
    check("store.complete", rq.Store(path).complete("a"))
    s2.clear()
    check("store.clear", rq.Store(path).done("a") == set())
    sc = bpy.context.scene
    it = sc.dlss_render_plus.queue.add()
    it.scene = sc
    sc.frame_start, sc.frame_end, sc.frame_step = 1, 10, 3
    check("queue.frames_scene_range", rq.item_frames(it, sc) == [1, 4, 7, 10])
    sc.frame_step = 1
    it.use_scene_range, it.frame_start, it.frame_end = False, 5, 6
    check("queue.frames_own_range", rq.item_frames(it, sc) == [5, 6])
    it.animation = False
    check("queue.frames_still", rq.item_frames(it, sc) == [5])
    sc.frame_step = 1
    sc.dlss_render_plus.queue.clear()


for name, fn in [("register", t_register), ("steady", t_steady), ("movie", t_movie), ("passes", t_passes),
                 ("store", t_store)]:
    run(name, fn)

with open(os.path.join(OUT, "results.json"), "w") as fh:
    json.dump(results, fh, indent=1)
print("=== RESULTS ===")
for k, v in results.items():
    print(f"{k}: {v}")
