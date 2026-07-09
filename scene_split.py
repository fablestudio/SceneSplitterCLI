#!/usr/bin/env python3
"""Split an H.264 video into ~N-second pieces, cutting at scene changes.

Each output piece runs until the first scene cut at or after the target
length (default 60s). By default pieces are re-encoded for frame-accurate
cuts exactly on the scene boundaries, to H.264 MP4 at 8 Mbps, keeping the
source resolution and frame rate (audio is stream-copied). Use --match-source
to re-encode at the source's own bitrate instead, or --copy for lossless
stream copy (which snaps cuts to keyframes and keeps the source container).

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

# Audio codecs MP4 can hold; anything else is re-encoded to AAC for MP4 output.
MP4_AUDIO_CODECS = {"aac", "mp3", "ac3", "eac3", "alac", "opus"}


def timecode_seconds(tc):
    """Return seconds for a FrameTimecode across scenedetect versions.

    Newer releases (0.7+) expose a ``.seconds`` property; older ones only
    have ``.get_seconds()`` (which newer releases deprecate). Prefer the
    property when present so we don't trigger the deprecation warning.
    """
    return tc.seconds if hasattr(tc, "seconds") else tc.get_seconds()


def find_tool(name):
    path = shutil.which(name)
    if not path:
        sys.exit(
            f"error: '{name}' not found on PATH. "
            "Install ffmpeg (macOS: 'brew install ffmpeg')."
        )
    return path


def probe_video(ffprobe, video_path):
    """Return (duration_s, video_bitrate_bps_or_None, audio_codec_or_None)."""
    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries",
            "format=duration:stream=bit_rate,codec_type,codec_name",
            "-of", "json", str(video_path),
        ],
        capture_output=True, text=True, check=True,
    )
    info = json.loads(result.stdout)
    duration = float(info["format"]["duration"])
    bitrate = None
    audio_codec = None
    for stream in info.get("streams", []):
        ct = stream.get("codec_type")
        if ct == "video" and bitrate is None and stream.get("bit_rate"):
            bitrate = int(stream["bit_rate"])
        elif ct == "audio" and audio_codec is None:
            audio_codec = stream.get("codec_name")
    return duration, bitrate, audio_codec


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


def detect_scene_boundaries(video, threshold, duration, target_len, allow_prompt):
    """Detect scene-cut times, retrying at a lower threshold if asked.

    If any scene is longer than 1.5x the target length - meaning a piece would
    be forced well past the requested length because no cut was found inside
    it, usually because the threshold is too high - offer to re-run the whole
    detection with the threshold reduced by 5, down to 0. Returns sorted
    scene-end times (seconds).
    """
    max_scene = target_len * 1.5
    while True:
        print(f"Detecting scenes in {video.name} (threshold {threshold:g}) ...")
        scene_list = detect(
            str(video), ContentDetector(threshold=threshold),
            show_progress=True,
        )
        boundaries = sorted(timecode_seconds(s[1]) for s in scene_list)
        print(f"Found {len(scene_list)} scenes in {duration:.1f}s of video.")

        # Longest gap between consecutive cuts (0 and end-of-video included).
        prev = longest = 0.0
        for b in boundaries + [duration]:
            longest = max(longest, b - prev)
            prev = b
        if longest <= max_scene or threshold <= 0:
            return boundaries

        new_threshold = max(0.0, threshold - 5)
        print(f"  Longest scene is {longest:.1f}s - over 1.5x the "
              f"{target_len:g}s target ({max_scene:.1f}s), so detection looks "
              f"too coarse at threshold {threshold:g}.")
        if not allow_prompt:
            print("  Non-interactive session; keeping this result.")
            return boundaries
        try:
            ans = input(f"  Retry from the start at threshold "
                        f"{new_threshold:g} (discards this pass)? [y/N] ")
        except EOFError:
            return boundaries
        if ans.strip().lower() not in ("y", "yes"):
            return boundaries
        threshold = new_threshold


def plan_segments(boundaries, duration, target_len):
    """Build (start, end) pairs in seconds.

    Each segment ends at the first boundary at or after
    segment_start + target_len. If no boundary remains, the segment runs to
    the end of the video. A final segment shorter than half the target is
    merged into the one before it rather than standing alone.
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
    if len(segments) >= 2 and segments[-1][1] - segments[-1][0] < target_len / 2:
        (start, _), (_, end) = segments[-2], segments[-1]
        segments[-2:] = [(start, end)]
    return segments


def segment_names(video_path, segments, width, suffix):
    """The output filenames the segment muxer will write, in order."""
    return [f"{video_path.stem}_{i:0{width}d}{suffix}"
            for i in range(1, len(segments) + 1)]


def stream_copy_cmd(ffmpeg, video_path, out_dir, segments, width):
    """ffmpeg command to split losslessly in one pass with the segment muxer.

    Each cut lands on the first keyframe at or after the requested time, so
    pieces never overlap and concatenate back to the exact source.
    """
    names = segment_names(video_path, segments, width, video_path.suffix)
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video_path), "-map", "0", "-c", "copy",
    ]
    if len(segments) <= 1:
        # No cuts: copy the whole thing to a single file (the segment muxer
        # would otherwise apply its default 2s split).
        cmd.append(str(out_dir / names[0]))
        return cmd
    cmd += ["-f", "segment", "-reset_timestamps", "1",
            "-segment_start_number", "1"]
    # 1ms early so float rounding can't push a cut past its keyframe
    cut_times = [f"{max(0.0, end - 0.001):.6f}" for _, end in segments[:-1]]
    cmd += ["-segment_times", ",".join(cut_times),
            str(out_dir / f"{video_path.stem}_%0{width}d{video_path.suffix}")]
    return cmd


def reencode_cmd(ffmpeg, video_path, out_dir, segments, width, bitrate,
                 audio_args):
    """ffmpeg command to re-encode and split in a single decode pass.

    A keyframe is forced exactly at each cut time and the segment muxer splits
    there, so cuts are frame-accurate. Because the input is decoded straight
    through (never seeked into mid-stream), this avoids the benign but noisy
    ``mmco: unref short failure`` decoder warnings that per-segment seeking
    produces, and only decodes the source once.
    """
    names = segment_names(video_path, segments, width, ".mp4")
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video_path), "-map", "0",
        "-c:v", "libx264", "-preset", "medium", *audio_args,
    ]
    if bitrate:
        cmd += ["-b:v", str(bitrate)]
    if len(segments) <= 1:
        # No cuts: a single piece is a straight re-encode to one file (the
        # segment muxer would otherwise apply its default 2s split).
        cmd.append(str(out_dir / names[0]))
        return cmd
    # Force a keyframe exactly at each cut (frame-accurate), but aim the split
    # 1ms earlier so float rounding can't leave the keyframe just below the
    # target time and make the muxer skip the cut.
    key_times = [f"{end:.6f}" for _, end in segments[:-1]]
    seg_times = [f"{max(0.0, end - 0.001):.6f}" for _, end in segments[:-1]]
    cmd += ["-force_key_frames", ",".join(key_times),
            "-f", "segment", "-reset_timestamps", "1",
            "-segment_start_number", "1",
            "-segment_times", ",".join(seg_times),
            str(out_dir / f"{video_path.stem}_%0{width}d.mp4")]
    return cmd


def main():
    parser = argparse.ArgumentParser(
        description="Split a video into ~N-second pieces at scene cuts, "
        "re-encoding to H.264 at 8 Mbps by default (or --match-source / "
        "--copy to keep the source bitrate)."
    )
    parser.add_argument("video", type=Path, help="input video file (H.264)")
    parser.add_argument(
        "-l", "--length", type=float, default=60.0, metavar="SECONDS",
        help="target piece length; each piece ends at the first scene cut "
        "at or after this many seconds (default: 60)",
    )
    parser.add_argument(
        "-o", "--output-dir", type=Path, default=None,
        help="output directory; existing files there are overwritten "
        "(default: <video name>_pieces next to input, auto-incremented to "
        "_pieces_2, _pieces_3, ... if it already exists)",
    )
    parser.add_argument(
        "-t", "--threshold", type=float, default=27.0,
        help="scene detection sensitivity; lower finds more cuts (default: 27)",
    )
    parser.add_argument(
        "-b", "--bitrate", default="8M", metavar="RATE",
        help="target video bitrate when re-encoding, in ffmpeg syntax "
        "(default: 8M; e.g. 5M, 8000k)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--match-source", action="store_true",
        help="re-encode at the source's own bitrate instead of --bitrate "
        "(frame-accurate, keeps source bitrate/resolution/frame rate)",
    )
    mode.add_argument(
        "--copy", action="store_true",
        help="use lossless stream copy instead of re-encoding (faster, "
        "bit-identical to source, but cuts snap to keyframes instead of "
        "exact scene-cut frames)",
    )
    parser.add_argument(
        "--no-summarize", dest="summarize", action="store_false",
        help="skip title/summary generation. By default each piece is sent to "
        "the Fable API (overlapped with extraction) to write <piece>.json plus "
        "a combined <video>_summary.txt; that needs FABLE_API_KEY in the "
        "environment (without it, summaries are skipped automatically).",
    )
    parser.add_argument(
        "--api-concurrency", type=int, default=4, metavar="N",
        help="how many pieces to summarize in parallel (default: 4)",
    )
    args = parser.parse_args()

    if not args.video.is_file():
        sys.exit(f"error: input file not found: {args.video}")

    ffmpeg = find_tool("ffmpeg")
    ffprobe = find_tool("ffprobe")

    # Set up the summariser up front (before the slow detection/extraction).
    # Summaries are on by default; if the key or dependency is missing, note
    # it and carry on with a plain split rather than failing.
    summary_client = None
    if args.summarize:
        try:
            import summarize
            summary_client = summarize.FableClient.from_env()
        except ImportError:
            print("note: 'requests' is not installed; skipping summaries "
                  "(pip install requests, or pass --no-summarize).")
            args.summarize = False
        except ValueError:
            print("note: FABLE_API_KEY is not set; skipping summaries "
                  "(set it to enable, or pass --no-summarize to silence).")
            args.summarize = False

    duration, bitrate, audio_codec = probe_video(ffprobe, args.video)

    boundaries = detect_scene_boundaries(
        args.video, args.threshold, duration, args.length,
        allow_prompt=sys.stdin.isatty(),
    )
    if args.copy:
        # Lossless cuts can only land on keyframes; snap each scene cut to
        # the first keyframe at or after it so the plan matches the output.
        boundaries = snap_to_keyframes(
            boundaries, keyframe_times(ffprobe, args.video), duration
        )

    segments = plan_segments(boundaries, duration, args.length)

    if args.copy:
        mode_desc = "lossless stream copy"
        target_bitrate = None
    elif args.match_source:
        if not bitrate:
            sys.exit(
                "error: could not read source bitrate for --match-source; "
                "use --bitrate RATE instead."
            )
        target_bitrate = bitrate
        mode_desc = (f"frame-accurate re-encode at source bitrate "
                     f"(~{bitrate / 1_000_000:.1f} Mbps)")
    else:
        target_bitrate = args.bitrate
        mode_desc = f"frame-accurate re-encode at {args.bitrate}"
    print(f"Splitting into {len(segments)} pieces ({mode_desc}):")

    # MP4 output can't hold every audio codec; copy when compatible, else AAC.
    if not args.copy:
        if audio_codec and audio_codec not in MP4_AUDIO_CODECS:
            audio_args = ["-c:a", "aac", "-b:a", "192k"]
            print(f"Note: '{audio_codec}' audio isn't MP4-compatible; "
                  "re-encoding audio to AAC.")
        else:
            audio_args = ["-c:a", "copy"]

    # Re-encoding always produces H.264 MP4; stream copy keeps the source
    # container so it stays bit-identical.
    out_suffix = args.video.suffix if args.copy else ".mp4"

    width = max(3, len(str(len(segments))))
    for i, (start, end) in enumerate(segments, 1):
        print(f"  {args.video.stem}_{i:0{width}d}{out_suffix}  "
              f"[{start:8.2f}s - {end:8.2f}s]  ({end - start:.1f}s)")

    # Create the output folder only now, once we're committed to writing, so
    # a detection pass the user discarded never leaves files behind.
    if args.output_dir:
        out_dir = args.output_dir
    else:
        # Never clobber a previous pass: bump to _pieces_2, _pieces_3, ...
        base = args.video.parent / f"{args.video.stem}_pieces"
        out_dir, n = base, 2
        while out_dir.exists():
            out_dir = base.with_name(f"{base.name}_{n}")
            n += 1
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.copy:
        cmd = stream_copy_cmd(ffmpeg, args.video, out_dir, segments, width)
    else:
        cmd = reencode_cmd(ffmpeg, args.video, out_dir, segments, width,
                           target_bitrate, audio_args)

    if not args.summarize:
        subprocess.run(cmd, check=True)
        print(f"Done. {len(segments)} pieces written to {out_dir}")
        return

    names = segment_names(args.video, segments, width, out_suffix)
    print(f"Extracting and summarizing (up to {args.api_concurrency} in "
          "parallel) ...")
    results = summarize.run_split_with_summaries(
        cmd, out_dir, names, summary_client,
        concurrency=args.api_concurrency,
        stderr_path=out_dir / ".ffmpeg-split.log",
        on_event=print,
    )
    text_path = out_dir / f"{args.video.stem}_summary.txt"
    summarize.write_summary_text(text_path, names, results)
    ok = sum(1 for r in results.values() if r.get("title") or r.get("summary"))
    failed = [n for n in names if results.get(n, {}).get("error")]
    print(f"Done. {len(segments)} pieces written to {out_dir}")
    print(f"Summaries: {ok}/{len(names)} succeeded"
          + (f", {len(failed)} failed" if failed else "")
          + f". Copy/paste file: {text_path}")


if __name__ == "__main__":
    main()
