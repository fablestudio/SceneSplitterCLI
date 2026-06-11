# SceneSplitter

A cross-platform command-line tool that splits an H.264 video into
approximately fixed-length pieces (default ~1 minute), cutting at scene
changes detected by [PySceneDetect](https://www.scenedetect.com/). Each piece
ends at the **first scene cut at or after** the target length, so pieces are
at least the target length and never cut mid-scene.

Cuts are frame-accurate, landing exactly on the detected scene boundaries.
Output pieces match the source resolution, frame rate, and bitrate; audio is
stream-copied untouched.

Pure Python + ffmpeg: runs unchanged on macOS, Windows, and Linux.

## Requirements

- Python 3.8+
- [ffmpeg](https://ffmpeg.org/) and ffprobe on PATH
- Python packages: `scenedetect`, `opencv-python` (see
  [requirements.txt](requirements.txt))

## Installation

### macOS

```sh
brew install python ffmpeg
git clone <this-repo>
cd SceneSplitterMac
pip3 install -r requirements.txt
```

### Windows

Install Python 3 from [python.org](https://www.python.org/downloads/) and
ffmpeg from [gyan.dev builds](https://www.gyan.dev/ffmpeg/builds/) (add it to
PATH), then:

```sh
git clone <this-repo>
cd SceneSplitterMac
pip install -r requirements.txt
```

## Usage

```sh
python3 scene_split.py input.mp4
```

Pieces are written to `input_pieces/` next to the source file, named
`input_001.mp4`, `input_002.mp4`, ...

### Options

| Option | Default | Description |
|---|---|---|
| `-l`, `--length SECONDS` | `60` | Target piece length. Each piece ends at the first scene cut at or after this. |
| `-o`, `--output-dir DIR` | `<name>_pieces/` | Where to write the pieces. |
| `-t`, `--threshold N` | `27` | Scene detection sensitivity; lower detects more cuts. |
| `--copy` | off | Lossless stream copy instead of re-encoding (faster, bit-identical, but cuts snap to keyframes). |

### Examples

```sh
# ~2 minute pieces
python3 scene_split.py movie.mp4 --length 120

# more sensitive scene detection, custom output folder
python3 scene_split.py movie.mp4 -t 20 -o ./clips

# fast lossless split (cuts on keyframes rather than exact frames)
python3 scene_split.py movie.mp4 --copy
```

## How it works

1. **Probe** — ffprobe reads the source duration and video bitrate.
2. **Detect** — PySceneDetect's `ContentDetector` finds every scene change.
3. **Plan** — starting from 0, each piece ends at the first scene boundary at
   or after `start + length`. If no boundary remains, the piece runs to the
   end of the video (so the final piece may be shorter than the target).
4. **Split** — each piece is encoded with libx264 at the source bitrate,
   keeping the source resolution and frame rate; audio is stream-copied.

## Notes

- **Default mode re-encodes the video.** That is what makes frame-accurate
  cuts possible: H.264 frames depend on earlier frames, so a copied stream
  can only begin at a keyframe. Re-encoding decodes and re-compresses each
  piece so it can start on the exact scene-cut frame.
- **`--copy` mode** is lossless and much faster, but each cut is snapped to
  the first keyframe at or after the scene cut (the printed plan shows the
  exact snapped times). Scene changes usually coincide with keyframes, so the
  drift is typically zero or small.
