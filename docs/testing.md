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

Last run on Blender 5.3 beta (f0406d0cb6f5), RTX 4080, driver 616.56: everything passed.

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
