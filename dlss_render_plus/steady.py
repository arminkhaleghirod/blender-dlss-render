"""Steady Glass & Noise: calms the frame-to-frame flicker of an animation.

Each frame is blended with the previous result, but the previous result is first clamped to the range of colours
found around each pixel in the new frame (3 x 3). Grain on glass keeps that range wide, so the history passes and the
flicker averages out; a moving edge falls outside it, so the history is cut off there and nothing ghosts behind.
A frame that differs from the last one almost everywhere (a camera cut) starts the history again.
"""
import numpy as np

LUMA = np.array([0.2126, 0.7152, 0.0722], np.float32)
MATCH = 0.4         # relative brightness difference (after clamping) where the history fades out
CUT = 0.3           # mean relative difference over the frame that counts as a new shot


def _box(a, fn):
    """3 x 3 minimum or maximum (separable), edges repeated."""
    p = np.pad(a, ((1, 1), (0, 0), (0, 0)), mode="edge")
    a = fn(fn(p[:-2], p[1:-1]), p[2:])
    p = np.pad(a, ((0, 0), (1, 1), (0, 0)), mode="edge")
    return fn(fn(p[:, :-2], p[:, 1:-1]), p[:, 2:])


def _rel(a, b):
    la, lb = a[..., :3] @ LUMA, b[..., :3] @ LUMA
    return np.abs(la - lb) / (np.abs(la) + np.abs(lb) + 1e-3)


class Steady:
    def __init__(self, strength):
        self.k = float(strength)
        self.hist = None

    def reset(self):
        self.hist = None

    def apply(self, cur):
        """cur: (H, W, 4) float RGBA. Returns the steadied frame (a new array)."""
        cur = np.asarray(cur, np.float32)
        if self.hist is None or self.hist.shape != cur.shape or self.k <= 0.0:
            self.hist = cur.copy()
            return cur
        if float(_rel(cur, self.hist).mean()) > CUT:
            self.hist = cur.copy()
            return cur
        h = np.clip(self.hist, _box(cur, np.minimum), _box(cur, np.maximum))
        w = (self.k * np.exp(-(_rel(cur, h) / MATCH) ** 2))[..., None]
        out = cur + (h - cur) * w
        self.hist = out
        return out
