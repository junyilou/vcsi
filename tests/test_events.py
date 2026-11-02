import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.fixture(scope = "module")
def clip(tmp_path_factory) -> Path:
    if not have_ffmpeg():
        pytest.skip("ffmpeg is not installed")
    path = tmp_path_factory.mktemp("media") / "clip.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24", "-t", "4",
        "-pix_fmt", "yuv420p", "-y", str(path),
    ], check = True, timeout = 60)
    return path


def run_cli(args: list[str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run([sys.executable, "-m", "vcsi.vcsi", *args],
        cwd = ROOT, env = env, capture_output = True, text = True, timeout = 120)


def events_of(output: str) -> list[dict]:
    events = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        events.append(json.loads(line))
    return events


def test_events_and_preview(clip: Path, tmp_path: Path) -> None:
    work = tmp_path / "work"
    out = tmp_path / "out.jpg"
    result = run_cli(["--events", "--work-dir", str(work), "--preview",
        "-g", "2x2", "-t", "-o", str(out), str(clip)], tmp_path)
    assert result.returncode == 0, result.stderr
    events = events_of(result.stdout)
    kinds = [event["event"] for event in events]
    assert kinds[0] == "run_start"
    assert "probe" in kinds
    assert kinds.count("frame") == 4
    assert kinds[-1] == "done"
    assert out.is_file()
    probe = next(event for event in events if event["event"] == "probe")
    assert probe["duration"] > 0
    assert probe["cells"] == 4
    previews = sorted((work / "preview").glob("*.jpg"))
    assert len(previews) == 4
    assert not list(work.glob("*.bmp"))


def test_invalid_delays(clip: Path, tmp_path: Path) -> None:
    result = run_cli(["--events", "--start-delay-percent", "60",
        "--end-delay-percent", "60", "-g", "2x2", str(clip)], tmp_path)
    assert result.returncode != 0
    events = events_of(result.stdout)
    assert any(event["event"] == "error" for event in events)


def test_webp_output(clip: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.webp"
    work = tmp_path / "work"
    result = run_cli(["--events", "--work-dir", str(work), "--image-format", "webp",
        "--image-quality", "70", "-g", "2x2", "-o", str(out), str(clip)], tmp_path)
    assert result.returncode == 0, result.stderr
    assert out.is_file()
    assert out.read_bytes()[:4] == b"RIFF"
