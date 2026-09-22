"""Easing curves, splines and signal filters used by the camera system.

All easing functions map [0, 1] → [0, 1] (``ease_out_back_damped`` may
overshoot slightly by design) and have a continuous first derivative, which is
the property Section VI.1 requires to avoid visible jerk.
"""

from __future__ import annotations

import math
from typing import Callable, Dict, List, Sequence


def clamp01(t: float) -> float:
    return 0.0 if t < 0 else 1.0 if t > 1 else t


def linear(t: float) -> float:
    return clamp01(t)


def smoothstep(t: float) -> float:
    t = clamp01(t)
    return t * t * (3.0 - 2.0 * t)


def smootherstep(t: float) -> float:
    """Quintic; first and second derivatives are zero at both ends."""
    t = clamp01(t)
    return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)


def ease_in_out_cubic(t: float) -> float:
    t = clamp01(t)
    return 4.0 * t ** 3 if t < 0.5 else 1.0 - ((-2.0 * t + 2.0) ** 3) / 2.0


def ease_out_cubic(t: float) -> float:
    t = clamp01(t)
    return 1.0 - (1.0 - t) ** 3


def ease_in_out_sine(t: float) -> float:
    t = clamp01(t)
    return -(math.cos(math.pi * t) - 1.0) / 2.0


def ease_out_back_damped(t: float, overshoot: float = 0.6) -> float:
    """ease-out-back with a reduced overshoot constant (default ~2.4 % peak)."""
    t = clamp01(t)
    c1 = overshoot
    c3 = c1 + 1.0
    return 1.0 + c3 * (t - 1.0) ** 3 + c1 * (t - 1.0) ** 2


EASINGS: Dict[str, Callable[[float], float]] = {
    "linear": linear,
    "smoothstep": smoothstep,
    "smootherstep": smootherstep,
    "ease-in-out-cubic": ease_in_out_cubic,
    "ease-out-cubic": ease_out_cubic,
    "ease-in-out-sine": ease_in_out_sine,
    "ease-out-back": ease_out_back_damped,
}


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def log_lerp(a: float, b: float, t: float) -> float:
    """Interpolate a scale value geometrically (perceptually uniform zoom)."""
    return math.exp(math.log(a) + (math.log(b) - math.log(a)) * t)


def catmull_rom(p0: float, p1: float, p2: float, p3: float, t: float) -> float:
    """Uniform Catmull-Rom segment between p1 and p2 (C1 continuous)."""
    t2 = t * t
    t3 = t2 * t
    return 0.5 * ((2 * p1) + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2
                  + (-p0 + 3 * p1 - 3 * p2 + p3) * t3)


def catmull_rom_sample(values: Sequence[float], position: float) -> float:
    """Sample a Catmull-Rom spline through ``values`` at fractional index."""
    n = len(values)
    if n == 0:
        raise ValueError("empty spline")
    if n == 1:
        return values[0]
    position = max(0.0, min(n - 1.0, position))
    i = min(int(math.floor(position)), n - 2)
    t = position - i
    p0 = values[max(i - 1, 0)]
    p1 = values[i]
    p2 = values[i + 1]
    p3 = values[min(i + 2, n - 1)]
    return catmull_rom(p0, p1, p2, p3, t)


def critically_damped_smooth(values: Sequence[float], stiffness: float) -> List[float]:
    """Forward-backward critically damped second-order low-pass filter.

    Running the filter forward then backward gives zero phase lag (the camera
    does not trail behind the subject) while removing high-frequency jitter.
    ``stiffness`` in (0, 1]: 1 = no smoothing.
    """
    if not values:
        return []
    k = max(1e-4, min(1.0, stiffness))

    def one_pass(seq: Sequence[float]) -> List[float]:
        out = []
        pos = seq[0]
        vel = 0.0
        omega = k * 2.0
        for target in seq:
            # semi-implicit critically damped spring step (dt = 1 sample)
            accel = omega * omega * (target - pos) - 2.0 * omega * vel
            vel += accel
            pos += vel
            out.append(pos)
        return out

    if k >= 0.999:
        return list(values)
    fwd = one_pass(values)
    back = one_pass(fwd[::-1])[::-1]
    return back


def gaussian_smooth(values: Sequence[float], sigma: float) -> List[float]:
    """Zero-phase Gaussian low-pass with edge clamping (no lag, no overshoot)."""
    import numpy as np

    n = len(values)
    if n == 0 or sigma <= 0.05:
        return list(values)
    radius = max(1, int(3 * sigma))
    k = np.exp(-0.5 * (np.arange(-radius, radius + 1) / sigma) ** 2)
    k /= k.sum()
    arr = np.asarray(values, dtype=np.float64)
    padded = np.concatenate([np.full(radius, arr[0]), arr, np.full(radius, arr[-1])])
    return np.convolve(padded, k, mode="valid").tolist()


def second_derivative(values: Sequence[float]) -> List[float]:
    return [values[i - 1] - 2 * values[i] + values[i + 1] for i in range(1, len(values) - 1)]


def spike_ratio(values: Sequence[float]) -> float:
    """Max |second derivative| relative to its median — a jerk spike metric.

    Used by the camera verification criterion (Section VI.1): a smooth path has
    a small ratio; a hard corner or snap produces a large one.
    """
    d2 = [abs(v) for v in second_derivative(values)]
    if not d2:
        return 0.0
    s = sorted(d2)
    median = s[len(s) // 2]
    p99 = s[min(len(s) - 1, int(len(s) * 0.99))]
    scale = max(median, p99 * 1e-3, 1e-12)
    return max(d2) / scale


def zoom_pan_interpolator(c0: tuple, c1: tuple, rho: float = 1.41421356):
    """van Wijk & Nuij (2003) "smooth and efficient zooming and panning".

    ``c0``/``c1`` are (cx, cy, span). Returns f(t) → (cx, cy, span) for t in
    [0, 1] such that perceived on-screen velocity is constant — the camera
    zooms out, pans, and zooms back in along an optimal path instead of
    panning linearly while the scale changes exponentially (which makes the
    image appear to whip at the end of a zoom-in).
    """
    ux0, uy0, w0 = c0
    ux1, uy1, w1 = c1
    w0 = max(w0, 1e-9)
    w1 = max(w1, 1e-9)
    dx, dy = ux1 - ux0, uy1 - uy0
    d2 = dx * dx + dy * dy
    rho2, rho4 = rho * rho, rho ** 4
    if d2 < 1e-12 * max(w0, w1) ** 2:
        big_s = math.log(w1 / w0) / rho

        def f(t: float):
            return ux0 + t * dx, uy0 + t * dy, w0 * math.exp(rho * t * big_s)
        return f
    d1 = math.sqrt(d2)
    b0 = (w1 * w1 - w0 * w0 + rho4 * d2) / (2 * w0 * rho2 * d1)
    b1 = (w1 * w1 - w0 * w0 - rho4 * d2) / (2 * w1 * rho2 * d1)
    r0 = math.log(math.sqrt(b0 * b0 + 1) - b0)
    r1 = math.log(math.sqrt(b1 * b1 + 1) - b1)
    big_s = (r1 - r0) / rho
    cosh_r0 = math.cosh(r0)
    sinh_r0 = math.sinh(r0)

    def g(t: float):
        s_ = t * big_s
        u = w0 / (rho2 * d1) * (cosh_r0 * math.tanh(rho * s_ + r0) - sinh_r0)
        return ux0 + u * dx, uy0 + u * dy, w0 * cosh_r0 / math.cosh(rho * s_ + r0)
    return g
