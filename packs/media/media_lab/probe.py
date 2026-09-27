"""FFprobe adapter. No shell, network protocols or media writes."""

import json
import math
import os
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

from .errors import MediaLabError

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def find_tool(name: str, explicit: str | None = None) -> str:
    requested = explicit or os.environ.get(f"MEDIA_LAB_{name.upper()}")
    if not requested:
        config_path = PROJECT_ROOT / ".tools" / "tool-paths.json"
        if config_path.is_file():
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
                if not isinstance(config, dict):
                    raise ValueError("Expected an object")
                requested = config.get(name)
                if requested is not None and not isinstance(requested, str):
                    raise ValueError("Expected an executable path string")
            except (OSError, ValueError) as exc:
                raise MediaLabError("invalid_tool_config", "Check .tools/tool-paths.json; expected executable paths.") from exc
    if requested:
        found = shutil.which(requested)
    else:
        local = PROJECT_ROOT / ".tools" / "ffmpeg" / "bin" / (
            name + (".exe" if os.name == "nt" else "")
        )
        found = str(local) if local.is_file() else shutil.which(name)
    if not found:
        raise MediaLabError(
            "invalid_tool_config" if requested else "missing_dependency",
            f"{name} was not found. See README.md for setup or set MEDIA_LAB_{name.upper()}.",
        )
    return str(Path(found).resolve())


def run_tool(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            args, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaLabError("probe_timeout", "Media inspection timed out; retry with --probe-timeout.") from exc
    except OSError as exc:
        raise MediaLabError("tool_unavailable", "The media tool could not start; check its installation.") from exc
    if result.returncode:
        # ffprobe cannot reliably distinguish a damaged container from an unsupported one.
        raise MediaLabError(
            "probe_failed", "FFprobe could not inspect this media (damaged or unsupported): "
            + result.stderr.strip()[-2000:],
        )
    return result


def positive_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def finite_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def frame_rate(value) -> str | None:
    try:
        rate = Fraction(str(value))
        return str(rate) if rate > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def normalize(raw: dict) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("streams"), list):
        raise MediaLabError("invalid_metadata", "FFprobe returned no usable stream metadata.")
    streams = raw["streams"]
    if any(not isinstance(stream, dict) for stream in streams):
        raise MediaLabError("invalid_metadata", "FFprobe returned malformed streams.")
    if any(s.get("disposition") is not None and not isinstance(s["disposition"], dict) for s in streams):
        raise MediaLabError("invalid_metadata", "FFprobe returned malformed stream dispositions.")
    container = raw.get("format", {})
    if not isinstance(container, dict):
        raise MediaLabError("invalid_metadata", "FFprobe returned malformed container metadata.")
    videos = [s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")]
    if not videos:
        raise MediaLabError("unsupported_media", "No video stream was found; this increment catalogues video only.")
    video = videos[0]
    width, height = video.get("width"), video.get("height")
    if (type(width) is not int or type(height) is not int or width <= 0 or height <= 0):
        raise MediaLabError("invalid_metadata", "The video has no usable dimensions.")
    tags = container.get("tags") or {}
    video_tags = video.get("tags") or {}
    if not isinstance(tags, dict) or not isinstance(video_tags, dict):
        raise MediaLabError("invalid_metadata", "FFprobe returned malformed tags.")
    return {
        "schema_version": 1,
        "container": container.get("format_name"),
        "duration_seconds": positive_number(container.get("duration")),
        "container_start_seconds": finite_number(container.get("start_time")),
        "video": {
            "stream_index": video.get("index"), "codec": video.get("codec_name"),
            "width": width, "height": height,
            "average_frame_rate": frame_rate(video.get("avg_frame_rate")),
            "nominal_frame_rate": frame_rate(video.get("r_frame_rate")),
            "time_base": video.get("time_base"), "start_pts": video.get("start_pts"),
            "start_seconds": finite_number(video.get("start_time")),
            "duration_seconds": positive_number(video.get("duration")),
            "pixel_format": video.get("pix_fmt"),
            "sample_aspect_ratio": video.get("sample_aspect_ratio"),
            "display_aspect_ratio": video.get("display_aspect_ratio"),
            "color_primaries": video.get("color_primaries"),
            "color_space": video.get("color_space"),
            "color_transfer": video.get("color_transfer"),
        },
        "audio_streams": [
            {"stream_index": s.get("index"), "codec": s.get("codec_name"),
             "channels": s.get("channels"), "sample_rate": s.get("sample_rate"),
             "start_seconds": finite_number(s.get("start_time")), "time_base": s.get("time_base")}
            for s in streams if s.get("codec_type") == "audio"
        ],
        # This is the embedded claim, not a verified camera capture time.
        "creation_time_tag": tags.get("creation_time") or video_tags.get("creation_time"),
        "creation_time_verified": False,
        "integrity": "metadata_probed_only",
    }


class FFprobe:
    def __init__(self, executable: str | None = None, timeout: float = 120):
        self.executable = find_tool("ffprobe", executable)
        self.timeout = timeout
        self.version = run_tool([self.executable, "-version"], 10).stdout.splitlines()[0]

    def inspect(self, path: Path) -> tuple[dict, dict]:
        result = run_tool([
            self.executable, "-v", "error", "-protocol_whitelist", "file",
            "-show_format", "-show_streams", "-of", "json", str(path),
        ], self.timeout)
        try:
            raw = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise MediaLabError("invalid_metadata", "FFprobe returned invalid JSON.") from exc
        return normalize(raw), raw
