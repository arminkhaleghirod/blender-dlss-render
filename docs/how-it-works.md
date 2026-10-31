# How it works

Blender 5.3 can denoise the Cycles viewport with NVIDIA's DLSS Ray Reconstruction. A final render (F12) can't use it, and Python has no way to reach the denoiser directly. So the add-on uses the viewport as the renderer.

## One frame

1. A 3D Viewport is maximized and put in camera view. Overlays, gizmos, header and sidebars are hidden.
2. The camera view is zoomed until one output pixel is exactly one screen pixel, then nudged by half a DLSS pixel so the picture lines up with where a normal render puts it.
3. The viewport switches to Rendered shading with DLSS as the denoiser and no sample cap.
4. The add-on watches the picture itself (a few rows and columns of pixels) and counts how often it changes. Each change is one more sample that DLSS has cleaned up. When the count reaches the quality setting (24 on Balanced), the frame is done. Scenes that stop changing early, like flat untextured shots, finish early.
5. The picture is read straight from the viewport's framebuffer before Blender draws over it. It's scene-linear float with the render's alpha, so it gets saved through Blender's normal color management and file settings, the same way a final render is.

Before any of this, every setting that differs between viewport and render is switched to the render value: hidden objects and collections, modifiers that are off in the viewport, subdivision levels, particle display amounts, Simplify. All of it is put back afterwards.

## Animations

DLSS is temporal: each picture builds on the ones before it. That's where most of the quality comes from, and it's also where ghosting comes from.

- The render starts 8 frames before the first saved frame, so frame 1 already has history behind it.
- Every 50 frames the viewport render is restarted. Long runs without a restart sometimes left patches black or with the wrong material in the original add-on's tests.
- DLSS Render Plus also looks for camera cuts: a different camera from a timeline marker, a turn of more than 25 degrees in one frame, or a move far beyond the camera's recent pace. On a cut the viewport render restarts on the new shot, with extra samples and no play-in from the old one, so nothing from the previous shot gets blended in.
- After a Resume skips frames that were already saved, the play-in also never reaches back across a marker cut.

## Outputs bigger than the screen

If the output doesn't fit in the viewport, it's rendered in tiles. The camera view is panned across the frame, tiles overlap by at least 64 pixels, and the seams are blended over 48 pixels. A 4K frame on a 1080p monitor is 4 to 6 tiles.

## Movie output

The frames are saved as 16-bit PNG next to the movie, in `<movie name>_frames`. When the last one is done, ffmpeg turns them into the movie with the container, codec and quality from Blender's own output settings (H.264, H.265, AV1, VP9, ProRes, FFV1, DNxHR, PNG and QuickTime RLE are mapped). The frames are deleted afterwards unless you tick Keep Movie Frames.

Because the frames are files on disk, a stopped movie render can be resumed from the queue. Missing frames are rendered again and the movie is made at the end.

## Render passes

DLSS only gives the beauty picture. For the passes, each frame also gets a short regular Cycles render (16 samples by default, no denoising) with only the data passes you picked. Blender writes that to a temporary multilayer EXR, one part per pass. The add-on reads every part with OpenImageIO (bundled with Blender), swaps the Combined layer for the DLSS picture, and writes the result as your frame. Cryptomatte manifests and other metadata are copied over, so the file opens in Blender's compositor, Nuke or Resolve like a normal multilayer render.

The passes are exact because they come from Cycles, not from the viewport. They cost the time of that short render on every frame.

## Steady Glass & Noise

DLSS leaves some grain on glass and fine detail, and in an animation that grain changes every frame. Steady blends each frame with the previous result, but first clamps the previous result to the range of colors in the 3x3 pixels around each pixel of the new frame. Grain keeps that range wide, so the history passes and the flicker averages out. A moving edge falls outside the range, so the history is cut off there and nothing trails behind. A frame that differs from the last one almost everywhere starts the history over.

It runs on the CPU in NumPy, about 0.7 seconds for a 4K frame on a Ryzen 7 9800X3D.

## The queue

Queue entries are stored in the scene, so they're saved with the .blend. Each entry is a scene, an optional camera, and a frame range, and renders with that scene's DLSS settings. Every saved frame is written to `<blend name>.dlss_queue.json` next to the .blend. Resume reads it and renders only what's missing. Movie entries check the frame files on disk instead, so a deleted frame gets rendered again.

While a render or queue runs, Windows is kept awake (screen included, since the viewport has to keep drawing), and the status bar shows the frame, samples, progress and the time left.
