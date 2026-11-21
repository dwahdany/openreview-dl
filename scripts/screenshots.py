"""Regenerate the pictures in the README (docs/images/*.png).

    uv run python scripts/screenshots.py

Everything shown is made up: the selector runs headlessly on invented
submissions, so no OpenReview account and no terminal are involved. Textual
writes each screen as an SVG, and a headless Chrome or Chromium turns that
into a PNG (it fetches the SVG's web font, Fira Code, from cdnjs). The
browser is found on PATH or in /Applications; set CHROME to name another.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from openreview_dl import picker
from openreview_dl.discover import Submission

IMAGES = Path(__file__).resolve().parent.parent / "docs" / "images"

_ROWS = [
    # role, venue, number, status, title
    ("Author", "ICLR.cc/2026/Conference", 4127, "Under review",
     "Sparse Routing Layers for Long-Context Retrieval"),
    ("Author", "ICLR.cc/2026/Conference", 9310, "Under review",
     "When Do Synthetic Captions Help? A Controlled Study"),
    ("Author", "NeurIPS.cc/2025/Conference", 11842, "NeurIPS 2025 poster",
     "Calibrated Abstention under Label Shift"),
    ("Author", "NeurIPS.cc/2025/Conference", 2206, "Rejected",
     "A Note on Warm-Up Schedules for Small Batches"),
    ("Author", "TMLR", 5531, "Accepted",
     "Revisiting Early Stopping with Noisy Validation Sets"),
    ("Reviewer", "ICLR.cc/2026/Conference", 733, "Under review",
     "Token Merging without Retraining"),
    ("Reviewer", "ICLR.cc/2026/Conference", 5068, "Under review",
     "Private Fine-Tuning with Public Feature Extractors"),
    ("Reviewer", "ICLR.cc/2026/Conference", 12954, "Withdrawn",
     "On the Geometry of Prompt Ensembles"),
    ("Reviewer", "ICML.cc/2025/Conference", 8421, "ICML 2025 spotlight",
     "Membership Inference Is Harder Than It Looks"),
    ("Reviewer", "ICML.cc/2025/Conference", 1977, "Rejected",
     "Scaling Laws for Data Pruning"),
    ("Area Chair", "ICLR.cc/2026/Workshop/DATA-FM", 12, "Under review",
     "Deduplicating Web-Scale Image-Text Pairs"),
    ("Area Chair", "ICLR.cc/2026/Workshop/DATA-FM", 31, "Under review",
     "What a Filter Removes: Auditing Quality Classifiers"),
    ("Area Chair", "ICLR.cc/2026/Workshop/DATA-FM", 47, "Under review",
     "Curriculum Sampling from Retrieval Scores"),
]


def demo_items() -> list[Submission]:
    return [
        Submission(
            forum=f"demo{index:06d}",
            number=number,
            title=title,
            venue_id=venue,
            status=status,
            roles=[role],
        )
        for index, (role, venue, number, status, title) in enumerate(_ROWS)
    ]


_TICKS = ["space", "down", "down", "space", "down", "down", "down", "space", "down"]
SHOTS = {
    # name: (theme, (columns, rows), keys pressed before the picture is taken)
    "picker-dark": ("textual-dark", (100, 29), _TICKS),
    "picker-light": ("textual-light", (100, 29), _TICKS),
    "picker-filter": ("textual-dark", (100, 10), ["space", "slash", *"reviewer iclr", "enter", "a"]),
}

_BROWSERS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


def find_browser() -> str | None:
    for candidate in (os.environ.get("CHROME"), *_BROWSERS):
        if candidate and (found := shutil.which(candidate)):
            return found
    return None


async def shoot(name: str, theme: str, size: tuple[int, int], keys: list[str], directory: Path) -> Path:
    app = picker.build_app(demo_items())
    app.title = "openreview-dl --pick"  # the window title in the picture
    async with app.run_test(size=size) as pilot:
        app.theme = theme
        for key in keys:
            await pilot.press(key)
        await pilot.pause()
        app.save_screenshot(filename=f"{name}.svg", path=str(directory))
    return directory / f"{name}.svg"


def to_png(browser: str, svg: Path, png: Path) -> None:
    box = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg.read_text(encoding="utf-8"))
    width, height = (math.ceil(float(value)) for value in box.groups())
    subprocess.run(
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--default-background-color=00000000",  # transparent window corners
            "--force-device-scale-factor=2",
            f"--window-size={width},{height}",
            "--virtual-time-budget=5000",  # wait for the web font
            f"--screenshot={png}",
            svg.as_uri(),
        ],
        check=True,
        capture_output=True,
    )


def main() -> int:
    browser = find_browser()
    if browser is None:
        print("No Chrome or Chromium found; set CHROME to the browser's executable.")
        return 1
    IMAGES.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for name, (theme, size, keys) in SHOTS.items():
            svg = asyncio.run(shoot(name, theme, size, keys, Path(tmp)))
            png = IMAGES / f"{name}.png"
            to_png(browser, svg, png)
            print(png)
    return 0


if __name__ == "__main__":
    sys.exit(main())
