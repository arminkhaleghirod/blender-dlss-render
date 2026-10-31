"""Render queue: scenes, cameras and frame ranges rendered one after another.

Every saved frame is written down in a small JSON file next to the .blend (<name>.dlss_queue.json), so Resume after a
crash or Esc continues with the frames that are still missing."""
import contextlib
import json
import os
import shutil
import tempfile
import time
import traceback
import uuid

import bpy

from . import movie, render


def store_path():
    fp = bpy.data.filepath
    if fp:
        return os.path.splitext(fp)[0] + ".dlss_queue.json"
    return os.path.join(tempfile.gettempdir(), "untitled.dlss_queue.json")


class Store:
    def __init__(self, path=None):
        self.path = path or store_path()
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.data = json.load(fh)
        except (OSError, ValueError):
            self.data = {}

    def entry(self, uid):
        return self.data.setdefault(uid, {"frames": [], "complete": False})

    def done(self, uid):
        e = self.data.get(uid)
        return set(e["frames"]) if e else set()

    def complete(self, uid):
        e = self.data.get(uid)
        return bool(e and e["complete"])

    def mark(self, uid, frame):
        e = self.entry(uid)
        if frame not in e["frames"]:
            e["frames"].append(frame)
        self.save()

    def finish(self, uid):
        self.entry(uid)["complete"] = True
        self.save()

    def clear(self, uid=None):
        if uid is None:
            self.data = {}
        else:
            self.data.pop(uid, None)
        self.save()

    def save(self):
        """Best effort: a locked or read-only progress file (cloud sync, read-only folder) must not stop a render.
        Resume then falls back to what was written last."""
        tmp = self.path + ".tmp"
        for attempt in range(3):
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self.data, fh)
                os.replace(tmp, self.path)
                return
            except OSError as e:
                if attempt == 2:
                    print(f"DLSS Render: could not write the queue progress file {self.path}: {e}")
                time.sleep(0.05)


def is_camera(_self, obj):
    return obj.type == "CAMERA"


class DLSSQueueItem(bpy.types.PropertyGroup):
    uid: bpy.props.StringProperty()
    enabled: bpy.props.BoolProperty(name="Render", default=True, description="Render this entry")
    scene: bpy.props.PointerProperty(type=bpy.types.Scene, name="Scene")
    camera: bpy.props.PointerProperty(type=bpy.types.Object, name="Camera", poll=is_camera,
                                      description="Camera to render with (empty: the scene's camera)")
    animation: bpy.props.BoolProperty(name="Animation", default=True,
                                      description="Render the frame range; off renders only the start frame")
    use_scene_range: bpy.props.BoolProperty(name="Scene Range", default=True,
                                            description="Use the scene's own frame range")
    frame_start: bpy.props.IntProperty(name="Start", default=1)
    frame_end: bpy.props.IntProperty(name="End", default=250)
    output: bpy.props.StringProperty(name="Output", subtype="FILE_PATH",
                                     description="Output path for this entry (empty: the scene's own). Use it when "
                                                 "two entries render the same scene, e.g. with different cameras")
    status: bpy.props.StringProperty()


def item_frames(item, scene):
    if item.use_scene_range:
        start, end = scene.frame_start, scene.frame_end
    else:
        start, end = item.frame_start, item.frame_end
    if not item.animation:
        return [start if not item.use_scene_range else scene.frame_current]
    return list(range(start, end + 1, scene.frame_step))


def item_label(item):
    sc = item.scene
    if sc is None:
        return "(no scene)"
    cam = item.camera or sc.camera
    frames = item_frames(item, sc)
    rng = f"{frames[0]}-{frames[-1]}" if item.animation and frames else f"frame {frames[0]}" if frames else "-"
    return f"{sc.name} / {cam.name if cam else 'no camera'} / {rng}"


class QueueJob:
    """Runs one render.Job per queue entry. Looks like a Job to render.Runner."""

    def __init__(self, context, resume):
        self.win = context.window
        self.home = self.win.scene
        self.cancel = False
        self.finished = False
        self.level, self.message = "INFO", ""
        self.stop_reason = ""
        self.store = Store()
        self.queue = self.home.dlss_render_plus.queue
        self.plan = []
        # entries copied with their scene (Scene > Full Copy) keep the same id: give this queue's copies their own,
        # or the two queues would share their progress
        others = {i.uid for sc in bpy.data.scenes if sc != self.home for i in sc.dlss_render_plus.queue if i.uid}
        seen = set()
        for item in self.queue:
            if not item.uid or item.uid in others or item.uid in seen:
                item.uid = uuid.uuid4().hex
            seen.add(item.uid)
        outputs = {}
        for item in self.queue:
            if not item.enabled or item.scene is None:
                continue
            frames = item_frames(item, item.scene)
            if not frames:
                continue
            out = os.path.normcase(bpy.path.abspath(item.output or item.scene.render.filepath))
            clash = outputs.get(out)
            if clash and set(frames) & clash[1]:
                raise render.JobError(f"Two queue entries save to the same files ({item.output or item.scene.render.filepath}): "
                                      "give one of them its own Output path.")
            outputs[out] = (item, set(frames))
        for item in self.queue:
            if not item.enabled or item.scene is None:
                continue
            frames = item_frames(item, item.scene)
            if not frames:
                continue
            with self.output_of(item):
                movie_item = item.scene.render.is_movie_format and item.animation
                folder = movie.frames_dir(item.scene, frames) if movie_item else None
            if not resume:
                self.store.clear(item.uid)      # only this queue's entries: other scenes keep their progress
                if folder and os.path.basename(folder).endswith("_frames"):
                    shutil.rmtree(folder, ignore_errors=True)   # frames of an earlier run must not end up in the movie
            elif self.store.complete(item.uid):
                item.status = "Done"
                continue
            if movie_item:
                # a movie is made from its frame files: whatever is missing there has to be rendered again
                todo = [f for f in frames if not os.path.isfile(movie.frame_file(folder, f))]
            else:
                saved = self.store.done(item.uid)
                todo = [f for f in frames if f not in saved]
            done = len(frames) - len(todo)
            self.plan.append((item.uid, item.scene, item.camera, item.animation, frames, todo, item.output))
            item.status = f"{done} of {len(frames)} saved" if done else "Waiting"
        if not self.plan:
            raise render.JobError("Nothing to render: the queue is empty or every entry is done.")
        self.total_frames = sum(len(p[5]) for p in self.plan)
        self.idx = -1
        self.cur = None
        self.cam_kept = None
        self.out_kept = None
        self.frames_before = 0      # frames of the entries already finished
        self.results = []
        self.spf = None             # seconds per frame seen so far, for the time left of later entries

    @staticmethod
    @contextlib.contextmanager
    def output_of(item):
        """The entry's own output path in place of its scene's, for as long as the block runs."""
        sc = item.scene
        old = sc.render.filepath
        if item.output:
            sc.render.filepath = item.output
        try:
            yield
        finally:
            sc.render.filepath = old

    def item(self, uid):
        return next((i for i in self.queue if i.uid == uid), None)

    def set_status(self, uid, text):
        it = self.item(uid)
        if it:
            it.status = text

    def start_next(self):
        while self.idx + 1 < len(self.plan):
            self.idx += 1
            uid, scene, camera, animation, frames, todo, output = self.plan[self.idx]
            self.win.scene = scene
            if camera and scene.camera != camera:
                self.cam_kept = (scene, scene.camera)
                scene.camera = camera
            if output:
                self.out_kept = (scene, scene.render.filepath)
                scene.render.filepath = output
            try:
                self.cur = render.Job(None, animation, scene=scene, window=self.win, frames=todo,
                                      all_frames=frames, on_frame=lambda f, uid=uid: self.store.mark(uid, f))
            except render.JobError as e:
                self.put_camera_back()
                self.results.append(f"{scene.name}: {e}")
                self.set_status(uid, f"Failed: {e}")
                self.frames_before += len(todo)
                continue
            self.set_status(uid, "Rendering")
            return True
        return False

    def put_camera_back(self):
        """The entry's camera and output path were set on its scene for the render: give the scene its own back."""
        if self.cam_kept:
            scene, cam = self.cam_kept
            try:
                scene.camera = cam
            except Exception:
                pass
            self.cam_kept = None
        if self.out_kept:
            scene, path = self.out_kept
            try:
                scene.render.filepath = path
            except Exception:
                pass
            self.out_kept = None

    def stop_for_save(self):
        """The file is being saved: settings back now (they'd be saved otherwise), the queue stops on its next tick."""
        if self.cur:
            self.cur.stop_reason = self.stop_reason
            self.cur.restore_data()
            self.cur.cancel = True
        self.put_camera_back()
        self.cancel = True

    def stop_now(self, reason):
        if self.cur:
            self.cur.stop_now(reason)
            self.cur = None
        self.put_camera_back()
        try:
            self.win.scene = self.home
        except Exception:
            pass
        render.drop_timer(self)
        self.level, self.message = "WARNING", f"DLSS Render queue stopped: {reason} Resume continues where it stopped."
        self.finished = True

    def end_current(self):
        job, uid = self.cur, self.plan[self.idx][0]
        self.cur = None
        self.put_camera_back()
        spf = job.seconds_per_frame()
        if spf:
            self.spf = spf
        self.frames_before += len(job.frames)
        if job.level == "INFO":
            self.store.finish(uid)
            self.set_status(uid, "Done")
        elif job.level == "WARNING":
            self.set_status(uid, f"Stopped, {len(self.store.done(uid))} of {len(job.all_frames)} saved")
            self.cancel = True
        else:
            self.set_status(uid, f"Failed: {job.message}")
            self.results.append(f"{job.scene.name}: {job.message}")

    def tick(self):
        if self.finished:       # stopped from outside (file load, add-on disabled)
            return None
        try:
            if self.cur is None and (self.cancel or not self.start_next()):
                return self.done()
            if self.cancel:
                self.cur.cancel = True
            if self.cur.step():
                self.end_current()
                return 0.5      # let the window redraw after leaving full screen before the next entry
            return 0.005
        except Exception as e:
            traceback.print_exc()
            if self.cur:
                try:
                    self.cur.restore()
                except Exception:
                    pass
                self.cur = None
            self.put_camera_back()
            self.level, self.message = "ERROR", f"DLSS Render queue failed: {e}"
            return self.done()

    def done(self):
        try:
            self.win.scene = self.home
        except Exception:
            pass
        n = len(self.plan)
        if self.level == "INFO":
            if self.cancel:
                self.level = "WARNING"
                self.message = (self.stop_reason or "DLSS Render queue stopped.") + " Resume continues where it stopped."
                if self.results:
                    self.message += " Failed: " + "; ".join(self.results)
            elif self.results:
                self.level = "ERROR"
                self.message = f"DLSS Render queue: {len(self.results)} of {n} failed. " + "; ".join(self.results)
            else:
                self.message = f"DLSS Render queue: {n} entr{'y' if n == 1 else 'ies'} done"
        print(self.message)
        self.finished = True
        render.ACTIVE = None
        render.keep_awake(False)
        return None

    def progress(self):
        done = self.frames_before + (self.cur.saved if self.cur else 0)
        return done / self.total_frames if self.total_frames else 0.0

    def status(self):
        head = f"Queue {self.idx + 1} of {len(self.plan)}"
        if not self.cur:
            return head
        text = f"{head} | {self.cur.status()}"
        spf = self.cur.seconds_per_frame() or self.spf
        if spf:
            later = sum(len(p[5]) for p in self.plan[self.idx + 1:])
            left = spf * (self.cur.frames_left() + later)
            text += f" | queue: {render.hms(left)} left (done {time.strftime('%H:%M', time.localtime(time.time() + left))})"
        return text


# ---------------------------------------------------------------------------------------------- operators

class RENDER_OT_dlss_queue_add(bpy.types.Operator):
    bl_idname = "render.dlss_queue_add"
    bl_label = "Add to Queue"
    bl_description = "Add this scene, its camera and frame range to the render queue"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.dlss_render_plus
        it = s.queue.add()
        it.uid = uuid.uuid4().hex
        it.scene = context.scene
        it.camera = context.scene.camera
        it.frame_start, it.frame_end = context.scene.frame_start, context.scene.frame_end
        s.queue_index = len(s.queue) - 1
        return {"FINISHED"}


class RENDER_OT_dlss_queue_remove(bpy.types.Operator):
    bl_idname = "render.dlss_queue_remove"
    bl_label = "Remove from Queue"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return render.ACTIVE is None and len(context.scene.dlss_render_plus.queue) > 0

    def execute(self, context):
        s = context.scene.dlss_render_plus
        it = s.queue[s.queue_index]
        if it.uid:
            Store().clear(it.uid)
        s.queue.remove(s.queue_index)
        s.queue_index = min(s.queue_index, len(s.queue) - 1)
        return {"FINISHED"}


class RENDER_OT_dlss_queue_move(bpy.types.Operator):
    bl_idname = "render.dlss_queue_move"
    bl_label = "Move in Queue"
    bl_options = {"REGISTER", "UNDO"}

    direction: bpy.props.EnumProperty(items=[("UP", "Up", ""), ("DOWN", "Down", "")])

    def execute(self, context):
        s = context.scene.dlss_render_plus
        i = s.queue_index
        j = i - 1 if self.direction == "UP" else i + 1
        if 0 <= j < len(s.queue):
            s.queue.move(i, j)
            s.queue_index = j
        return {"FINISHED"}


class RENDER_OT_dlss_queue_run(render.Runner, bpy.types.Operator):
    bl_idname = "render.dlss_queue_run"
    bl_label = "Render Queue"
    bl_description = "Render every ticked entry of the queue, one after another"
    bl_options = {"REGISTER"}
    chime_at_end = True

    resume: bpy.props.BoolProperty(name="Resume", default=False,
                                   description="Keep the frames already saved and render only what is missing")

    @classmethod
    def poll(cls, context):
        return render.ACTIVE is None and context.window is not None

    @classmethod
    def description(cls, context, props):
        if props.resume:
            return "Continue the queue where it stopped: frames already saved are kept"
        return "Render every ticked entry of the queue from the start, one after another"

    def invoke(self, context, event):
        return self.execute(context)

    def execute(self, context):
        try:
            job = QueueJob(context, self.resume)
        except render.JobError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        return self.start(context, job)


class DLSS_UL_queue(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "enabled", text="")
        row.label(text=item_label(item), icon="RENDER_ANIMATION" if item.animation else "RENDER_STILL")
        if item.status:
            sub = row.row()
            sub.alignment = "RIGHT"
            sub.label(text=item.status)


def draw(layout, context):
    s = context.scene.dlss_render_plus
    row = layout.row()
    row.template_list("DLSS_UL_queue", "", s, "queue", s, "queue_index", rows=3)
    col = row.column(align=True)
    col.operator("render.dlss_queue_add", text="", icon="ADD")
    col.operator("render.dlss_queue_remove", text="", icon="REMOVE")
    col.separator()
    col.operator("render.dlss_queue_move", text="", icon="TRIA_UP").direction = "UP"
    col.operator("render.dlss_queue_move", text="", icon="TRIA_DOWN").direction = "DOWN"
    if 0 <= s.queue_index < len(s.queue):
        it = s.queue[s.queue_index]
        col = layout.column()
        col.prop(it, "scene")
        col.prop(it, "camera")
        col.prop(it, "animation")
        col.prop(it, "output", placeholder="Scene's own output")
        col.prop(it, "use_scene_range")
        if not it.use_scene_range:
            sub = col.column(align=True)
            sub.prop(it, "frame_start")
            if it.animation:
                sub.prop(it, "frame_end", text="End")
    row = layout.row(align=True)
    row.scale_y = 1.4
    row.enabled = len(s.queue) > 0
    row.operator("render.dlss_queue_run", text="Render Queue", icon="SEQ_STRIP_DUPLICATE").resume = False
    row.operator("render.dlss_queue_run", text="Resume", icon="PLAY").resume = True
    info = layout.column()
    info.active = False
    info.label(text="Each entry uses its scene's DLSS Render settings.")


classes = (DLSSQueueItem, RENDER_OT_dlss_queue_add, RENDER_OT_dlss_queue_remove, RENDER_OT_dlss_queue_move,
           RENDER_OT_dlss_queue_run, DLSS_UL_queue)
