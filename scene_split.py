#!/usr/bin/env python3
"""Split an H.264 video into ~N-second pieces, cutting at scene changes.

Each output piece runs until the first scene cut at or after the target
length (default 60s). By default pieces are re-encoded for frame-accurate
cuts exactly on the scene boundaries, matching the source resolution, frame
rate, and bitrate (audio is stream-copied). Use --copy for lossless stream
copy instead, which snaps cuts to keyframes.

Requires: Python 3.8+, scenedetect, and ffmpeg/ffprobe on PATH.
Works on macOS, Windows, and Linux.
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from scenedetect import ContentDetector, detect


def find_tool(name):
    path = shutil.which(name)
    if not path:
        sys.exit(
            f"error: '{name}' not found on PATH. "
            "Install ffmpeg (macOS: 'brew install ffmpeg')."
        )
    return path


def probe_video(ffprobe, video_path):
    """Return (duration_seconds, video_bitrate_bps_or_None) for the input."""
    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration:stream=bit_rate,codec_type",
            "-of", "json", str(video_path),
        ],
        capture_output=True, text=True, check=True,
    )
    info = json.loads(result.stdout)
    duration = float(info["format"]["duration"])
    bitrate = None
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "video" and stream.get("bit_rate"):
            bitrate = int(stream["bit_rate"])
            break
    return duration, bitrate


def keyframe_times(ffprobe, video_path):
    """Return sorted presentation times of all video keyframes."""
    result = subprocess.run(
        [
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0",
            str(video_path),
        ],
        capture_output=True, text=True, check=True,
    )
    times = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 2 and "K" in parts[1] and parts[0] not in ("", "N/A"):
            times.append(float(parts[0]))
    return sorted(times)


def snap_to_keyframes(boundaries, keyframes, duration):
    """Map each boundary to the first keyframe at or after it (deduped)."""
    snapped = []
    for b in boundaries:
        k = next((k for k in keyframes if k >= b - 1e-3), None)
        if k is not None and k < duration - 0.01 and (
            not snapped or k > snapped[-1] + 0.01
        ):
            snapped.append(k)
    return snapped


def plan_segments(boundaries, duration, target_len):
    """Build (start, end) pairs in seconds.

    Each segment ends at the first boundary at or after
    segment_start + target_len. If no boundary remains, the segment runs to
    the end of the video.
    """
    segments = []
    start = 0.0
    while start < duration - 0.01:
        end = next(
            (b for b in boundaries if b >= start + target_len and b < duration - 0.01),
            duration,
        )
        segments.append((start, end))
        start = end
    return segments


def split_stream_copy(ffmpeg, video_path, out_dir, segments, width):
    """Split losslessly in one pass with the segment muxer.

    Each cut lands on the first keyframe at or after the requested time, so
    pieces never overlap and concatenate back to the exact source.
    """
    pattern = out_dir / f"{video_path.stem}_%0{width}d{video_path.suffix}"
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video_path), "-map", "0", "-c", "copy",
        "-f", "segment", "-reset_timestamps", "1",
        "-segment_start_number", "1",
    ]
    # 1ms early so float rounding can't push a cut past its keyframe
    cut_times = [f"{max(0.0, end - 0.001):.6f}" for _, end in segments[:-1]]
    if cut_times:
        cmd += ["-segment_times", ",".join(cut_times)]
    cmd.append(str(pattern))
    subprocess.run(cmd, check=True)


def split_reencode(ffmpeg, video_path, out_path, start, end, bitrate):
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.6f}", "-i", str(video_path),
        "-t", f"{end - start:.6f}", "-map", "0",
        "-c:v", "libx264", "-preset", "medium", "-c:a", "copy",
    ]
    if bitrate:
        cmd += ["-b:v", str(bitrate)]
    cmd.append(str(out_path))
    subprocess.run(cmd, check=True)


def main():
    parser = argparse.ArgumentParser(
        description="Split a video into ~N-second pieces at scene cuts, "
        "preserving the source bitrate/resolution/frame rate."
    )
    parser.add_argument("video", type=Path, help="input video file (H.264)")
    parser.add_argument(
        "-l", "--length", type=float, default=60.0, metavar="SECONDS",
        help="target piece length; each piece ends at the first scene cut "
        "at or after this many seconds (default: 60)",
    )
    parser.add_argument(
        "-o", "--output-dir", type=Path, default=None,
        help="output directory (default: <video name>_pieces next to input)",
    )
    parser.add_argument(
        "-t", "--threshold", type=float, default=27.0,
        help="scene detection sensitivity; lower finds more cuts (default: 27)",
    )
    parser.add_argument(
        "--copy", action="store_true",
        help="use lossless stream copy instead of re-encoding (faster, "
        "bit-identical to source, but cuts snap to keyframes instead of "
        "exact scene-cut frames)",
    )
    args = parser.parse_args()

    if not args.video.is_file():
        sys.exit(f"error: input file not found: {args.video}")

    ffmpeg = find_tool("ffmpeg")
    ffprobe = find_tool("ffprobe")

    out_dir = args.output_dir or args.video.parent / f"{args.video.stem}_pieces"
    out_dir.mkdir(parents=True, exist_ok=True)

    duration, bitrate = probe_video(ffprobe, args.video)

    print(f"Detecting scenes in {args.video.name} ...")
    scene_list = detect(
        str(args.video), ContentDetector(threshold=args.threshold),
        show_progress=True,
    )
    print(f"Found {len(scene_list)} scenes in {duration:.1f}s of video.")

    boundaries = sorted(scene[1].seconds for scene in scene_list)
    if args.copy:
        # Lossless cuts can only land on keyframes; snap each scene cut to
        # the first keyframe at or after it so the plan matches the output.
        boundaries = snap_to_keyframes(
            boundaries, keyframe_times(ffprobe, args.video), duration
        )

    segments = plan_segments(boundaries, duration, args.length)
    print(f"Splitting into {len(segments)} pieces "
          f"({'lossless stream copy' if args.copy else 'frame-accurate re-encode'}):")

    width = max(3, len(str(len(segments))))
    for i, (start, end) in enumerate(segments, 1):
        print(f"  {args.video.stem}_{i:0{width}d}{args.video.suffix}  "
              f"[{start:8.2f}s - {end:8.2f}s]  ({end - start:.1f}s)")

    if args.copy:
        split_stream_copy(ffmpeg, args.video, out_dir, segments, width)
    else:
        for i, (start, end) in enumerate(segments, 1):
            out_path = (out_dir /
                        f"{args.video.stem}_{i:0{width}d}{args.video.suffix}")
            split_reencode(ffmpeg, args.video, out_path, start, end, bitrate)

    print(f"Done. {len(segments)} pieces written to {out_dir}")


if __name__ == "__main__":
    main()
