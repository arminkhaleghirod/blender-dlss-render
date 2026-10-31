"""DLSS Render Plus: render Cycles stills and animations through the DLSS viewport.

Based on DLSS Render 1.0.2 by Armin5D (GPL-3.0-or-later), with a render queue, movie output, render passes and
Steady Glass & Noise added."""
import bpy

from . import passes, render, render_queue


class DLSSRenderSettings(bpy.types.PropertyGroup):
    quality: bpy.props.EnumProperty(
        name="Quality",
        description="How many viewport samples each frame gets before it is saved",
        items=[
            ("DRAFT", "Draft", "Fastest: 8 samples per frame"),
            ("BALANCED", "Balanced", "24 samples per frame"),
            ("HIGH", "High", "64 samples per frame; cleaner glass and fine shadows"),
            ("CUSTOM", "Custom", "Choose the number of samples per frame"),
        ],
        default="BALANCED",
    )
    samples: bpy.props.IntProperty(
        name="Samples",
        description="Viewport samples each frame gets before it is saved (the count the rendered viewport shows)",
        default=24, min=1, max=4096)
    use_steady: bpy.props.BoolProperty(
        name="Steady Glass & Noise",
        description="Calm the frame-to-frame flicker on glass and fine detail in animations. Moving edges keep "
                    "their sharpness; a camera cut starts afresh",
        default=False)
    steady_strength: bpy.props.FloatProperty(
        name="Strength", description="How much of the previous frame is kept where nothing moved",
        default=0.6, min=0.0, max=0.9, subtype="FACTOR")
    passes: bpy.props.EnumProperty(
        name="Render Passes", options={"ENUM_FLAG"}, items=passes.ENUM_ITEMS, default=set(),
        description="Passes saved with the DLSS picture as one OpenEXR MultiLayer file per frame")
    pass_samples: bpy.props.IntProperty(
        name="Pass Samples", default=16, min=1, max=1024,
        description="Samples of the short Cycles render that makes the passes (more: smoother AO and cryptomatte "
                    "edges, slower)")
    keep_movie_frames: bpy.props.BoolProperty(
        name="Keep Movie Frames", default=False,
        description="Keep the lossless PNG frames the movie was made from")
    queue: bpy.props.CollectionProperty(type=render_queue.DLSSQueueItem)
    queue_index: bpy.props.IntProperty()


class DLSSRenderPrefs(bpy.types.AddonPreferences):
    bl_idname = __package__

    ffmpeg_path: bpy.props.StringProperty(
        name="ffmpeg", subtype="FILE_PATH",
        description="ffmpeg.exe used to make movies (empty: the one on the PATH)")

    def draw(self, context):
        from .movie import find_ffmpeg
        self.layout.prop(self, "ffmpeg_path")
        found = find_ffmpeg(self.ffmpeg_path)
        self.layout.label(text=f"Using: {found}" if found else "ffmpeg not found: movie output won't work",
                          icon="CHECKMARK" if found else "ERROR")


SECTIONS_CLOSED = True      # Output starts folded away
AUTHOR = "Armin5D"
YOUTUBE_URL = "https://www.youtube.com/@armin5d"
WEBSITE_URL = "https://armin5d.com"
REPO_URL = "https://github.com/arminkhaleghirod/blender-dlss-render"


def draw_main(layout, context):
    sc = context.scene
    s = sc.dlss_render_plus
    block = render.blocker(context)

    if block:
        # one step at a time: what is missing, and the button that fixes it when there is one
        box = layout.box()
        box.label(text=block[1], icon="ERROR")
        if block[2]:
            row = box.row()
            row.scale_y = 1.3
            row.operator("render.dlss_render_plus_setup", text=block[2])

    col = layout.column()
    col.active = block is None
    row = col.row(align=True)
    row.use_property_split = False      # the four choices need the full width
    row.prop(s, "quality", expand=True)
    if s.quality == "CUSTOM":
        col.prop(s, "samples")
    if hasattr(sc, "cycles") and hasattr(sc.cycles, "preview_denoising_upscale_quality"):
        col.prop(sc.cycles, "preview_denoising_upscale_quality", text="Upscale Mode")
    col.prop(s, "use_steady")
    if s.use_steady:
        col.prop(s, "steady_strength")

    row = layout.row(align=True)
    row.scale_y = 1.5
    row.enabled = block is None
    row.operator("render.dlss_render_plus", text="Render Image", icon="RENDER_STILL").animation = False
    row.operator("render.dlss_render_plus", text="Render Animation", icon="RENDER_ANIMATION").animation = True

    w, h = render.output_size(sc)
    frames = len(range(sc.frame_start, sc.frame_end + 1, sc.frame_step))
    win = context.window
    tiles = len(render.tile_starts(w, win.width)) * len(render.tile_starts(h, win.height))
    info = layout.column(align=True)
    info.active = False
    info.label(text=f"{w} x {h} px, {frames} frame{'s' if frames != 1 else ''}")
    if tiles > 1:
        info.label(text=f"Larger than this window: {tiles} tiles")
    if sc.render.is_movie_format:
        info.label(text="Movie output: made with ffmpeg from lossless frames")
    if s.passes:
        info.label(text=f"Passes: OpenEXR MultiLayer, +{s.pass_samples}-sample data render per frame")


def draw_passes(layout, context):
    s = context.scene.dlss_render_plus
    grid = layout.grid_flow(columns=2, even_columns=True, align=True)
    grid.use_property_split = False
    for k, label, _ in passes.PASSES:
        grid.prop_enum(s, "passes", k, text=label)
    if s.passes:
        layout.prop(s, "pass_samples")


def draw_output(layout, context):
    sc = context.scene
    r = sc.render
    layout.prop(sc, "camera")

    col = layout.column(align=True)
    col.prop(r, "resolution_x", text="Resolution X")
    col.prop(r, "resolution_y", text="Y")
    col.prop(r, "resolution_percentage", text="%")

    col = layout.column(align=True)
    col.prop(sc, "frame_start", text="Frame Start")
    col.prop(sc, "frame_end", text="End")
    col.prop(sc, "frame_step", text="Step")

    layout.prop(r, "filepath", text="Folder")
    layout.template_image_settings(r.image_settings, color_management=False)
    layout.prop(r, "film_transparent", text="Transparent Background")
    if r.is_movie_format:
        layout.prop(sc.dlss_render_plus, "keep_movie_frames")


def draw_render_context(self, context):
    """Drawn inside Blender's own panel at the top of the Render tab, right under the Render Engine menu, so it is
    there for every engine (a separate panel always lands below all of the engine's panels)."""
    if context.scene is None:
        return
    layout = self.layout
    header, body = layout.panel("dlss_render_plus", default_closed=False)
    header.label(text="DLSS Render Plus")
    if not body:
        return
    body.use_property_split = True
    body.use_property_decorate = False
    draw_main(body, context)

    header, sub = body.panel("dlss_render_plus_passes", default_closed=SECTIONS_CLOSED)
    header.label(text="Render Passes")
    if sub:
        draw_passes(sub, context)
    header, sub = body.panel("dlss_render_plus_queue", default_closed=SECTIONS_CLOSED)
    header.label(text="Render Queue")
    if sub:
        render_queue.draw(sub, context)
    header, sub = body.panel("dlss_render_plus_output", default_closed=SECTIONS_CLOSED)
    header.label(text="Output")
    if sub:
        draw_output(sub, context)
    draw_credit(body)


def draw_credit(layout):
    """Credit for the original add-on this one is built on."""
    box = layout.box()
    row = box.row()
    row.alignment = "CENTER"
    row.label(text="Based on DLSS Render by")
    row.label(text="", icon="FUND")
    row.label(text=f"|  {AUTHOR}")
    row = box.row(align=True)
    row.operator("wm.url_open", text="YouTube", icon="PLAY").url = YOUTUBE_URL
    row.operator("wm.url_open", text="armin5d.com", icon="URL").url = WEBSITE_URL
    row = box.row()
    row.operator("wm.url_open", text="DLSS Render Plus on GitHub", icon="SCRIPT").url = REPO_URL


classes = render_queue.classes[:1] + (DLSSRenderSettings, DLSSRenderPrefs) + render.classes + render_queue.classes[1:]


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.dlss_render_plus = bpy.props.PointerProperty(type=DLSSRenderSettings)
    bpy.types.RENDER_PT_context.append(draw_render_context)


def unregister():
    bpy.types.RENDER_PT_context.remove(draw_render_context)
    del bpy.types.Scene.dlss_render_plus
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
