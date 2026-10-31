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
- Output format settings are remembered before any of them change and restored in an order Blender accepts, so a movie or passes render no longer changes your color depth or color mode.
