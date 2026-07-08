#!/usr/bin/env python3
"""Title/summary generation for video segments via the Fable (ComfyDeploy) API.

Flow per segment: upload the file to get a URL, queue a deployment run with
that URL as ``input_video``, poll until the run finishes, then pull the title
and summary out of the run's outputs. Segments are processed concurrently and,
when driven by ``run_split_with_summaries``, overlap the ffmpeg extraction so
uploads start as soon as each piece is written.

Configuration comes from the environment (overridable by the caller):
  FABLE_API_KEY        - bearer token (required)
  FABLE_API_URL        - base URL (default https://api.fablecd.com)
  FABLE_DEPLOYMENT_ID  - deployment to run (default the summariser deployment)
"""

import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

DEFAULT_API_URL = "https://api.fablecd.com"
DEFAULT_DEPLOYMENT_ID = "d4772b05-f548-4327-b4f9-67acd9a780e6"

# Run statuses that mean "no longer in progress".
_TERMINAL = {"success", "failed", "error", "cancelled", "canceled", "timeout"}
_SUCCESS = {"success"}


class SummaryError(RuntimeError):
    """Raised when a segment could not be summarised."""


class FableClient:
    def __init__(self, api_key, api_url=None, deployment_id=None,
                 poll_interval=10.0, run_timeout=1800.0):
        if not api_key:
            raise ValueError("api_key is required")
        self.base = (api_url or DEFAULT_API_URL).rstrip("/")
        self.deployment_id = deployment_id or DEFAULT_DEPLOYMENT_ID
        self.poll_interval = poll_interval
        self.run_timeout = run_timeout
        self._auth = {"Authorization": f"Bearer {api_key}"}

    @classmethod
    def from_env(cls, **kwargs):
        return cls(
            api_key=os.environ.get("FABLE_API_KEY", ""),
            api_url=os.environ.get("FABLE_API_URL"),
            deployment_id=os.environ.get("FABLE_DEPLOYMENT_ID"),
            **kwargs,
        )

    def upload(self, path):
        """Upload a local file, returning its public URL."""
        with open(path, "rb") as fh:
            r = requests.post(
                f"{self.base}/api/file/upload",
                headers=self._auth,
                files={"file": (path.name, fh, "video/mp4")},
                timeout=900,
            )
        r.raise_for_status()
        return r.json()["file_url"]

    def queue(self, video_url, seed=-1):
        """Queue a deployment run; returns the run id."""
        r = requests.post(
            f"{self.base}/api/run/deployment/queue",
            headers={**self._auth, "Content-Type": "application/json"},
            json={
                "deployment_id": self.deployment_id,
                "inputs": {"input_seed": seed, "input_video": video_url},
            },
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["run_id"]

    def poll(self, run_id):
        """Block until the run reaches a terminal status; return the run JSON."""
        deadline = time.monotonic() + self.run_timeout
        while True:
            r = requests.get(f"{self.base}/api/run/{run_id}",
                             headers=self._auth, timeout=60)
            r.raise_for_status()
            data = r.json()
            if data.get("status") in _TERMINAL:
                return data
            if time.monotonic() > deadline:
                raise SummaryError(f"run {run_id} timed out after "
                                   f"{self.run_timeout:.0f}s")
            time.sleep(self.poll_interval)

    def summarize(self, path):
        """Upload, run, and return {"title": ..., "summary": ...} for a file."""
        url = self.upload(path)
        run_id = self.queue(url)
        run = self.poll(run_id)
        if run.get("status") not in _SUCCESS:
            raise SummaryError(f"run {run_id} ended with status "
                               f"{run.get('status')!r}")
        return extract_title_summary(run)


def _first_str(values):
    for v in values:
        if isinstance(v, str) and v.strip():
            return v
    return None


def _maybe_json(text):
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def extract_title_summary(run):
    """Pull title/summary out of a completed run's outputs.

    Handles both a single output whose text is a ``{"title","summary"}`` JSON
    blob and separate outputs keyed by name. Returns a dict with whatever was
    found (missing fields are empty strings).
    """
    title = summary = None
    for output in run.get("outputs", []):
        data = output.get("data") or {}
        for key, values in data.items():
            if not isinstance(values, list):
                continue
            text = _first_str(values)
            if text is None:
                continue
            blob = _maybe_json(text)
            if blob is not None:
                title = title or blob.get("title")
                summary = summary or blob.get("summary") or blob.get("synopsis")
                continue
            kl = key.lower()
            if "title" in kl and title is None:
                title = text
            elif ("summary" in kl or "synopsis" in kl or "description" in kl) \
                    and summary is None:
                summary = text
    return {"title": title or "", "summary": summary or ""}


def summary_json_path(segment_path):
    return segment_path.with_suffix(".json")


def write_summary_json(segment_path, result):
    out = summary_json_path(segment_path)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    return out


def write_summary_text(text_path, ordered_names, results_by_name):
    """Write the copy/paste block file: filename, title, synopsis per segment.

    Layout per segment (no headers): the filename, a blank line, the title, a
    blank line, the synopsis, then a double blank line before the next.
    """
    parts = []
    for name in ordered_names:
        r = results_by_name.get(name) or {}
        block = "\n".join([name, "", r.get("title", ""), "",
                           r.get("summary", "")])
        # Trailing "\n\n\n" ends the synopsis line and leaves two blank lines
        # before the next segment (and after the last one).
        parts.append(block + "\n\n\n")
    text_path.write_text("".join(parts), encoding="utf-8")
    return text_path


def run_split_with_summaries(cmd, out_dir, segment_names, client, concurrency,
                             stderr_path, on_event=None):
    """Run the ffmpeg split while summarising each piece as it is written.

    ``cmd`` is the ffmpeg command (segment muxer) that writes the files named
    in ``segment_names`` (in order) into ``out_dir``. Each piece is uploaded and
    summarised the moment ffmpeg moves on to the next one, overlapping the
    remaining extraction. Returns ``{name: result_or_error_dict}``.
    """
    def announce(msg):
        if on_event:
            on_event(msg)

    results = {}
    lock = threading.Lock()

    def worker(name):
        path = out_dir / name
        try:
            result = client.summarize(path)
            write_summary_json(path, result)
            announce(f"  summarised {name}: {result.get('title') or '(no title)'}")
        except Exception as exc:  # noqa: BLE001 - report, don't abort the batch
            result = {"title": "", "summary": "", "error": str(exc)}
            announce(f"  FAILED {name}: {exc}")
        with lock:
            results[name] = result

    with open(stderr_path, "wb") as errf:
        proc = subprocess.Popen(cmd, stderr=errf, stdout=subprocess.DEVNULL)
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            submitted = set()

            def submit_ready(final):
                for i, name in enumerate(segment_names):
                    if name in submitted:
                        continue
                    # A piece is finished once the next piece file exists, or
                    # (for the last piece) once ffmpeg itself has exited.
                    nxt = segment_names[i + 1] if i + 1 < len(segment_names) else None
                    done = (nxt is not None and (out_dir / nxt).exists()) or \
                           (final and (out_dir / name).exists())
                    if done:
                        submitted.add(name)
                        announce(f"  extracted {name}; summarising ...")
                        pool.submit(worker, name)

            while proc.poll() is None:
                submit_ready(final=False)
                time.sleep(0.5)
            rc = proc.wait()
            submit_ready(final=True)

    if rc != 0:
        detail = ""
        try:
            detail = stderr_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        raise SummaryError(f"ffmpeg exited with code {rc}\n{detail}")
    try:
        stderr_path.unlink()  # nothing went wrong; don't leave the log behind
    except OSError:
        pass
    return results
