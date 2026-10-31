"""The render job: drives the Cycles viewport (DLSS denoiser) as a renderer and saves what it shows.

A Python add-on cannot reach the final render's denoiser, so the job puts a 3D viewport in camera view at one output
pixel per screen pixel, steps the frames and lets DLSS settle. The picture is read straight from the viewport's own
buffer (scene-linear float with the render's alpha), so it is saved through Blender's normal colour management and
file settings, like a normal render. Outputs bigger than the viewport are rendered as tiles by panning the zoomed
camera view.
"""
import math
import os
import sys
import time
import traceback

import bpy
import gpu
import numpy as np
from bpy_extras.view3d_utils import location_3d_to_region_2d

from . import movie as movie_mod
from . import passes as passes_mod
from .steady import Steady

OVERLAP = 64            # least shared pixels between neighbouring tiles: a hard seam shows in glass
FEATHER = 48            # width of the blend across a tile seam
MIN_ROOM = 256          # smallest viewport worth rendering in
# viewport refreshes (the viewport's own "Sample" counter) each frame gets before it is saved
QUALITY = {"DRAFT": 8, "BALANCED": 24, "HIGH": 64}
# DLSS shows its picture half a pixel of its own (lower) render resolution to the right and up of where a normal
# render puts it. These are the render scales of the upscale modes; the camera view is nudged the other way.
UPSCALE = {"NONE": 1.0, "QUALITY": 2.0 / 3.0, "BALANCED": 0.58, "PERFORMANCE": 0.5, "ULTRA_PERFORMANCE": 1.0 / 3.0}
PANELS = ("show_region_header", "show_region_tool_header", "show_region_toolbar", "show_region_ui")
RESTART_EVERY = 50      # frames between fresh starts of the viewport render in an animation (see run)
PLAY_IN = 8             # frames stepped through, unsaved, before the first frame and after each fresh start
CUT_ANGLE = math.radians(25)    # camera turn between two frames that counts as a cut (DLSS history is dropped)
CUT_MOVE = 6.0          # camera move this many times its recent average per frame counts as a cut too
ACTIVE = None           # the running job, if any


class JobError(Exception):
    pass


class Cancelled(Exception):
    pass


def addon_prefs():
    addon = bpy.context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def keep_awake(on):
    """Keep Windows (and the screen: the viewport has to keep drawing) awake while rendering."""
    if sys.platform != "win32":
        return
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED if on else 0)
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


def chime():
    try:
        if sys.platform == "win32":
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        else:
            sys.stdout.write("\a")
            sys.stdout.flush()
    except Exception:
        pass


def hms(seconds):
    s = int(round(seconds))
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


def find_view(window, prefer=None):
    """Largest 3D viewport of the window's screen: (area, region, space)."""
    areas = [a for a in window.screen.areas if a.type == "VIEW_3D"]
    if not areas:
        return None
    area = prefer if prefer in areas else max(areas, key=lambda a: a.width * a.height)
    return area, next(r for r in area.regions if r.type == "WINDOW"), area.spaces.active


def output_size(scene):
    r = scene.render
    return r.resolution_x * r.resolution_percentage // 100, r.resolution_y * r.resolution_percentage // 100


def tile_starts(out, room):
    """Where each tile starts along one axis (output pixels), spread evenly. One tile if the output fits."""
    if out <= room:
        return [0]
    n = math.ceil((out - OVERLAP) / (room - OVERLAP))
    return [round(i * (out - room) / (n - 1)) for i in range(n)]


def free_name(path):
    """A single image never replaces an earlier one: name_0001.png, then name_0001_2.png, name_0001_3.png...
    (An animation does write over its own frames, like Blender's.)"""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{root}_{n}{ext}"):
        n += 1
    return f"{root}_{n}{ext}"


def room_limit():
    """Debug: pretend the viewport is no bigger than this (set DLSS_RENDER_MAX_REGION=1920x1080)."""
    v = os.environ.get("DLSS_RENDER_MAX_REGION", "").lower()
    return tuple(int(n) for n in v.split("x")) if "x" in v else None


def cycles_prefs(context):
    addon = context.preferences.addons.get("cycles")
    return addon.preferences if addon else None


def nvidia_backend(context, prefs):
    """The Cycles backend DLSS can run on here: OptiX, else CUDA, else None."""
    types = [t[0] for t in prefs.get_device_types(context)]
    return "OPTIX" if "OPTIX" in types else "CUDA" if "CUDA" in types else None


def blocker(context, scene=None, window=None):
    """The first thing that keeps a DLSS render from starting, or None when all is ready:
    (key, message, label of the one-click fix or None)."""
    sc = scene or context.scene
    win = window or context.window
    prefs = cycles_prefs(context)
    if prefs is None or not hasattr(sc, "cycles"):
        return "ADDON", "Cycles is turned off. Enable the Cycles add-on in Preferences.", None
    if sc.render.engine != "CYCLES":
        return "ENGINE", "DLSS renders with Cycles.", "Switch to Cycles"
    if nvidia_backend(context, prefs) is None:
        return "CARD", "DLSS needs an NVIDIA RTX graphics card.", None
    if prefs.compute_device_type not in {"OPTIX", "CUDA"} or not prefs.has_active_device():
        return "PREFS", "No graphics card is turned on for Cycles.", "Turn On Graphics Card"
    if sc.cycles.device != "GPU":
        return "DEVICE", "Cycles is set to render on the CPU.", "Use GPU Compute"
    if not prefs.has_dlss_gpu_devices():
        return "DLSS", "DLSS is not available here. It needs an NVIDIA RTX card and driver 590 or newer.", None
    if sc.camera is None:
        return "CAMERA", "The scene has no camera.", None
    if abs(sc.render.pixel_aspect_x - sc.render.pixel_aspect_y) > 1e-6:
        return "ASPECT", "Non-square pixel aspect is not supported.", None
    s = sc.dlss_render_plus
    if s.passes and sc.render.is_movie_format:
        return "PASSES", "Render passes are saved as EXR frames: choose an image output, not a movie.", None
    if win is None or not find_view(win):
        return "VIEW", "Open a 3D Viewport in this window.", None
    return None


class Job:
    def __init__(self, context, animation, scene=None, window=None, frames=None, all_frames=None, on_frame=None):
        """frames: the frames to render now (default: the scene's range, or the current frame for a still).
        all_frames: every frame of the shot, when frames is only what is left of it (Resume): a movie is made from
        all of them. on_frame(frame) is called after each saved frame."""
        self.win = window or context.window
        self.scene = scene or context.scene
        self.animation = animation
        self.cancel = False
        self.finished = False
        self.level, self.message = "INFO", ""
        self.kept = []
        self.unhidden = []          # objects shown for the render that were hidden in the viewport
        self.handler = None
        self.image = None
        self.fullscreen = False
        self.area = None
        self.draws = 0
        self.grab_rect = None       # set to (x, y, w, h) to have the next draw read the picture there
        self.watch_rect = None      # the part of the viewport whose picture is watched for new samples
        self.watch_mark = None
        self.pictures = 0           # distinct pictures seen there
        self.changed_at = time.perf_counter()
        self.sample_time = 0.0      # seconds between the last two
        self.quiet_scene = False    # the last frame's picture stopped changing after its first sample
        self.grabbed = None
        self.wait = None
        self.done_frames = 0
        self.total = 0
        self.last_path = ""
        self.t0 = time.perf_counter()
        self.debug = bool(os.environ.get("DLSS_RENDER_DEBUG"))
        self.log = print if self.debug else (lambda *a: None)

        self.saved = 0              # frames saved
        self.saved_at = []          # when each was saved, for the time left
        self.cur_frame = None
        self.phase = "Starting"
        self.on_frame = on_frame
        self.movie = None
        self.cam_last = None        # (camera, matrix) of the last rendered frame, for cut detection
        self.cam_moves = []

        sc = self.scene
        block = blocker(bpy.context, sc, self.win)
        if block:
            raise JobError(block[1])
        self.area0 = find_view(self.win, context.area if context else None)[0]
        s = sc.dlss_render_plus
        self.samples = s.samples if s.quality == "CUSTOM" else QUALITY[s.quality]
        self.W, self.H = output_size(sc)
        if frames is None:
            frames = list(range(sc.frame_start, sc.frame_end + 1, sc.frame_step)) if animation else [sc.frame_current]
        self.frames = list(frames)
        self.all_frames = list(all_frames) if all_frames is not None else list(self.frames)
        if not self.all_frames:
            raise JobError("No frames to render.")
        self.frame0 = sc.frame_current
        self.passes = set(s.passes)
        self.pass_samples = s.pass_samples
        self.steady = Steady(s.steady_strength) if s.use_steady and animation else None
        self.keep_frames = s.keep_movie_frames
        if sc.render.is_movie_format:
            if not animation:
                raise JobError("The output is a movie: render an animation, or choose an image format for a still.")
            prefs = addon_prefs()
            try:
                self.movie = movie_mod.Movie(sc, self.all_frames, movie_mod.find_ffmpeg(prefs.ffmpeg_path if prefs else ""))
            except movie_mod.MovieError as e:
                raise JobError(str(e))
        self.gen = self.run()

    # ------------------------------------------------------------------ small helpers

    def keep(self, obj, attr, value=None):
        """Remember obj.attr so restore() can put it back; optionally set a new value."""
        old = getattr(obj, attr)
        self.kept.append((obj, attr, tuple(old) if hasattr(old, "__len__") and not isinstance(old, str) else old))
        if value is not None:
            setattr(obj, attr, value)

    def view(self):
        return find_view(self.win, self.area)

    def seconds_per_frame(self):
        t = self.saved_at[-11:]
        return (t[-1] - t[0]) / (len(t) - 1) if len(t) > 1 else None

    def frames_left(self):
        return len(self.frames) - self.saved

    def status(self):
        """One line for the status bar."""
        if self.phase != "Rendering":
            return f"DLSS Render: {self.phase}"
        n = len(self.frames)
        text = f"DLSS Render: frame {self.cur_frame} ({self.saved + 1} of {n}), {self.samples} samples"
        if self.total:
            text += f", {100 * self.done_frames // self.total}%"
        spf = self.seconds_per_frame()
        if spf:
            left = spf * self.frames_left()
            text += f", {hms(left)} left (done {time.strftime('%H:%M', time.localtime(time.time() + left))})"
        return text

    def override(self):
        area, region, _ = self.view()
        return bpy.context.temp_override(window=self.win, screen=self.win.screen, area=area, region=region)

    def until(self, cond, timeout, what):
        t0 = time.perf_counter()

        def ready():
            if cond():
                return True
            if time.perf_counter() - t0 > timeout:
                raise JobError(f"The viewport did not respond ({what}).")
            return False
        return ready

    def sleep(self, seconds):
        t1 = time.perf_counter() + seconds
        return lambda: time.perf_counter() >= t1

    def redraw(self):
        n = self.draws
        self.area.tag_redraw()
        yield self.until(lambda: self.draws > n, 10.0, "redraw")

    def refreshed(self, count):
        """Wait for this many new pictures from the viewport (its samples). With DLSS each one is another step of
        the clean-up, and how clean the result is follows their number, not the clock.

        Redraws cannot be counted for this: when a sample takes long (heavy scene, busy graphics card) the viewport
        also redraws just to update its status line, and the frame would be saved far too early. So the picture
        itself is watched (see on_draw). A picture that stops changing has nothing more to give: flat, noise-free
        scenes look like that from their first sample, and the wait ends early for them."""
        start, t0 = self.pictures, time.perf_counter()
        seen = [self.draws, t0, self.draws]     # last draw count, when it changed, draw count at the last new picture
        was = [self.pictures]

        def ready():
            now = time.perf_counter()
            if self.draws != seen[0]:
                seen[0], seen[1] = self.draws, now
            if self.pictures != was[0]:
                was[0], seen[2] = self.pictures, self.draws
            got = self.pictures - start
            if got >= count:
                if got > 1:
                    self.quiet_scene = False
                return True
            # redraws keep coming but the picture stays the same
            if self.draws - seen[2] >= 2:
                quiet = now - max(self.changed_at, t0)
                patience = max(0.4 if self.quiet_scene else 2.0, 3.0 * self.sample_time)
                if got <= 1 and quiet > patience:
                    self.quiet_scene = True
                    return True
                if quiet > max(15.0, 8.0 * self.sample_time):
                    return True
            if now - seen[1] > 120.0:
                raise JobError("The viewport stopped updating.")
            return False
        return ready
    def frame_rect(self):
        """Camera frame in region pixels (x0, y0, x1, y1), y from the bottom."""
        cam = self.scene.camera
        r3d = self.space.region_3d
        pts = [location_3d_to_region_2d(self.region, r3d, cam.matrix_world @ v) for v in cam.data.view_frame(scene=self.scene)]
        return min(p.x for p in pts), min(p.y for p in pts), max(p.x for p in pts), max(p.y for p in pts)

    def on_draw(self):
        # Runs before the viewport draws: the bound buffer still holds the picture of the previous refresh,
        # in scene-linear float with the render's alpha.
        if bpy.context.region.as_pointer() != self.region_ptr:
            return
        self.draws += 1
        fb = gpu.state.active_framebuffer_get()
        if self.watch_rect:
            # a new sample changes the picture: compare a few lines of pixels with the last redraw's
            x, y, w, h = self.watch_rect
            lines = [fb.read_color(x, y + h * k // 4, w, 1, 4, 0, "FLOAT") for k in (1, 2, 3)]
            lines += [fb.read_color(x + w * k // 4, y, 1, h, 4, 0, "FLOAT") for k in (1, 2, 3)]
            values = np.concatenate([np.asarray(line, dtype=np.float32).reshape(-1, 4) for line in lines])
            mark = values.tobytes()
            if mark != self.watch_mark:
                now = time.perf_counter()
                if self.watch_mark is not None:
                    self.sample_time = now - self.changed_at
                    if self.debug:
                        # how much of the watched picture changed: a whole new sample changes nearly all of it
                        before = np.frombuffer(self.watch_mark, dtype=np.float32).reshape(-1, 4)
                        changed = np.abs(values - before).max(axis=1) > 1e-6
                        self.log("DLSS", "new picture", self.pictures + 1, "changed %.2f" % changed.mean(),
                                 "after %.3f s" % self.sample_time, "redraw", self.draws)
                self.watch_mark, self.changed_at = mark, now
                self.pictures += 1
        if self.grab_rect:
            x, y, w, h = self.grab_rect
            self.grab_rect = None
            buf = fb.read_color(x, y, w, h, 4, 0, "FLOAT")
            buf.dimensions = w * h * 4
            self.grabbed = np.frombuffer(buf, dtype=np.float32).reshape(h, w, 4).copy()

    def grab(self, rect):
        """Read region pixels rect = (x, y, w, h): float RGBA, rows bottom-up like Blender images."""
        self.grabbed = None
        self.grab_rect = rect
        self.area.tag_redraw()
        yield self.until(lambda: self.grabbed is not None, 10.0, "reading the picture")
        return self.grabbed

    def expose(self, rect, factor=1.0):
        """One frame's (or tile's) picture: give DLSS its samples, then read what the viewport shows.
        (Averaging several refreshes was tried: it changes nothing, DLSS's own picture already carries them.)"""
        t0, p0, d0 = time.perf_counter(), self.pictures, self.draws
        yield self.refreshed(math.ceil(self.samples * factor))
        picture = yield from self.grab(rect)
        self.log("DLSS", "picture after", self.pictures - p0, "new pictures in", self.draws - d0, "redraws,", round(time.perf_counter() - t0, 2), "s")
        return picture

    # ------------------------------------------------------------------ set-up and restore
    def prepare(self):
        sc = self.scene
        r, c = sc.render, sc.cycles
        self.keep(c, "preview_denoiser")
        self.keep(c, "use_preview_denoising", True)
        try:
            c.preview_denoiser = "DLSS"
        except TypeError:
            raise JobError("DLSS is not available here. Check Render Properties > Sampling > Viewport > Denoise.")
        self.keep(c, "preview_samples", 0)      # no cap: the job decides when a frame has enough
        self.keep(r, "preview_pixel_size", "1")
        # no render region: with one, the picture comes out slightly stretched and shifted
        self.keep(r, "use_border", False)
        mode = getattr(c, "preview_denoising_upscale_quality", "NONE")
        self.nudge = 0.5 / UPSCALE.get(mode, 1.0) if mode else 0.0
        if self.movie or self.passes:
            # remember every output format setting before changing any: changing one can change the others
            for attr in ("media_type", "file_format", "color_depth", "color_mode", "compression", "exr_codec"):
                if hasattr(r.image_settings, attr):
                    self.keep(r.image_settings, attr)
        if self.movie:
            # lossless frames first (Blender 5 keeps movie and image formats apart); movie.py makes the movie
            im = r.image_settings
            if hasattr(im, "media_type"):
                im.media_type = "IMAGE"
            im.file_format = "PNG"
            im.color_depth = "16"
            im.color_mode = "RGBA" if r.film_transparent else "RGB"
            im.compression = 15
            os.makedirs(self.movie.dir, exist_ok=True)
        if self.passes:
            try:
                passes_mod.prepare(self, self.passes, self.pass_samples)
            except passes_mod.PassError as e:
                raise JobError(str(e))
        self.match_render()
        if not self.win.screen.show_fullscreen:
            sp0 = self.area0.spaces.active
            self.panels = {a: getattr(sp0, a) for a in PANELS}
            # maximise the area only (Ctrl+Space). The full-screen variant (use_hide_panels) also hides the window's
            # top bar and status bar, and Blender does not bring those back after back_to_previous: users were left
            # without File / Edit / workspace tabs. The view's own panels are hidden by prepare_view instead.
            with bpy.context.temp_override(window=self.win, screen=self.win.screen, area=self.area0,
                                           region=next(g for g in self.area0.regions if g.type == "WINDOW")):
                bpy.ops.screen.screen_full_area(use_hide_panels=False)
            self.fullscreen = True

    def keep_safe(self, obj, attr, value):
        try:
            if getattr(obj, attr) != value:
                self.keep(obj, attr, value)
        except (AttributeError, TypeError, RuntimeError, ValueError):
            pass        # linked or locked data: leave it as it is

    def match_render(self):
        """Make the viewport show what a normal render would. A scene can differ between the two: objects and
        collections hidden only in one of them, modifiers switched off in the viewport, lighter viewport
        subdivision, fewer particles shown, Simplify. All of it is put back afterwards."""
        sc, vl = self.scene, self.win.view_layer
        rendered = set()

        def walk(lc):
            for ch in lc.children:
                c = ch.collection
                if ch.exclude or c.hide_render:
                    continue
                # a collection the render uses must not be hidden in the viewport
                self.keep_safe(ch, "hide_viewport", False)
                self.keep_safe(c, "hide_viewport", False)
                rendered.update(o for o in c.objects if not o.hide_render)
                walk(ch)
        # objects and collections that only feed instances (particles, collection instances) are often switched off
        # for viewports, which removes the instances from the viewport too
        for c in bpy.data.collections:
            if not c.hide_render:
                self.keep_safe(c, "hide_viewport", False)
        for o in bpy.data.objects:
            if not o.hide_render:
                self.keep_safe(o, "hide_viewport", False)
        rendered.update(o for o in vl.layer_collection.collection.objects if not o.hide_render)
        walk(vl.layer_collection)
        for o in vl.objects:
            if o in rendered:
                self.keep_safe(o, "hide_viewport", False)
                if o.hide_get(view_layer=vl):
                    o.hide_set(False, view_layer=vl)
                    self.unhidden.append((o, vl))
            elif o.visible_get(view_layer=vl):
                self.keep_safe(o, "hide_viewport", True)
            for m in o.modifiers:
                self.keep_safe(m, "show_viewport", m.show_render)
                if m.type in {"SUBSURF", "MULTIRES"}:
                    self.keep_safe(m, "levels", m.render_levels)
                if m.type == "PARTICLE_SYSTEM":
                    ps = m.particle_system.settings
                    self.keep_safe(ps, "display_percentage", 100)
                    if hasattr(ps, "child_percent"):
                        self.keep_safe(ps, "child_percent", ps.rendered_child_count)
                    if ps.display_method == "NONE":
                        self.keep_safe(ps, "display_method", "RENDER")
        r = sc.render
        if r.use_simplify:
            self.keep_safe(r, "simplify_subdivision", r.simplify_subdivision_render)
            self.keep_safe(r, "simplify_child_particles", r.simplify_child_particles_render)
            self.keep_safe(sc.cycles, "texture_limit", sc.cycles.texture_limit_render)
        vl.update()
        if self.debug:
            dg = bpy.context.evaluated_depsgraph_get()
            n = sum(1 for i in dg.object_instances if i.is_instance)
            self.log("DLSS", "match render:", len(self.kept), "settings changed,", len(self.unhidden), "unhidden,", n, "instances in the viewport")
    def prepare_view(self):
        self.area, self.region, self.space = self.view()
        self.region_ptr = self.region.as_pointer()
        sp = self.space
        for a in PANELS:            # header, toolbars and sidebar out of the way, put back in restore
            self.keep(sp, a, False)
        self.keep(sp, "show_gizmo", False)
        self.keep(sp.overlay, "show_overlays", False)
        self.keep(sp, "use_local_camera", False)
        self.keep(sp.shading, "type")
        self.keep(sp.shading, "use_scene_lights_render", True)
        self.keep(sp.shading, "use_scene_world_render", True)
        if hasattr(sp.shading, "cycles"):
            self.keep(sp.shading.cycles, "render_pass", "COMBINED")
        r3d = sp.region_3d
        self.keep(r3d, "view_perspective", "CAMERA")
        self.keep(r3d, "view_camera_zoom")
        self.keep(r3d, "view_camera_offset", (0.0, 0.0))
        self.handler = bpy.types.SpaceView3D.draw_handler_add(self.on_draw, (), "WINDOW", "PRE_VIEW")
        self.image = bpy.data.images.new(".dlss_render_plus_out", self.W, self.H, alpha=True, float_buffer=True)

    def restore(self):
        """Put everything back. Must not run inside an operator's event handling (it switches screens)."""
        if self.movie:
            self.movie.kill()
        if self.handler:
            bpy.types.SpaceView3D.draw_handler_remove(self.handler, "WINDOW")
            self.handler = None
        for obj, attr, value in reversed(self.kept):
            try:
                setattr(obj, attr, value)
            except Exception:
                pass
        # Output format settings depend on each other (a colour depth or format is only accepted while the media
        # type / format allows it), so in reverse some are refused: set them again, media type and format first.
        for obj, attr, value in self.kept:
            if isinstance(obj, bpy.types.ImageFormatSettings):
                try:
                    setattr(obj, attr, value)
                except Exception:
                    pass
        self.kept = []
        for o, vl in self.unhidden:
            try:
                o.hide_set(True, view_layer=vl)
            except Exception:
                pass
        self.unhidden = []
        if self.fullscreen:
            try:
                with self.override():
                    bpy.ops.screen.back_to_previous()
                # Blender leaves the panels it hid for the full-screen view hidden
                if self.area0 in list(self.win.screen.areas):
                    for a, v in self.panels.items():
                        setattr(self.area0.spaces.active, a, v)
            except Exception:
                traceback.print_exc()
            self.fullscreen = False
        if self.image:
            bpy.data.images.remove(self.image)
            self.image = None
        if self.scene.frame_current != self.frame0:
            self.scene.frame_set(self.frame0)

    # ------------------------------------------------------------------ framing

    def fit_zoom(self):
        """Zoom the camera view until one output pixel is one screen pixel."""
        r3d = self.space.region_3d
        for _ in range(8):
            x0, _, x1, _ = self.frame_rect()
            if abs((x1 - x0) - self.W) < 0.01:
                return
            cur = (math.sqrt(2) + r3d.view_camera_zoom / 50) ** 2 / 4
            zoom = (2 * math.sqrt(cur * self.W / (x1 - x0)) - math.sqrt(2)) * 50
            if not -30.0 <= zoom <= 600.0:
                raise JobError("This output size cannot be shown 1:1 in the viewport.")
            r3d.view_camera_zoom = zoom
            yield from self.redraw()
        raise JobError("Could not match the camera view to the output size.")

    def calibrate_pan(self):
        """How many pixels the frame moves per unit of camera-view offset."""
        r3d = self.space.region_3d
        a = self.frame_rect()
        r3d.view_camera_offset = (0.02, 0.02)
        yield from self.redraw()
        b = self.frame_rect()
        self.pan_k = ((b[0] - a[0]) / 0.02, (b[1] - a[1]) / 0.02)
        r3d.view_camera_offset = (0.0, 0.0)
        yield from self.redraw()

    def pan_to(self, left, bottom):
        """Put the frame's bottom-left corner at this region pixel."""
        r3d = self.space.region_3d
        for _ in range(6):
            x0, y0, _, _ = self.frame_rect()
            ex, ey = left - x0, bottom - y0
            if abs(ex) < 0.02 and abs(ey) < 0.02:
                return
            off = r3d.view_camera_offset
            r3d.view_camera_offset = (off[0] + ex / self.pan_k[0], off[1] + ey / self.pan_k[1])
            yield from self.redraw()
        raise JobError("Could not line the camera view up with the pixel grid.")

    def aim(self, tile, room):
        """Show this tile: returns the region rectangle that holds its pixels."""
        ox, oy, tw, th = tile
        # the tile sits at the viewport's bottom-left corner, or centred when the whole output is smaller
        left, bottom = (room[0] - tw) // 2 - ox, (room[1] - th) // 2 - oy
        yield from self.pan_to(left - self.nudge, bottom - self.nudge)
        self.watch_rect, self.watch_mark = (left + ox, bottom + oy, tw, th), None
        return (left + ox, bottom + oy, tw, th)

    # ------------------------------------------------------------------ output

    def write(self, frame, rgba):
        """rgba: scene-linear, premultiplied alpha, (H, W, 4), rows bottom-up."""
        if self.steady:
            rgba = self.steady.apply(rgba)
        if self.movie:
            path = self.movie.frame_file(frame)
        else:
            path = bpy.path.abspath(self.scene.render.frame_path(frame=frame))
            if not self.animation:
                path = free_name(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if self.passes:
            # the data render runs at this frame; Combined is the DLSS picture
            self.phase = "Render passes"
            try:
                passes_mod.render(self)
                passes_mod.merge(self.pass_tmp, rgba, path, self.pass_layer)
            except passes_mod.PassError as e:
                raise JobError(str(e))
            self.phase = "Rendering"
        else:
            self.image.pixels.foreach_set(np.ascontiguousarray(rgba, np.float32).ravel())
            # same colour management and file settings as a normal render
            self.image.save_render(filepath=path, scene=self.scene)
        self.last_path = path
        self.saved += 1
        self.saved_at.append(time.perf_counter())
        if self.on_frame:
            self.on_frame(frame)

    def camera_cut(self):
        """True when the camera jumped since the last rendered frame: another camera (markers), a sharp turn or a
        move far beyond its recent pace. DLSS would blend the old shot into the new one (ghosting)."""
        cam = self.scene.camera
        m = cam.matrix_world.copy()
        last, self.cam_last = self.cam_last, (cam, m)
        if last is None:
            return False
        if last[0] != cam:
            self.cam_moves = []
            return True
        move = (m.translation - last[1].translation).length
        turn = last[1].to_quaternion().rotation_difference(m.to_quaternion()).angle
        pace = sum(self.cam_moves) / len(self.cam_moves) if self.cam_moves else None
        self.cam_moves = (self.cam_moves + [move])[-10:]
        if turn > CUT_ANGLE:
            return True
        return pace is not None and pace > 1e-4 and move > CUT_MOVE * pace and move > 0.5

    def encode(self):
        missing = self.movie.missing()
        if missing:
            raise JobError(f"{len(missing)} frames of the movie are missing (first: {missing[0]}). Render them again.")
        self.phase = "Making the movie"
        if getattr(self, "space", None):
            self.space.shading.type = "SOLID"   # free the graphics card while ffmpeg works
        proc = self.movie.start()
        yield lambda: proc.poll() is not None
        try:
            self.movie.finish(self.keep_frames)
        except movie_mod.MovieError as e:
            raise JobError(str(e))
        self.last_path = self.movie.path

    def tile_weight(self, tile, feather):
        """Blend weight of a tile: ramps down towards edges it shares with a neighbour."""
        ox, oy, w, h = tile

        def ramp(n, start, total, f):
            v = np.ones(n, np.float32)
            if f > 0:
                edge = (np.arange(f, dtype=np.float32) + 1.0) / (f + 1.0)
                if start > 0:
                    v[:f] = edge
                if start + n < total:
                    v[n - f:] = edge[::-1]
            return v
        return ramp(h, oy, self.H, feather[1])[:, None] * ramp(w, ox, self.W, feather[0])[None, :]

    def start_render(self, rect, empty_patience):
        """Switch the viewport to its rendered mode and wait until it actually shows a picture. Returns the wait."""
        self.space.shading.type = "RENDERED"
        yield self.sleep(0.3)
        yield from self.redraw()        # from here the buffer belongs to the new render: empty until its first sample
        t0 = time.perf_counter()
        # An empty viewport is fully transparent. With a transparent film a shot can stay empty for real, so the
        # wait is short then; with a background every pixel gets covered, so wait as long as the scene needs to load.
        patience = empty_patience if self.scene.render.film_transparent else 900.0
        while True:
            a = yield from self.grab(rect)
            if float(a[..., 3].max()) > 0.0 or time.perf_counter() - t0 > patience:
                break
            yield self.sleep(0.1)
        return time.perf_counter() - t0

    def shot_start(self, frame, lead):
        """Where the play-in before this frame starts: lead frames earlier, but not before the timeline marker that
        switched to the current camera (the frames before it belong to another shot)."""
        switches = [m.frame for m in self.scene.timeline_markers if m.camera and m.frame <= frame]
        return max([frame - lead] + switches)

    def play_in(self, frame):
        """Step through the frames just before this one (no saving), ending on it, so the picture of the frame is
        made with the same DLSS history as its neighbours: without it a restarted render looks visibly different."""
        sc = self.scene
        for g in range(sc.frame_current + 1, frame):
            sc.frame_set(g)
            yield self.refreshed(self.samples)
        if sc.frame_current != frame:
            sc.frame_set(frame)

    # ------------------------------------------------------------------ the run

    def run(self):
        sc = self.scene
        if not self.frames:
            # Resume after every frame was saved but before the movie was made
            if self.movie:
                yield from self.encode()
            self.message = f"DLSS Render: nothing left to render, saved to {self.last_path or 'the output folder'}"
            return
        self.prepare()
        yield self.sleep(0.3)
        sc.frame_set(self.frames[0])        # frame the camera the first frame is seen through (markers can switch it)
        self.prepare_view()
        yield from self.redraw()
        yield from self.fit_zoom()
        yield from self.calibrate_pan()
        self.framed_cam = sc.camera

        room = (self.region.width, self.region.height)
        limit = room_limit()
        if limit:
            room = (min(room[0], limit[0]), min(room[1], limit[1]))
        if min(room) < MIN_ROOM:
            raise JobError("The 3D Viewport is too small to render in.")
        xs, ys = tile_starts(self.W, room[0]), tile_starts(self.H, room[1])
        tw, th = min(self.W, room[0]), min(self.H, room[1])
        tiles = [(ox, oy, tw, th) for oy in ys for ox in xs]
        feather = (min(FEATHER, tw - (xs[1] - xs[0])) if len(xs) > 1 else 0,
                   min(FEATHER, th - (ys[1] - ys[0])) if len(ys) > 1 else 0)
        many = len(tiles) > 1
        self.tile_count = len(tiles)
        self.total = len(tiles) * len(self.frames)
        self.log("DLSS", "output", self.W, self.H, "room", room, "tiles", len(tiles), xs, ys, "feather", feather,
                 "samples", self.samples)
        if many:
            acc = np.zeros((self.H, self.W, 4), np.float32)
            wsum = np.zeros((self.H, self.W, 1), np.float32)
            weights = [self.tile_weight(t, feather)[..., None] for t in tiles]

        rect = yield from self.aim(tiles[0], room)
        # an animation starts a few frames early, so the first saved frame already has DLSS's history behind it
        sc.frame_set(self.shot_start(self.frames[0], PLAY_IN if self.animation else 0))
        waited = yield from self.start_render(rect, 20.0)
        self.log("DLSS", "first picture after", round(waited, 2), "s")
        d0, p0, t_rate = self.draws, self.pictures, time.perf_counter()
        yield self.refreshed(self.samples)
        self.log("DLSS", "warm-up:", self.pictures - p0, "new pictures in", self.draws - d0, "redraws,", round(time.perf_counter() - t_rate, 2), "s")
        yield from self.play_in(self.frames[0])
        self.phase = "Rendering"
        self.camera_cut()       # remember where the camera starts

        prev = None
        for i, f in enumerate(self.frames):
            self.cur_frame = f
            # After many frame changes in a row the Cycles viewport sometimes leaves a part black or with the wrong
            # material for seconds. Starting its render again now and then prevents that (tests: 8 runs of 300
            # frames clean, against most runs without it).
            restart = i and i % RESTART_EVERY == 0
            gap = prev is not None and f - prev != sc.frame_step     # Resume skipped frames that were saved
            cut = False
            if not (restart or gap):
                if sc.frame_current != f:
                    sc.frame_set(f)
                cut = self.camera_cut()
            if restart or gap or cut:
                # Start the viewport render again and give DLSS the frames just before this one, but never frames
                # of another shot: those would be blended into this one (ghosting)
                if cut:
                    self.log("DLSS", "camera cut at frame", f)
                self.space.shading.type = "SOLID"
                yield from self.redraw()
                if sc.frame_current != f:
                    sc.frame_set(f)
                if sc.camera != self.framed_cam:
                    # another camera can frame differently: line the view up again
                    yield from self.fit_zoom()
                    yield from self.calibrate_pan()
                    rect = yield from self.aim(tiles[0], room)
                    self.framed_cam = sc.camera
                start = f if cut else self.shot_start(f, PLAY_IN)
                sc.frame_set(start)
                yield from self.start_render(rect, 5.0)
                # no frames before it to learn from: a fresh shot gets more samples instead
                yield self.refreshed(self.samples if start < f else 2 * self.samples)
                yield from self.play_in(f)
                self.camera_cut()
                if self.steady and (gap or cut):
                    self.steady.reset()
            prev = f
            self.log("DLSS", "frame", f, "at", round(time.perf_counter() - self.t0, 2), "clock", time.strftime("%H:%M:%S"))
            for ti, tile in enumerate(tiles):
                if many:
                    rect = yield from self.aim(tile, room)
                rgba = yield from self.expose(rect)
                if many:
                    ox, oy, w, h = tile
                    acc[oy:oy + h, ox:ox + w] += rgba * weights[ti]
                    wsum[oy:oy + h, ox:ox + w] += weights[ti]
                    self.done_frames += 1
            if many:
                acc /= wsum
                self.write(f, acc)
                acc[:] = 0.0
                wsum[:] = 0.0
            else:
                self.write(f, rgba)
                self.done_frames += 1
        n = len(self.frames)
        dt = time.perf_counter() - self.t0
        if self.movie:
            yield from self.encode()
            where = self.movie.path
        else:
            where = self.last_path if n == 1 else os.path.dirname(self.last_path)
        self.message = f"DLSS Render: {n} frame{'s' if n > 1 else ''} in {dt:.1f} s ({dt / n:.2f} s per frame), saved to {where}"

    def advance(self):
        """Run until the job has to wait for the viewport. True when finished."""
        if self.cancel:
            raise Cancelled()
        for _ in range(32):
            if self.wait is not None:
                if not self.wait():
                    return False
                self.wait = None
            try:
                self.wait = next(self.gen)
            except StopIteration:
                return True
        return False

    def progress(self):
        return self.done_frames / self.total if self.total else 0.0

    def tick(self):
        """Application timer. All the work happens here, outside any operator's event handling: switching screens
        from inside a modal operator leaves Blender holding a dead area and crashes it."""
        global ACTIVE
        if not self.step():
            return 0.005
        print(self.message)
        ACTIVE = None
        return None

    def step(self):
        """Advance the job; True once it has ended and everything is put back."""
        try:
            if not self.advance():
                return False
        except Cancelled:
            self.level, self.message = "WARNING", f"DLSS Render cancelled after {self.done_frames} of {self.total} steps"
        except JobError as e:
            self.level, self.message = "ERROR", str(e)
        except Exception as e:
            traceback.print_exc()
            self.level, self.message = "ERROR", f"DLSS Render failed: {e}"
        try:
            self.restore()
        except Exception:
            traceback.print_exc()
        self.finished = True
        return True


class Runner:
    """Shared by the render and queue operators: runs a job (anything with tick, step-free progress(), status(),
    cancel, finished, level, message, win) and keeps the user's input away from the viewport meanwhile."""
    chime_at_end = False

    def start(self, context, job):
        global ACTIVE
        ACTIVE = self.job = job
        wm = context.window_manager
        self.timer = wm.event_timer_add(0.1, window=context.window)
        wm.progress_begin(0, 100)
        wm.modal_handler_add(self)
        keep_awake(True)
        bpy.app.timers.register(job.tick, first_interval=0.05)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        # This operator only keeps the user's input away from the viewport (moving the view would break the framing)
        # and listens for Esc; the job itself runs from an application timer.
        job = self.job
        if event.type == "ESC" and event.value == "PRESS":
            job.cancel = True
        if event.type != "TIMER":
            return {"RUNNING_MODAL"}
        wm = context.window_manager
        if not job.finished:
            wm.progress_update(int(100 * job.progress()))
            if context.workspace:
                context.workspace.status_text_set(job.status())
            return {"RUNNING_MODAL"}
        wm.event_timer_remove(self.timer)
        wm.progress_end()
        keep_awake(False)
        if context.workspace:
            context.workspace.status_text_set(None)
        if self.chime_at_end:
            chime()
        # A warning or error report opens a popup as the operator ends; show errors with our own popup instead,
        # a moment later, so it is built in a clean context.
        self.report({"INFO"}, job.message)
        if job.level != "INFO":
            win, text = job.win, job.message

            def popup():
                with bpy.context.temp_override(window=win):
                    bpy.context.window_manager.popup_menu(lambda menu, _c: menu.layout.label(text=text),
                                                          title="DLSS Render", icon="ERROR")
            bpy.app.timers.register(popup, first_interval=0.2)
        return {"FINISHED"} if job.level == "INFO" else {"CANCELLED"}


class RENDER_OT_dlss_render_plus(Runner, bpy.types.Operator):
    bl_idname = "render.dlss_render_plus"
    bl_label = "DLSS Render"
    bl_description = "Render through the Cycles DLSS viewport and save the result to the output path"
    bl_options = {"REGISTER"}

    animation: bpy.props.BoolProperty(name="Animation", default=False)

    @classmethod
    def poll(cls, context):
        return ACTIVE is None and context.scene is not None

    def invoke(self, context, event):
        return self.execute(context)

    def execute(self, context):
        try:
            job = Job(context, self.animation)
        except JobError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        self.chime_at_end = self.animation
        return self.start(context, job)


class RENDER_OT_dlss_render_setup(bpy.types.Operator):
    bl_idname = "render.dlss_render_plus_setup"
    bl_label = "Set Up DLSS Render"
    bl_description = "Change the setting that keeps DLSS Render from starting"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        block = blocker(context)
        if not block or not block[2]:
            return {"CANCELLED"}
        key = block[0]
        if key == "ENGINE":
            context.scene.render.engine = "CYCLES"
        elif key == "DEVICE":
            context.scene.cycles.device = "GPU"
        elif key == "PREFS":
            prefs = cycles_prefs(context)
            backend = nvidia_backend(context, prefs)
            prefs.compute_device_type = backend
            for device in prefs.get_devices_for_type(backend):
                if device.type == backend:
                    device.use = True
        return {"FINISHED"}


classes = (RENDER_OT_dlss_render_plus, RENDER_OT_dlss_render_setup)
