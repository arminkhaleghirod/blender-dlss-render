# Testing

The scripts in `tests/` run inside Blender. Output goes to `DLSSPLUS_TEST_DIR`, or `<temp>/dlssplus` if that isn't set. Install the add-on first (it's imported as `bl_ext.user_default.dlss_render_plus`). A separate Blender config keeps the tests away from your own preferences:

```bash
export BLENDER_USER_RESOURCES=/path/to/scratch/blender-config
blender --command extension build --source-dir dlss_render_plus --output-dir dist
blender --command extension install-file -r user_default -e dist/dlss_render_plus-1.0.0.zip
```

## Headless tests

```bash
blender -b --python tests/headless.py
```

These don't need DLSS, only an NVIDIA GPU for the passes test and ffmpeg (with ffprobe) on the PATH. They check:

- the add-on registers its settings and operators
- Steady reduces flicker on synthetic grain, leaves no trail behind a moving edge, resets on a cut, and how long a 4K frame takes
- movie output for H.264, H.265, AV1, VP9 with alpha, ProRes, and FFV1, each with the exact frame count, including frame ranges with gaps
- render passes: every EXR part survives the merge, Combined is replaced and right side up, Cryptomatte metadata is kept, depth values are sensible
- the queue's progress file and frame lists

```bash
blender -b --python tests/headless_extra.py
```

- disabling and enabling the add-on three times in a row
- installed next to the original DLSS Render (`dlss_render`): both register, and ours keeps working after the original is disabled
- queue entries and settings survive saving and reopening the .blend, and the progress file sits next to it
- movie names with `#` in folder names and `###` padding
- Output > Overwrite off keeps existing frames
- Render Queue (not Resume) deletes leftover movie frames
- two entries writing the same files are refused, a copied scene's entry gets its own id, and an entry with its own output is accepted
- a relative output path in an unsaved file is refused before rendering

## Live DLSS tests

```bash
blender --python tests/gui_test.py
```

This one needs a window (not `-b`), an RTX card and working DLSS. It builds a small scene with a glass sphere, an animated camera and a marker that cuts to a second camera, runs every feature for real and quits. Results are in `out_gui/results.json`. It checks:

- a still and a 24-frame animation with Steady, with no black frames
- the camera cut: the change from the last frame of the old shot to the first of the new one has to be far larger than the change between frames inside the new shot
- an H.264 movie with the right number of frames, and the user's movie settings restored exactly afterwards
- render passes on 6 frames, with the output settings restored and DLSS's frame history intact (frame-to-frame change compared with the same frames rendered without passes)
- a 4K render in tiles
- the queue: Esc after 3 frames, then Resume finishes the rest without touching the frames already saved, and puts the scene and camera back
- a movie in the queue: Esc after 4 frames, one frame file deleted, Resume renders what's missing and makes the full movie

```bash
blender --python tests/gui_edge.py
```

The edge cases, each followed by a check that the viewport, full screen, overlays and sample settings are back:

- a transparent PNG (background alpha 0, objects 1)
- JPEG at 50% of an odd resolution with frame step 2, with viewport adaptive sampling on (this used to stall small frames for 15 s each)
- a single-layer EXR
- a movie at an odd size with frame step 2 (ffmpeg pads it to even)
- Esc while ffmpeg encodes (no ffmpeg left running), Esc during a passes render (no half-written EXR, settings back)
- ffmpeg missing, and an output folder that can't be created: both refused before anything changes
- a 1080p frame forced into tiles compared with the same frame in one tile: no seam
- 60 frames across the 50-frame restart and a marker cut: no black frames, no brightness jumps
- a second view layer
- saving the .blend mid-render: the saved file has the user's settings, not the render's, and the render stops
- autosave every minute during a 75-frame render: all 75 frames are rendered
- disabling the add-on mid-render, and opening another file mid-render: the render stops and nothing keeps running

```bash
blender --python tests/align_gui.py
blender -b --python tests/align_cmp.py
```

Renders the showcase frame once per DLSS upscale mode and measures its sub-pixel offset against a Cycles render (phase correlation), to check that the picture lines up. `DLSSPLUS_ALIGN_RES=1920x1080` and `DLSSPLUS_ALIGN_MODES=NONE,BALANCED` pick other sizes and modes.

This is how a 0.6 pixel vertical offset was found, the same in every mode and on both the OpenGL and Vulkan backends. With the correction in `render.py` (`NUDGE_Y`) the offset is:

| Size | Modes | Horizontal | Vertical |
|---|---|---|---|
| 1280 x 720 | all five | 0.008 to 0.019 px | -0.039 to -0.010 px |
| 1920 x 1080 | None, Balanced | 0.017 to 0.019 px | -0.009 to 0.002 px |
| 1001 x 777 | None, Balanced | 0.001 to 0.006 px | -0.034 to -0.025 px |

Last run on Blender 5.3 beta (f0406d0cb6f5), RTX 4080, driver 616.56, Windows 11: headless 38/38, headless_extra 12/12, gui_test 17/17, gui_edge 28/28.

## Benchmarks

The README numbers come from:

```bash
blender --python tests/bench_gui.py        # DLSS side, needs a window
blender -b --python tests/bench_cycles.py  # Cycles with Blender's default settings
```

Both build the scene in `tests/showcase.py` at 1920 x 1080: a subdivided monkey with a subsurface material, a glass sphere, a gold torus, two area lights and a camera with depth of field moving slowly over 48 frames.

- DLSS: Balanced quality, 12 frames of the animation, time measured between saved frames (the start-up isn't counted).
- Cycles: 4096 samples max, adaptive threshold 0.01, OpenImageDenoise. A warm-up render first, then frames 1, 6 and 12. The 128-sample figure used the same settings with 128 samples.
- Steady: 16 frames of a static close-up of the glass (camera `CamGlass`), off and on. Flicker is the per-pixel standard deviation of brightness over frames 5 to 16.

| | Seconds per frame |
|---|---|
| Cycles, Blender defaults | 17.0 |
| Cycles, 128 samples | 4.4 |
| DLSS Render Plus, Balanced | 0.98 |
| DLSS Render Plus, Balanced + Steady | 1.06 |

RTX 4080, Ryzen 7 9800X3D, Blender 5.3 beta, Windows 11.
