"""Movie output: the job saves lossless 16-bit PNG frames next to the movie, then ffmpeg turns them into the movie
with the scene's container, codec and quality."""
import os
import re
import shutil
import subprocess
from fractions import Fraction

import bpy

EXT = {"MPEG4": ".mp4", "MKV": ".mkv", "QUICKTIME": ".mov", "WEBM": ".webm", "AVI": ".avi", "OGG": ".ogv",
       "FLASH": ".flv", "MPEG1": ".mpg", "MPEG2": ".mpg", "DV": ".dv"}
CRF = {"LOSSLESS": 0, "PERC_LOSSLESS": 17, "HIGH": 20, "MEDIUM": 23, "LOW": 26, "VERYLOW": 29, "LOWEST": 32}
EVEN = "pad=ceil(iw/2)*2:ceil(ih/2)*2"
BT709 = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]


class MovieError(Exception):
    pass


# where package managers put ffmpeg; a Blender started from the Dock or a desktop menu doesn't see the shell's PATH
FFMPEG_PLACES = ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg", "/snap/bin/ffmpeg")


def find_ffmpeg(configured=""):
    path = bpy.path.abspath(configured) if configured else ""
    if path and os.path.isfile(path):
        return path
    return shutil.which("ffmpeg") or next((p for p in FFMPEG_PLACES if os.path.isfile(p)), None)


def movie_path(scene, first, last):
    """Where Blender itself would put the movie: the output path with the frame range, e.g. render_0001-0250.mp4.
    #### in the file name sets the padding of the numbers; # in folder names is left alone."""
    r = scene.render
    folder, name = os.path.split(bpy.path.abspath(r.filepath or "//"))
    run = re.search(r"#+", name)
    if run:
        n = len(run.group())
        name = name[:run.start()] + f"{first:0{n}d}-{last:0{n}d}" + name[run.end():]
        path = os.path.join(folder, name)
    else:
        path = os.path.join(folder, f"{name}{first:04d}-{last:04d}")
    if r.use_file_extension:
        path += EXT.get(r.ffmpeg.format, ".mp4")
    return os.path.normpath(path)


def frames_dir(scene, frames):
    """Where the lossless frames of the movie go: next to it, <movie name>_frames."""
    return os.path.splitext(movie_path(scene, frames[0], frames[-1]))[0] + "_frames"


def frame_file(folder, frame):
    return os.path.join(folder, f"frame_{frame:06d}.png")


def codec_args(scene, alpha):
    ff = scene.render.ffmpeg
    codec = getattr(ff, "codec", None) or getattr(ff, "video_codec", "H264")
    crf = CRF.get(ff.constant_rate_factor)
    quality = ["-crf", str(crf)] if crf is not None else ["-b:v", f"{ff.video_bitrate}k"]
    yuv = ["-vf", EVEN + ",scale=out_color_matrix=bt709:out_range=tv"] + BT709
    if codec == "H265":
        return ["-c:v", "libx265", "-pix_fmt", "yuv420p", "-tag:v", "hvc1"] + quality + yuv
    if codec == "AV1":
        return ["-c:v", "libsvtav1", "-pix_fmt", "yuv420p"] + quality + yuv
    if codec in {"WEBM", "VP9"}:
        q = quality + (["-b:v", "0"] if crf is not None else [])
        return ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p" if alpha else "yuv420p"] + q + yuv
    if codec == "PRORES":
        if alpha:
            return ["-c:v", "prores_ks", "-profile:v", "4", "-pix_fmt", "yuva444p10le"] + BT709
        return ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le"] + BT709
    if codec == "FFV1":
        return ["-c:v", "ffv1", "-level", "3", "-pix_fmt", "rgba64le" if alpha else "rgb48le"]
    if codec == "PNG":
        return ["-c:v", "png", "-pix_fmt", "rgba64be" if alpha else "rgb48be"]
    if codec == "QTRLE":
        return ["-c:v", "qtrle", "-pix_fmt", "argb" if alpha else "rgb24"]
    if codec == "DNXHD":
        return ["-c:v", "dnxhd", "-profile:v", "dnxhr_hq", "-pix_fmt", "yuv422p"] + BT709
    # H264 and anything ffmpeg has no better match for
    preset = {"BEST": "slow", "GOOD": "medium", "REALTIME": "veryfast"}.get(ff.ffmpeg_preset, "medium")
    return ["-c:v", "libx264", "-preset", preset, "-pix_fmt", "yuv420p"] + quality + yuv


class Movie:
    def __init__(self, scene, all_frames, ffmpeg):
        if not ffmpeg:
            raise MovieError("Movie output needs ffmpeg. Install it or set its path in the add-on's preferences.")
        r = scene.render
        self.scene = scene
        self.ffmpeg = ffmpeg
        self.frames = list(all_frames)
        self.path = movie_path(scene, self.frames[0], self.frames[-1])
        self.dir = frames_dir(scene, self.frames)
        self.fps = Fraction(r.fps / r.fps_base).limit_denominator(1001)
        self.alpha = r.film_transparent and r.image_settings.color_mode == "RGBA"
        self.proc = None
        self.log_path = os.path.join(self.dir, "ffmpeg_log.txt")

    def frame_file(self, frame):
        return frame_file(self.dir, frame)

    def missing(self):
        return [f for f in self.frames if not os.path.isfile(self.frame_file(f))]

    def command(self, pattern):
        return ([self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                 "-framerate", str(self.fps), "-start_number", "1", "-i", pattern]
                + codec_args(self.scene, self.alpha) + [self.path])

    def start(self):
        """Start ffmpeg in the background. Poll self.proc."""
        # The frames are named by frame number (Resume needs that), which can have gaps (frame step); ffmpeg reads
        # a numbered sequence, so link them as 1, 2, 3... (hard links: no copying)
        seq = os.path.join(self.dir, "seq")
        shutil.rmtree(seq, ignore_errors=True)
        os.makedirs(seq)
        for i, f in enumerate(self.frames, 1):
            dst = os.path.join(seq, f"{i:06d}.png")
            try:
                os.link(self.frame_file(f), dst)
            except OSError:
                shutil.copyfile(self.frame_file(f), dst)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.log = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(self.command(os.path.join(seq, "%06d.png")), stdout=self.log,
                                     stderr=subprocess.STDOUT, cwd=self.dir, creationflags=flags)
        return self.proc

    def finish(self, keep_frames):
        """After ffmpeg ended: raise on failure, else optionally delete the frames."""
        self.log.close()
        if self.proc.returncode != 0 or not os.path.isfile(self.path):
            raise MovieError(f"ffmpeg could not make the movie, see {self.log_path}")
        shutil.rmtree(os.path.join(self.dir, "seq"), ignore_errors=True)
        if not keep_frames:
            shutil.rmtree(self.dir, ignore_errors=True)

    def kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
            self.log.close()
