# Troubleshooting

## "DLSS is not available here"

Blender needs an NVIDIA RTX card, a recent driver (590 or newer), and NVIDIA's DLSS Ray Reconstruction library, `nvngx_dlssd.dll`.

Some Blender 5.3 beta builds ship without that DLL. You can check: if there's no `nvngx_dlssd.dll` next to `blender.exe`, that's the problem. Note the extra `d` at the end. `nvngx_dlss.dll` (without it) is DLSS upscaling, which Blender doesn't use for this.

The fix is to copy an NVIDIA-signed `nvngx_dlssd.dll` into the folder that has `blender.exe`. Many recent games ship one. To find it on your PC, run this in PowerShell (it searches the usual Steam folder; change the path for other game libraries):

```powershell
Get-ChildItem "C:\Program Files (x86)\Steam\steamapps\common" -Recurse -Filter nvngx_dlssd.dll -ErrorAction SilentlyContinue |
  Select-Object FullName, @{n="Version";e={$_.VersionInfo.FileVersion}}, @{n="Signed";e={(Get-AuthenticodeSignature $_.FullName).SignerCertificate.Subject -match "NVIDIA"}}
```

Use one where Signed is True, preferably the newest version. NVIDIA also publishes the DLL in its DLSS SDK on GitHub. Restart Blender afterwards. Blender 5.3's release builds may already include it, in which case none of this is needed.

## Black or partly black frames

The add-on restarts the viewport render every 50 frames to prevent this, and starts each animation a few frames early. If it still happens:

- Don't render anything else on the same graphics card at the same time.
- Make sure the PC doesn't lock the screen during the render. The add-on keeps Windows awake, but some corporate policies override that.
- Render the bad frames again as single images into the same folder.

## Ghosting on some frames

DLSS reuses earlier frames. A camera that jumps between two frames can leave a faint copy of the old picture. DLSS Render Plus restarts the render on camera cuts (marker switches, sharp turns, sudden jumps), which covers most cases. For a very fast move that isn't detected as a cut, re-render that frame as a single image, and keep Steady off for that shot.

## Things missing compared to a normal render

The viewport shows most of what a final render shows, and the add-on switches viewport-only settings to their render values first. Some things still only exist in a final render, such as some particle instances. Render one frame and compare it with F12 before starting a long animation. There's also no motion blur; use the Vector pass with a Vector Blur node if you need it.

## Grain on glass

DLSS gives each picture only a few fresh samples, so bright light seen through glass keeps some grain. Raise the quality (High is 64 samples per frame) or use Custom. In animations, Steady Glass & Noise reduces the flicker between frames, but not the grain inside a single frame.

## Movie output says ffmpeg was not found

Movie output uses ffmpeg, which isn't part of Blender. Install it (for example `winget install Gyan.FFmpeg`) or download a build and set its path in Edit > Preferences > Add-ons > DLSS Render Plus. The preferences show which ffmpeg is being used.

## ProRes with alpha

Blender 5.3 doesn't offer RGBA for ProRes in its output settings, so the add-on can't know you want alpha and writes ProRes 422 HQ. Use FFV1 or PNG in MKV/MOV if you need alpha in a movie, or render an image sequence.

## The queue says "Done" but I want to render again

Render Queue starts every entry from scratch. Resume skips what's done. The progress file is `<blend name>.dlss_queue.json` next to your .blend (or in your temp folder for unsaved files). Deleting it resets every entry.

## Debug output

Start Blender from a terminal with `DLSS_RENDER_DEBUG=1` set to print what the add-on does on each frame: samples seen, redraws, time per frame, camera cuts.
