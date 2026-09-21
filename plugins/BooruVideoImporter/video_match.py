"""FFmpeg-only frame extraction and perceptual verification for Booru Video Importer."""
from __future__ import annotations

import json
import math
import os
import statistics
import subprocess
from typing import Dict, Iterable, List, Tuple

DEFAULT_RATIOS = (0.10, 0.24, 0.38, 0.52, 0.66, 0.80, 0.90)


def ffprobe_from_ffmpeg(ffmpeg_path: str) -> str:
    directory, name = os.path.split(ffmpeg_path or "ffmpeg")
    lower = name.casefold()
    if "ffmpeg" in lower:
        probe = name[: lower.index("ffmpeg")] + "ffprobe" + name[lower.index("ffmpeg") + len("ffmpeg"):]
    else:
        probe = "ffprobe.exe" if name.lower().endswith(".exe") else "ffprobe"
    return os.path.join(directory, probe) if directory else probe


def probe_duration(source: str, ffmpeg_path: str = "ffmpeg", timeout: int = 30) -> float:
    cmd = [
        ffprobe_from_ffmpeg(ffmpeg_path),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        source,
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace")[:300] or "ffprobe failed")
    payload = json.loads(proc.stdout.decode("utf-8", errors="replace") or "{}")
    value = ((payload.get("format") or {}).get("duration"))
    duration = float(value or 0)
    if duration <= 0:
        raise RuntimeError("video duration is unavailable")
    return duration


def _timestamp(duration: float, ratio: float) -> float:
    return max(0.05, min(max(0.05, duration - 0.05), duration * float(ratio)))


def frame_timestamp_seconds(duration: float, ratio: float) -> float:
    """Return the exact clamped timestamp used for proportional frame sampling."""
    return _timestamp(duration, ratio)


def extract_jpeg_frame(
    source: str,
    duration: float,
    ratio: float,
    ffmpeg_path: str = "ffmpeg",
    timeout: int = 30,
) -> bytes:
    ts = _timestamp(duration, ratio)
    cmd = [
        ffmpeg_path, "-hide_banner", "-loglevel", "error",
        "-ss", f"{ts:.3f}", "-i", source,
        "-frames:v", "1",
        "-vf", "scale=640:640:force_original_aspect_ratio=decrease",
        "-q:v", "5",
        "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace")[:300] or "frame extraction failed")
    return bytes(proc.stdout)



def frame_ahash_at_seconds(
    source: str,
    seconds: float,
    ffmpeg_path: str = "ffmpeg",
    timeout: int = 30,
) -> int:
    """Return a 64-bit average hash from a frame near an absolute timestamp."""
    ts = max(0.05, float(seconds))
    vf = "scale=8:8:force_original_aspect_ratio=decrease,pad=8:8:(ow-iw)/2:(oh-ih)/2,format=gray"
    cmd = [
        ffmpeg_path, "-hide_banner", "-loglevel", "error",
        "-ss", f"{ts:.3f}", "-i", source,
        "-frames:v", "1", "-vf", vf,
        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    raw = bytes(proc.stdout or b"")
    if proc.returncode != 0 or len(raw) < 64:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace")[:300] or "absolute-frame extraction failed")
    pixels = raw[:64]
    mean = sum(pixels) / 64.0
    value = 0
    for pixel in pixels:
        value = (value << 1) | (1 if pixel >= mean else 0)
    return value


def image_ahash(
    source: str,
    ffmpeg_path: str = "ffmpeg",
    timeout: int = 30,
) -> int:
    """Return the same 64-bit average hash for a still image/preview URL."""
    vf = "scale=8:8:force_original_aspect_ratio=decrease,pad=8:8:(ow-iw)/2:(oh-ih)/2,format=gray"
    cmd = [
        ffmpeg_path, "-hide_banner", "-loglevel", "error",
        "-i", source,
        "-frames:v", "1", "-vf", vf,
        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    raw = bytes(proc.stdout or b"")
    if proc.returncode != 0 or len(raw) < 64:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace")[:300] or "preview hash extraction failed")
    pixels = raw[:64]
    mean = sum(pixels) / 64.0
    value = 0
    for pixel in pixels:
        value = (value << 1) | (1 if pixel >= mean else 0)
    return value


def early_frame_hashes(
    source: str,
    duration: float,
    ffmpeg_path: str = "ffmpeg",
    timeout: int = 30,
) -> List[int]:
    """Hash a first-useful frame and an early proportional frame.

    About one second avoids literal frame-zero black/fade frames. Ten percent adds
    enough temporal diversity to make the cheap source-first candidate filter useful.
    """
    duration = max(0.1, float(duration))
    first_seconds = min(1.0, max(0.05, duration * 0.02))
    second_ratio = 0.10
    return [
        frame_ahash_at_seconds(source, first_seconds, ffmpeg_path, timeout),
        frame_ahash(source, duration, second_ratio, ffmpeg_path, timeout),
    ]


def early_hash_candidate(
    local_hashes: Iterable[int],
    remote_hashes: Iterable[int],
    max_distance: int = 4,
) -> Dict[str, object]:
    """Require two aligned early-frame hashes before expensive verification.

    Source-first matching is intentionally conservative. A single visually similar
    frame is never enough to nominate a local scene.
    """
    local = [int(x) for x in local_hashes]
    remote = [int(x) for x in remote_hashes]
    if len(local) < 2 or len(remote) < 2:
        return {
            "candidate": False,
            "matched": 0,
            "min_distance": 64,
            "median_distance": 64.0,
            "distances": [],
        }

    distances = [
        hamming_distance(local[index], remote[index])
        for index in range(min(len(local), len(remote), 2))
    ]
    matched = sum(1 for distance in distances if distance <= max_distance)
    median = float(statistics.median(distances)) if distances else 64.0
    minimum = min(distances) if distances else 64

    return {
        "candidate": len(distances) == 2 and matched == 2,
        "matched": matched,
        "min_distance": minimum,
        "median_distance": round(median, 2),
        "distances": distances,
    }



def frame_ahash(
    source: str,
    duration: float,
    ratio: float,
    ffmpeg_path: str = "ffmpeg",
    timeout: int = 30,
) -> int:
    """Return a 64-bit average hash from one normalized video frame."""
    ts = _timestamp(duration, ratio)
    vf = "scale=8:8:force_original_aspect_ratio=decrease,pad=8:8:(ow-iw)/2:(oh-ih)/2,format=gray"
    cmd = [
        ffmpeg_path, "-hide_banner", "-loglevel", "error",
        "-ss", f"{ts:.3f}", "-i", source,
        "-frames:v", "1", "-vf", vf,
        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    raw = bytes(proc.stdout or b"")
    if proc.returncode != 0 or len(raw) < 64:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace")[:300] or "perceptual frame extraction failed")
    pixels = raw[:64]
    mean = sum(pixels) / 64.0
    value = 0
    for pixel in pixels:
        value = (value << 1) | (1 if pixel >= mean else 0)
    return value


def frame_dhash(
    source: str,
    duration: float,
    ratio: float,
    ffmpeg_path: str = "ffmpeg",
    timeout: int = 30,
) -> int:
    """Return a 256-bit horizontal difference hash from a normalized frame."""
    ts = _timestamp(duration, ratio)
    vf = "scale=17:16:force_original_aspect_ratio=decrease,pad=17:16:(ow-iw)/2:(oh-ih)/2,format=gray"
    cmd = [
        ffmpeg_path, "-hide_banner", "-loglevel", "error",
        "-ss", f"{ts:.3f}", "-i", source,
        "-frames:v", "1", "-vf", vf,
        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    raw = bytes(proc.stdout or b"")
    if proc.returncode != 0 or len(raw) < 272:
        raise RuntimeError(
            proc.stderr.decode("utf-8", errors="replace")[:300]
            or "strong perceptual frame extraction failed"
        )

    pixels = raw[:272]
    value = 0
    for row in range(16):
        offset = row * 17
        for col in range(16):
            left = pixels[offset + col]
            right = pixels[offset + col + 1]
            value = (value << 1) | (1 if left >= right else 0)
    return value



_PHASH_SIZE = 32
_PHASH_LOW = 8
_PHASH_BASIS = [
    [
        math.cos((math.pi / _PHASH_SIZE) * (x + 0.5) * u)
        for x in range(_PHASH_SIZE)
    ]
    for u in range(_PHASH_LOW)
]


def frame_phash(
    source: str,
    duration: float,
    ratio: float,
    ffmpeg_path: str = "ffmpeg",
    timeout: int = 45,
) -> int:
    """Return a standard 64-bit DCT perceptual hash for one video frame."""
    ts = _timestamp(duration, ratio)
    vf = (
        "scale=32:32:force_original_aspect_ratio=decrease,"
        "pad=32:32:(ow-iw)/2:(oh-ih)/2,format=gray"
    )
    cmd = [
        ffmpeg_path, "-hide_banner", "-loglevel", "error",
        "-ss", f"{ts:.3f}", "-i", source,
        "-frames:v", "1", "-vf", vf,
        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    raw = bytes(proc.stdout or b"")
    expected = _PHASH_SIZE * _PHASH_SIZE
    if proc.returncode != 0 or len(raw) < expected:
        raise RuntimeError(
            proc.stderr.decode("utf-8", errors="replace")[:300]
            or "perceptual hash frame extraction failed"
        )

    # Separable 2-D DCT, limited to the 8x8 low-frequency coefficients used
    # by pHash. Scaling constants are irrelevant because the final operation
    # compares coefficients against their median.
    pixels = [
        [float(raw[y * _PHASH_SIZE + x]) for x in range(_PHASH_SIZE)]
        for y in range(_PHASH_SIZE)
    ]
    row_coeffs = [
        [
            sum(
                pixels[y][x] * _PHASH_BASIS[u][x]
                for x in range(_PHASH_SIZE)
            )
            for u in range(_PHASH_LOW)
        ]
        for y in range(_PHASH_SIZE)
    ]
    coeffs = []
    for v in range(_PHASH_LOW):
        for u in range(_PHASH_LOW):
            coeffs.append(
                sum(
                    row_coeffs[y][u] * _PHASH_BASIS[v][y]
                    for y in range(_PHASH_SIZE)
                )
            )

    # Exclude the DC coefficient from the median while retaining its bit,
    # matching the common pHash construction.
    median = statistics.median(coeffs[1:]) if len(coeffs) > 1 else 0.0
    value = 0
    for coefficient in coeffs:
        value = (value << 1) | (1 if coefficient >= median else 0)
    return value


def frame_phashes(
    source: str,
    duration: float,
    ffmpeg_path: str = "ffmpeg",
    ratios: Iterable[float] = (0.10, 0.50, 0.90),
    timeout: int = 45,
) -> List[int]:
    return [
        frame_phash(source, duration, ratio, ffmpeg_path, timeout)
        for ratio in ratios
    ]


def phash_hex(value: int) -> str:
    return f"{int(value) & ((1 << 64) - 1):016x}"


def phash_from_hex(value: str) -> int:
    return int(str(value), 16)


def hamming_distance(a: int, b: int) -> int:
    return (int(a) ^ int(b)).bit_count()


def frame_hashes(
    source: str,
    duration: float,
    ffmpeg_path: str = "ffmpeg",
    ratios: Iterable[float] = DEFAULT_RATIOS,
    timeout: int = 30,
) -> List[int]:
    return [frame_dhash(source, duration, ratio, ffmpeg_path, timeout) for ratio in ratios]


def verify_video_candidate(
    local_source: str,
    remote_source: str,
    ffmpeg_path: str = "ffmpeg",
    ratios: Iterable[float] = DEFAULT_RATIOS,
    frame_distance: int = 16,
    timeout: int = 45,
    local_duration: float | None = None,
    local_hashes: List[int] | None = None,
    remote_duration: float | None = None,
    remote_hashes: List[int] | None = None,
    strict: bool = False,
) -> Dict[str, object]:
    """Verify videos using temporally aligned 256-bit frame hashes.

    Frames are compared only at the same relative positions. This intentionally
    favors false negatives over false positives; unrelated videos must not pass
    merely because they contain visually similar frames in different places.
    """
    ratios = tuple(ratios)
    local_duration = local_duration or probe_duration(local_source, ffmpeg_path, timeout)
    remote_duration = remote_duration or probe_duration(remote_source, ffmpeg_path, timeout)
    local_hashes = local_hashes or frame_hashes(
        local_source, local_duration, ffmpeg_path, ratios, timeout
    )
    remote_hashes = remote_hashes or frame_hashes(
        remote_source, remote_duration, ffmpeg_path, ratios, timeout
    )

    pair_count = min(len(local_hashes), len(remote_hashes), len(ratios))
    aligned = [
        hamming_distance(local_hashes[index], remote_hashes[index])
        for index in range(pair_count)
    ]

    matched = sum(1 for distance in aligned if distance <= frame_distance)
    median = float(statistics.median(aligned)) if aligned else 256.0
    mean = float(sum(aligned) / len(aligned)) if aligned else 256.0
    duration_delta = (
        abs(float(local_duration) - float(remote_duration))
        / max(float(local_duration), float(remote_duration))
        if local_duration and remote_duration
        else 1.0
    )

    if strict:
        # Source-first verification intentionally favors false negatives. A high
        # confidence result requires every sampled timestamp to agree closely and
        # the files to have nearly identical runtime.
        high = (
            pair_count >= 7
            and matched == pair_count
            and median <= 10.0
            and max(aligned, default=256) <= frame_distance
            and duration_delta <= 0.02
        )
        review = (
            not high
            and pair_count >= 7
            and matched >= pair_count - 1
            and median <= 16.0
            and duration_delta <= 0.05
        )
    else:
        required_high = max(6, pair_count - 1)
        high = (
            pair_count >= 6
            and matched >= required_high
            and median <= 18.0
            and duration_delta <= 0.08
        )
        review = (
            not high
            and pair_count >= 6
            and matched >= 5
            and median <= 26.0
            and duration_delta <= 0.15
        )

    return {
        "high": high,
        "review": review,
        "matched_frames": matched,
        "total_frames": pair_count,
        "median_distance": round(median, 2),
        "mean_distance": round(mean, 2),
        "aligned_distances": aligned,
        # Compatibility key retained for existing logging/callers.
        "nearest_distances": aligned,
        "duration_delta": round(duration_delta, 4),
        "local_duration": round(float(local_duration), 3),
        "remote_duration": round(float(remote_duration), 3),
    }

