"""VIII.6 — perceptual-hash visual regression.

Differences above the threshold raise a ``VisualRegressionWarning`` (surfaced by
CI) rather than failing: intended visual changes are legitimate. Update the
baseline with ``python scripts/update_visual_baseline.py`` after reviewing.
"""

import warnings

import numpy as np
import pytest

from visual_support import hamming, load_baseline, phash, render_cases

THRESHOLD_BITS = 6


class VisualRegressionWarning(UserWarning):
    pass


@pytest.fixture(scope="module")
def frames(tmp_path_factory):
    return render_cases(tmp_path_factory.mktemp("vr-tiles"))


def test_phash_against_baseline(frames):
    base = load_baseline()
    if not base:
        pytest.skip("no visual baseline recorded yet (run scripts/update_visual_baseline.py)")
    drift = {}
    for key, img in frames.items():
        if key in base:
            d = hamming(phash(img), base[key])
            if d > THRESHOLD_BITS:
                drift[key] = d
    if drift:
        warnings.warn(VisualRegressionWarning(f"perceptual drift above {THRESHOLD_BITS} bits: {drift}"))
    missing = set(base) - set(frames)
    assert not missing, f"baseline cases no longer rendered: {missing}"


def test_render_is_deterministic(tmp_path):
    a = render_cases(tmp_path / "a")
    b = render_cases(tmp_path / "b")
    for k in a:
        assert np.array_equal(np.asarray(a[k]), np.asarray(b[k])), k


def test_themes_are_visually_distinct(frames):
    hs = {k.split("/")[0]: phash(v) for k, v in frames.items() if k.endswith("/300")}
    vals = list(hs.values())
    for i in range(len(vals)):
        for j in range(i + 1, len(vals)):
            assert hamming(vals[i], vals[j]) > 0
