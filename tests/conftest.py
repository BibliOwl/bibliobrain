from __future__ import annotations

import os
import shutil
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

TEST_TMP_ROOT = ROOT_DIR / "tests_tmp"
TEST_TMP_ROOT.mkdir(exist_ok=True)

# Set before any src import: the suite must never read or write the real
# %LOCALAPPDATA%\bibliobrain (settings, logs, secrets, indexes).
os.environ["BIBLIOBRAIN_HOME"] = str(TEST_TMP_ROOT / "home")
for _variable in ("DATA_PATH", "CHROMA_PATH", "STORAGE_DIR"):
    os.environ.pop(_variable, None)
# Tests fake the PDF loader in-process; the subprocess path has its own tests
# (tests/test_document_isolation.py) that switch isolation back on explicitly.
os.environ["BIBLIOBRAIN_DOCUMENT_ISOLATION"] = "0"


@pytest.fixture
def workspace_tmp_dir() -> Iterator[Path]:
    """Fournit un dossier temporaire local au repo pour eviter les blocages du temp systeme."""
    path = TEST_TMP_ROOT / f"case-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def read_app_javascript() -> str:
    """Read application scripts in the same order as the browser."""
    import re

    html = (ROOT_DIR / "static/index.html").read_text(encoding="utf-8")
    scripts = re.findall(r'<script src="(/static/js/app(?:/[^"?]+|)\.js)\?', html)
    if not scripts:
        raise AssertionError("No application scripts found in static/index.html")
    return "".join(
        (ROOT_DIR / script.lstrip("/")).read_text(encoding="utf-8") for script in scripts
    )
