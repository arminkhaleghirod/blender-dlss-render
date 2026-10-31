"""Render passes: a short Cycles render per frame makes the data passes (depth, normals, cryptomatte...), and the
DLSS picture goes in as their Combined, all in one OpenEXR MultiLayer file.

Blender cannot hand Python the passes of a render directly, so the data render is saved to a temporary multilayer
EXR first and read back with OpenImageIO (bundled with Blender)."""
import glob
import os
import tempfile

import bpy
import numpy as np

# enum id, label, view layer setting(s)
PASSES = [
    ("Z", "Depth", ("use_pass_z",)),
    ("MIST", "Mist", ("use_pass_mist",)),
    ("NORMAL", "Normal", ("use_pass_normal",)),
    ("POSITION", "Position", ("use_pass_position",)),
    ("UV", "UV", ("use_pass_uv",)),
    ("VECTOR", "Vector", ("use_pass_vector",)),
    ("CRYPTO", "Cryptomatte", ("use_pass_cryptomatte_object", "use_pass_cryptomatte_material",
                               "use_pass_cryptomatte_asset")),
    ("ALBEDO", "Albedo", ("use_pass_diffuse_color",)),
    ("AO", "AO", ("use_pass_ambient_occlusion",)),
    ("EMIT", "Emission", ("use_pass_emit",)),
    ("SHADOW", "Shadow Catcher", ("cycles.use_pass_shadow_catcher",)),
]
ENUM_ITEMS = [(k, label, f"Save the {label} pass") for k, label, _ in PASSES]
DEFAULT = {"Z", "NORMAL", "CRYPTO", "AO"}


class PassError(Exception):
    pass


def _target(vl, attr):
    obj = vl
    *path, name = attr.split(".")
    for p in path:
        obj = getattr(obj, p)
    return obj, name


def prepare(job, chosen, samples):
    """Set the scene up for the data render (final-render settings only: the viewport doesn't use them)."""
    try:
        import OpenImageIO  # noqa: F401
    except ImportError:
        raise PassError("Render passes need OpenImageIO, which this Blender build doesn't include.")
    sc, r, c = job.scene, job.scene.render, job.scene.cycles
    vl = job.win.view_layer
    job.pass_layer = vl.name
    for k, _label, attrs in PASSES:
        for attr in attrs:
            obj, name = _target(vl, attr)
            job.keep_safe(obj, name, k in chosen)
    job.keep_safe(vl, "use_pass_combined", True)
    job.keep_safe(c, "samples", samples)
    job.keep_safe(c, "use_adaptive_sampling", False)
    job.keep_safe(c, "use_denoising", False)
    job.keep_safe(r, "use_motion_blur", False)      # the vector pass needs it off, and DLSS has none anyway
    job.keep_safe(r, "use_compositing", False)
    job.keep_safe(r, "use_sequencer", False)
    im = r.image_settings      # the job has remembered these already (render.Job.prepare)
    if hasattr(im, "media_type"):
        im.media_type = "MULTI_LAYER_IMAGE"
    im.file_format = "OPEN_EXR_MULTILAYER"
    im.color_depth = "32"
    im.exr_codec = "ZIP"
    job.pass_tmp = os.path.join(tempfile.gettempdir(), f"dlss_render_plus_passes_{os.getpid()}.exr")


def render(job):
    """The data render of the current frame, written by Blender itself to job.pass_tmp (saving the Render Result
    afterwards keeps only Combined)."""
    r = job.scene.render
    old = r.filepath, r.use_file_extension, r.use_overwrite
    base = os.path.splitext(job.pass_tmp)[0] + "_"
    r.filepath, r.use_file_extension, r.use_overwrite = base, True, True
    # a still is written with or without the frame number depending on the Blender version: take what appears
    for old_file in glob.glob(glob.escape(base) + "*.exr"):
        os.remove(old_file)
    try:
        override = {"scene": job.scene}
        if job.win:
            override["window"] = job.win
        with bpy.context.temp_override(**override):
            bpy.ops.render.render(write_still=True, layer=job.pass_layer, scene=job.scene.name)
    finally:
        r.filepath, r.use_file_extension, r.use_overwrite = old
    written = glob.glob(glob.escape(base) + "*.exr")
    if not written:
        raise PassError("The data render for the passes wrote no file.")
    os.replace(max(written, key=os.path.getmtime), job.pass_tmp)


def merge(tmp, rgba, path, layer):
    """Write tmp to path with the layer's Combined replaced by rgba ((H, W, 4), rows bottom-up). Blender writes one
    EXR part per pass; every part is copied as it is, with its own metadata (cryptomatte manifests)."""
    import OpenImageIO as oiio
    inp = oiio.ImageInput.open(tmp)
    if inp is None:
        raise PassError(f"Could not read the passes: {oiio.geterror()}")
    parts = []
    try:
        n = 0
        while inp.seek_subimage(n, 0):
            spec = oiio.ImageSpec(inp.spec())
            data = inp.read_image(n, 0, 0, spec.nchannels, oiio.FLOAT)
            if data is None:
                raise PassError(f"Could not read the passes: {inp.geterror()}")
            parts.append((spec, np.array(data, dtype=np.float32).reshape(spec.height, spec.width, spec.nchannels)))
            n += 1
    finally:
        inp.close()

    top_down = rgba[::-1]
    replaced = False
    for want in (f"{layer}.Combined.", None):
        for spec, data in parts:
            names = list(spec.channelnames)
            prefix = want or next((c[:-1] for c in names if c.endswith(".Combined.R")), None)
            if prefix is None or prefix + "R" not in names:
                continue
            if (spec.height, spec.width) != rgba.shape[:2]:
                raise PassError(f"The passes are {spec.width} x {spec.height}, "
                                f"the picture {rgba.shape[1]} x {rgba.shape[0]}.")
            for i, ch in enumerate("RGBA"):
                if prefix + ch in names:
                    data[..., names.index(prefix + ch)] = top_down[..., i]
            replaced = True
            break
        if replaced:
            break
    if not replaced:
        raise PassError("The passes file has no Combined layer.")

    specs = []
    for spec, _data in parts:
        spec.set_format(oiio.FLOAT)
        spec.attribute("compression", "zip")
        specs.append(spec)
    # write next to the target first, so a crash never leaves a half-written frame behind
    tmp_out = os.path.splitext(path)[0] + ".part.exr"
    out = oiio.ImageOutput.create(tmp_out)
    if out is None:
        raise PassError(f"Could not write {path}: {oiio.geterror()}")
    ok = out.open(tmp_out, specs)
    for k, (spec, data) in enumerate(parts):
        if ok and k:
            ok = out.open(tmp_out, spec, "AppendSubimage")
        ok = ok and out.write_image(data)
    msg = out.geterror()
    out.close()
    if not ok:
        if os.path.exists(tmp_out):
            os.remove(tmp_out)
        raise PassError(f"Could not write {path}: {msg}")
    os.replace(tmp_out, path)
