"""Remove build artefacts: build/, dist/, .venv-build/, __pycache__, .pytest_cache."""
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for d in ("build", "dist", ".venv-build", ".pytest_cache"):
    shutil.rmtree(ROOT / d, ignore_errors=True)
for p in ROOT.rglob("__pycache__"):
    shutil.rmtree(p, ignore_errors=True)
print("clean")
