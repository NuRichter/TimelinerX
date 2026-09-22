import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

_HOME = tempfile.mkdtemp(prefix="tlx-test-home-")
os.environ["TIMELINERX_HOME"] = _HOME
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FIX = ROOT / "fixtures"


def pytest_sessionstart(session):
    # Qt must be created on the main thread before any render thread starts. A QApplication
    # (widgets) is used so the GUI smoke test can run in the same process.
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        session._nrte_app = QApplication(["nrte-tests"])
    from timelinerx.rendering.qt import ensure_qt_app
    ensure_qt_app()
    if not (FIX / "standard-route.json").exists():
        import make_fixtures
        make_fixtures.main()


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_HOME, ignore_errors=True)


@pytest.fixture
def fix():
    return FIX


@pytest.fixture
def standard_journey():
    from timelinerx.journeys.journey import JourneyConfig, build_journey
    from timelinerx.timeline.parser import load_timeline
    return build_journey(load_timeline(FIX / "standard-route.json", compute_sha=False), JourneyConfig())


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


needs_ffmpeg = pytest.mark.skipif(not have_ffmpeg(), reason="FFmpeg/ffprobe not installed")


def make_project(timeline: Path, **video):
    from timelinerx.projects.project import Project, TimelineRef
    from timelinerx.timeline.parser import sha256_file
    p = Project(name="Test", timeline=TimelineRef(str(timeline), sha256_file(timeline), timeline.stat().st_size))
    p.visual.map_provider = "plain"
    p.visual.theme = "neon_dark_blue"
    p.title.name = "Test"
    p.video.resolution = "480p"
    p.video.duration_s = 6
    p.video.quality = "draft"
    for k, v in video.items():
        setattr(p.video, k, v)
    return p
