# SceneSplitter

A cross-platform command-line tool that splits an H.264 video into
approximately fixed-length pieces (default ~1 minute), cutting at scene
changes detected by [PySceneDetect](https://www.scenedetect.com/). Each piece
ends at the **first scene cut at or after** the target length, so pieces are
at least the target length and never cut mid-scene.

Cuts are frame-accurate, landing exactly on the detected scene boundaries.
By default pieces are re-encoded to **H.264 MP4 at 8 Mbps** (regardless of the
input container), keeping the source resolution and frame rate. Audio is
stream-copied untouched, or transcoded to AAC only if the source audio isn't
MP4-compatible. Use `--match-source` to re-encode at the source's
own bitrate, or `--copy` for a fast lossless stream copy that keeps the
source container.

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

_*** If you get a Brew Not Found Error do the following:***_
#### Step 1: Add Homebrew to your PATH ####
If you already installed Homebrew, your shell just needs to be told where to find it. This is a very common issue on newer Macs with Apple Silicon (M1, M2, or M3 chips).Run the following commands in your Terminal one by one:Add the Homebrew path to your configuration:
```sh
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
```
Apply the changes to your current session:
```sh
eval "$(/opt/homebrew/bin/brew shellenv)"
```
Now, type brew help to see if it works. If it does, you are all set!

#### Step 2: Install Homebrew (If Step 1 didn't work) ####
If the command still fails, Homebrew is likely not installed on your system.
Open your Terminal and run the official installation command:
```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```
Follow the on-screen instructions and type your Mac user password when prompted.
Once the installation finishes, the installer will print out "Next steps." 
Copy and run those two specific echo commands provided by the installer in your Terminal to finalize your PATH.


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
`input_001.mp4`, `input_002.mp4`, ... Re-running on the same video never
overwrites a previous pass: if the folder already exists, the next run
writes to `input_pieces_2/`, then `input_pieces_3/`, and so on. (An explicit
`-o DIR` always writes to exactly that directory, overwriting its contents.)

### Options

| Option | Default | Description |
|---|---|---|
| `-l`, `--length SECONDS` | `60` | Target piece length. Each piece ends at the first scene cut at or after this. |
| `-o`, `--output-dir DIR` | `<name>_pieces/` (auto-incremented) | Where to write the pieces. Explicit `DIR` overwrites; the default never does. |
| `-t`, `--threshold N` | `27` | Scene detection sensitivity; lower detects more cuts. |
| `-b`, `--bitrate RATE` | `8M` | Target video bitrate when re-encoding (ffmpeg syntax, e.g. `5M`, `8000k`). |
| `--match-source` | off | Re-encode at the source's own bitrate instead of `--bitrate` (still frame-accurate). |
| `--copy` | off | Lossless stream copy instead of re-encoding (faster, bit-identical, but cuts snap to keyframes). |
| `--no-summarize` | off | Skip title/synopsis generation. It's **on by default** (needs `FABLE_API_KEY`; skipped automatically if unset). See [Summaries](#summaries-titles--synopses). |
| `--api-concurrency N` | `4` | How many pieces to summarize in parallel. |

`--match-source` and `--copy` are mutually exclusive.

### Examples

```sh
# ~2 minute pieces
python3 scene_split.py movie.mp4 --length 120

# more sensitive scene detection, custom output folder
python3 scene_split.py movie.mp4 -t 20 -o ./clips

# re-encode at a custom bitrate
python3 scene_split.py movie.mp4 --bitrate 5M

# keep the source's original bitrate (frame-accurate)
python3 scene_split.py movie.mp4 --match-source

# fast lossless split (cuts on keyframes rather than exact frames)
python3 scene_split.py movie.mp4 --copy

# split with summaries (the default) — just set the key
export FABLE_API_KEY="your-token"
python3 scene_split.py movie.mp4

# split only, no API calls
python3 scene_split.py movie.mp4 --no-summarize
```

## Summaries (titles & synopses)

By default each piece is sent to the Fable (ComfyDeploy) API to get a title
and synopsis (pass `--no-summarize` to skip, or leave `FABLE_API_KEY` unset and
it's skipped automatically). Uploads run **in parallel with the extraction** —
a piece is uploaded the moment ffmpeg finishes writing it, so summarization
overlaps the rest of the split.

Configuration is read from the environment:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `FABLE_API_KEY` | yes | — | Bearer token for the API. |
| `FABLE_API_URL` | no | `https://api.fablecd.com` | API base URL. |
| `FABLE_DEPLOYMENT_ID` | no | the summariser deployment | Deployment to run. |

Outputs, written into the pieces folder alongside the videos:

- **`<piece>.json`** for each piece, e.g. `movie_001.json`:
  ```json
  { "title": "…", "summary": "…" }
  ```
- **`<video>_summary.txt`** — one copy/paste-friendly block per piece with no
  headers: the filename, a blank line, the title, a blank line, the synopsis,
  then a double blank line before the next. Designed to paste straight into a
  web form.

Each piece is retried up to **3 times** on failure — a network error, a run
that times out or errors, or a run that comes back with no title/summary. If
all three attempts fail, that piece is recorded in its JSON with an `error`
field and left blank in the text file; the rest still complete.

## How it works

1. **Probe** — ffprobe reads the source duration and video bitrate.
2. **Detect** — PySceneDetect's `ContentDetector` finds every scene change.
   If any scene turns out longer than 1.5× the target length (i.e. a piece
   would be forced well past the requested `length` because no cut was found
   inside it, usually meaning the threshold is too high), you're offered the
   chance to re-run detection from scratch with the threshold lowered by 5,
   repeatedly down to 0. Nothing is written until you accept a result, so a
   discarded pass never leaves files behind.
3. **Plan** — starting from 0, each piece ends at the first scene boundary at
   or after `start + length`. If no boundary remains, the piece runs to the
   end of the video. If that leftover final piece would be shorter than half
   the target length, it is appended to the previous piece instead of
   standing alone — no piece is ever shorter than half the target.
4. **Split** — each piece is encoded with libx264 to an `.mp4` at the target
   bitrate (8 Mbps by default, or the source bitrate with `--match-source`),
   keeping the source resolution and frame rate; audio is stream-copied (or
   transcoded to AAC if the source audio isn't MP4-compatible).
   (`--copy` instead keeps the source container and is bit-identical.)

## Notes

- **Default mode re-encodes the video.** That is what makes frame-accurate
  cuts possible: H.264 frames depend on earlier frames, so a copied stream
  can only begin at a keyframe. Re-encoding forces a keyframe exactly at each
  cut, in a single decode pass, so every piece starts on the exact scene-cut
  frame. (Stream-copied audio can only split on its own frame boundaries, so a
  piece's audio may run a few milliseconds longer than its video — harmless.)
- **`--copy` mode** is lossless and much faster, but each cut is snapped to
  the first keyframe at or after the scene cut (the printed plan shows the
  exact snapped times). Scene changes usually coincide with keyframes, so the
  drift is typically zero or small.
