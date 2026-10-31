# Changelog

## 1.0.0

First release, forked from DLSS Render 1.0.2 (Free) by Armin5D.

Added:

- Render queue with Resume. Scenes, cameras and frame ranges render one after another, progress is saved after every frame, and Resume renders only what's missing.
- Movie output through ffmpeg, from lossless 16-bit PNG frames, using Blender's container, codec and quality settings.
- Render passes saved with the DLSS picture as OpenEXR MultiLayer, from a short Cycles data render per frame. Cryptomatte metadata is kept.
- Steady Glass & Noise, a temporal filter against frame-to-frame flicker.
- Camera cut detection (marker camera switches, sharp turns, jumps). The DLSS history is reset on a cut so the previous shot doesn't ghost into the next.
- Status bar line with frame, samples, progress, time left and finish time. Windows stays awake while rendering. A chime plays when an animation or queue ends.
- ffmpeg path in the add-on preferences.

Changed:

- The add-on id is `dlss_render_plus`, so it installs next to the original instead of replacing it.
- Objects are hidden and shown again in the window's own view layer, which matters when the queue switches scenes.
- Output format settings are remembered before any of them change and restored in an order Blender accepts, so a movie or passes render no longer changes your color depth, color mode or output color space.
- The picture is now lined up with a Cycles render to within 0.05 pixels. It used to sit 0.6 pixels off vertically in every upscale mode, which also put Cycles passes (Cryptomatte, depth) slightly out of line with the DLSS picture.
- Disabling the add-on during a render could crash Blender: the render's timer kept running against a closed viewport. It's now stopped reliably.
- An output folder that can't be created is reported before the render starts.
- Viewport adaptive sampling is off during the render. Small or simple frames used to stop sampling early and then wait 15 seconds each.
- Saving the .blend during a render puts every setting back first and stops the render, so the file is saved the way you left it. Opening another file or disabling the add-on stops a render cleanly.
- Output > Overwrite off is respected for image sequences.
- A relative output path in an unsaved file is caught before the render starts.
- `#` in folder names is kept in movie paths, and `###` in the file name sets the padding.
- Queue: entries can have their own output path, two entries writing the same files are refused, Render Queue (not Resume) deletes leftover movie frames, copied scenes no longer share queue progress, and a locked progress file no longer stops the render.
- ffmpeg is also looked for in the usual Homebrew and Linux locations.
