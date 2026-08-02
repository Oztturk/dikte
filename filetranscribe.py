"""Transcribe an existing audio/video file with the same models.

ffmpeg converts whatever comes in to 16 kHz mono WAV; long files are cut into
chunks that stay under the API's size limit, then stitched back together with
their timestamps shifted into place.
"""

import contextlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import wave

from PyQt6.QtCore import QObject, pyqtSignal

import api
import cleanup
import ggml
from i18n import t

CHUNK_SECONDS = 600          # 10 min ≈ 19 MB at 16 kHz mono s16
CLEANUP_CHUNK_CHARS = 12000  # keep each cleanup call comfortably small
RATE = 16000
MIN_SUBTITLE_SECONDS = 1.5   # how long a cue with no end time of its own stays up

# The [mm:ss] or [h:mm:ss] prefix a timestamped line starts with.
STAMP_RE = re.compile(r"^\[(?:(\d+):)?(\d{1,2}):(\d{2})\]\s*")


# What a stopped run comes back with, wherever it was stopped: the request that
# was cut off raises it from api, and the steps in between raise it themselves.
Cancelled = api.Aborted


class FileTranscriber(QObject):
    progress = pyqtSignal(str)
    finished = pyqtSignal(str, list)   # text, [(start, end, text)] when timestamped
    failed = pyqtSignal(str)

    def __init__(self, conf, parent=None):
        super().__init__(parent)
        self.conf = conf
        self._thread = None
        self._abort = api.Aborter()
        # The server on this machine the work is with, when it is with one.
        self._local = None

    @property
    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, path, timestamps, do_cleanup):
        if self.busy:
            return
        self._abort = api.Aborter()      # the last one is spent
        self._thread = threading.Thread(
            target=self._work, args=(path, timestamps, do_cleanup), daemon=True
        )
        self._thread.start()

    def stop(self):
        """Cut the run off where it stands, rather than at the next step."""
        self._abort.abort()
        # Closing the socket is nothing to a server on this machine: it is a
        # process of ours, and it would grind on to the end of the chunk with
        # nobody left to hand the answer to. Stopping it is what stops the
        # work; the next run starts it again. Killing waits on the process, so
        # not on the thread the window is drawn from.
        local = self._local
        if local is not None:
            threading.Thread(target=local.stop, daemon=True).start()

    def _check(self):
        self._abort.check()

    def _work(self, path, timestamps, do_cleanup):
        conf = self.conf
        workdir = None
        try:
            if not shutil.which("ffmpeg"):
                raise api.ApiError(t("ffmpeg not found. Install it to transcribe files."))

            workdir = tempfile.mkdtemp(prefix="dikte-file-")
            self.progress.emit(t("Converting audio…"))
            wav_path = _to_wav(path, workdir, self._abort)
            self._check()

            chunks = split_wav(wav_path, workdir)
            if len(chunks) > 1:
                self.progress.emit(t("Splitting into {count} chunks…", count=len(chunks)))

            target = conf.transcribe_target()
            self._local = ggml.whisper if target.provider == "local" else None
            pieces = []
            segments = []
            for index, (chunk_path, offset) in enumerate(chunks, start=1):
                self._check()
                self.progress.emit(
                    t("Transcribing chunk {index}/{count}…", index=index, count=len(chunks))
                )
                if timestamps:
                    segments.extend(
                        (start + offset, end + offset, line)
                        for start, end, line in api.transcribe_segments(
                            target,
                            chunk_path,
                            language=conf["language"],
                            prompt=conf["transcribe_prompt"],
                            aborter=self._abort,
                        )
                    )
                    pieces = [f"[{format_timestamp(start)}] {line}"
                              for start, _, line in segments]
                else:
                    pieces.append(api.transcribe(
                        target,
                        chunk_path,
                        language=conf["language"],
                        prompt=conf["transcribe_prompt"],
                        aborter=self._abort,
                    ))

            text = "\n".join(pieces) if timestamps else " ".join(pieces)

            if do_cleanup and text:
                self._check()
                self.progress.emit(t("Cleaning up…"))
                text = self._cleanup(text, timestamps)

            self.finished.emit(text, segments)

        except Cancelled:
            self.progress.emit(t("Stopped."))
        except (api.ApiError, OSError, subprocess.SubprocessError, wave.Error) as exc:
            self.failed.emit(str(exc))
        finally:
            self._local = None
            if workdir:
                shutil.rmtree(workdir, ignore_errors=True)

    def _cleanup(self, text, timestamps):
        conf = self.conf
        self._local = ggml.llm if cleanup.provider(conf) == "local" else None
        prompt = conf.cleanup_prompt(with_timestamps=timestamps, subtitles=True)
        out = []
        for block in split_text(text, timestamps):
            self._check()
            out.append(cleanup.run(block, conf, prompt, aborter=self._abort))
        return ("\n" if timestamps else "\n\n").join(out)


def format_timestamp(seconds):
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def srt_timestamp(seconds):
    millis = int(round(max(seconds, 0.0) * 1000))
    hours, rest = divmod(millis, 3600000)
    minutes, rest = divmod(rest, 60000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def to_srt(text, segments):
    """Turn the timestamped transcript into SRT cues.

    The text is the authority on wording, so cleanup edits survive; the segments
    are the authority on timing. They meet at the [mm:ss] prefix, which cleanup
    is told to leave alone: a line's whole-second stamp finds the segment it came
    from, and with it the fractional start and the end time whisper reported. A
    line whose stamp finds nothing runs until the next line starts.
    """
    cues = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = STAMP_RE.match(line)
        body = line[match.end():].strip() if match else line
        if not match:
            if cues and body:      # a wrapped line belongs to the cue above it
                cues[-1][2] += " " + body
            continue
        if not body:
            continue
        hours, minutes, secs = (int(g or 0) for g in match.groups())
        cues.append([hours * 3600 + minutes * 60 + secs, None, body])

    timing = {}
    for start, end, _ in segments:
        timing.setdefault(int(start), (start, end))
    for cue in cues:
        cue[0], cue[1] = timing.get(cue[0], (float(cue[0]), 0.0))
    for index, cue in enumerate(cues):
        following = cues[index + 1][0] if index + 1 < len(cues) else 0.0
        if following > cue[0]:
            cue[1] = min(cue[1], following) if cue[1] > cue[0] else following
        elif cue[1] <= cue[0]:
            cue[1] = cue[0] + MIN_SUBTITLE_SECONDS

    blocks = [
        f"{number}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{body}"
        for number, (start, end, body) in enumerate(cues, start=1)
    ]
    return "\n\n".join(blocks) + "\n" if blocks else ""


def _reap(proc):
    """Leave nothing running behind a conversion that did not finish."""
    if proc.poll() is None:
        proc.kill()
        proc.wait()


def _to_wav(path, workdir, aborter=None):
    out = os.path.join(workdir, "audio.wav")
    proc = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-y", "-i", path, "-vn",
         "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", out],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    # A two hour film is a minute of ffmpeg, which is a minute of a Stop button
    # doing nothing unless the abort reaches the process itself.
    with contextlib.ExitStack() as stack:
        stack.callback(_reap, proc)
        if aborter is not None:
            stack.enter_context(aborter.holding(proc.kill))
        _stdout, stderr = proc.communicate()
    if aborter is not None:
        aborter.check()
    if proc.returncode != 0 or not os.path.exists(out):
        tail = (stderr or "").strip().splitlines()
        raise api.ApiError(t("Could not read the file: {error}",
                             error=tail[-1] if tail else proc.returncode))
    return out


def split_wav(wav_path, workdir):
    """[(chunk path, offset in seconds)], a single entry for short files."""
    with contextlib.closing(wave.open(wav_path, "rb")) as src:
        rate = src.getframerate()
        total = src.getnframes()
        per_chunk = CHUNK_SECONDS * rate
        if total <= per_chunk:
            return [(wav_path, 0.0)]

        chunks = []
        index = 0
        while True:
            frames = src.readframes(per_chunk)
            if not frames:
                break
            path = os.path.join(workdir, f"chunk-{index:03d}.wav")
            with contextlib.closing(wave.open(path, "wb")) as dst:
                dst.setnchannels(src.getnchannels())
                dst.setsampwidth(src.getsampwidth())
                dst.setframerate(rate)
                dst.writeframes(frames)
            chunks.append((path, index * CHUNK_SECONDS))
            index += 1
        return chunks


def split_text(text, timestamps):
    """Break long text into cleanup-sized blocks, never mid-line."""
    if len(text) <= CLEANUP_CHUNK_CHARS:
        return [text]
    separator = "\n" if timestamps else " "
    blocks, current = [], ""
    for part in text.split(separator):
        candidate = f"{current}{separator}{part}" if current else part
        if len(candidate) > CLEANUP_CHUNK_CHARS and current:
            blocks.append(current)
            current = part
        else:
            current = candidate
    if current:
        blocks.append(current)
    return blocks
