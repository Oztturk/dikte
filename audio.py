"""Raw PCM capture with a live level meter.

Dictation records one source. A meeting records two of them at once, the
microphone and what comes out of the speakers, and for that it goes through
ffmpeg. PulseAudio hands both devices to a single process, which merges them
into the two channels of one stream and keeps them aligned itself. AVFoundation
cannot be asked the same: two of its sessions inside one process starve each
other, so a Mac captures each device on its own and the two mono streams are
interleaved here as they arrive.

Which programs do the capturing is a property of the machine, not of the code
above: PulseAudio or PipeWire on Linux, AVFoundation through ffmpeg on macOS.
They are gathered into one group each near the bottom of this file, and a
chooser picks between them.
"""

import array
import collections
import json
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import wave

from PyQt6.QtCore import QObject, pyqtSignal

from i18n import t

RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # s16
CHUNK_FRAMES = 1024
CHUNK_BYTES = CHUNK_FRAMES * SAMPLE_WIDTH * CHANNELS
CHUNK_LATENCY_MS = round(CHUNK_FRAMES / RATE * 1000)
MIN_FRAMES = int(RATE * 0.25)

# A capture process hands over a block every CHUNK_LATENCY_MS. One that has said
# nothing for this long has stopped rather than fallen behind, and the meeting
# ends and says so instead of sitting on a read that will never return.
STALL_SECONDS = 5.0
# Room for a whole stall of the other stream, so the side still delivering is
# never the one left waiting.
QUEUE_BLOCKS = int(STALL_SECONDS * RATE / CHUNK_FRAMES) + 8

# Exact zeroes are not quiet, they are nothing: a microphone that is really in
# the room has a noise floor. This much of a recording that long means it handed
# nothing over, which is worth saying once the meeting is over and nothing can
# be done about it any more.
QUIET_MIC_SECONDS = 10
QUIET_MIC_SHARE = 0.5


class Recorder(QObject):
    """Runs the available sound-server recorder and reads raw PCM from stdout."""

    level = pyqtSignal(float)              # 0.0 - 1.0, for the waveform
    stopped = pyqtSignal(str, float, object)  # wav path, duration (s), per-chunk RMS
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc = None
        self._thread = None
        self._buffer = bytearray()
        self._rms = []
        self._cancelled = False
        self._stopping = False
        self._lock = threading.Lock()

    @property
    def active(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, target="", max_seconds=300):
        if self.active:
            return
        try:
            cmd = recording_command(target)
        except AudioDeviceError as exc:
            self.failed.emit(str(exc))
            return
        if not cmd:
            self.failed.emit(t(sound().missing))
            return

        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
            )
        except OSError as exc:
            self.failed.emit(t("Could not start recording: {error}", error=exc))
            return

        self._buffer = bytearray()
        self._rms = []
        self._cancelled = False
        self._stopping = False
        self._max_bytes = int(max_seconds * RATE * SAMPLE_WIDTH * CHANNELS)
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        proc = self._proc
        stdout = proc.stdout
        try:
            while True:
                chunk = stdout.read(CHUNK_BYTES)
                if not chunk:
                    break
                peak, rms = chunk_levels(chunk)
                with self._lock:
                    self._buffer.extend(chunk)
                    self._rms.append(rms)
                    too_long = len(self._buffer) >= self._max_bytes
                self.level.emit(peak)
                if too_long:
                    self._terminate()
                    break
        except (OSError, ValueError):
            pass
        # Nobody asked it to end and it captured nothing: the recorder is not
        # installed properly, or the device was refused. Said out loud here,
        # because stop() would otherwise report it as a recording that was too
        # short, which sends the user looking in the wrong place.
        with self._lock:
            captured = bool(self._buffer)
        if self._stopping or self._cancelled or captured:
            return
        try:
            detail = proc.stderr.read().decode("utf-8", "replace").strip()
        except (AttributeError, OSError):
            detail = ""
        self.failed.emit(t(
            "Audio recorder stopped before receiving sound: {error}",
            error=detail or f"exit code {proc.returncode}",
        ))

    def _terminate(self):
        self._stopping = True
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=1.5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass

    def cancel(self):
        self._cancelled = True
        self._terminate()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None
        self._proc = None
        with self._lock:
            self._buffer = bytearray()

    def stop(self):
        """End the recording and write the WAV file."""
        if not self._proc:
            return
        self._terminate()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None
        self._proc = None

        with self._lock:
            pcm = bytes(self._buffer)
            rms = list(self._rms)
            self._buffer = bytearray()

        if self._cancelled:
            return

        frames = len(pcm) // (SAMPLE_WIDTH * CHANNELS)
        if frames < MIN_FRAMES:  # a stray keypress, not speech
            self.failed.emit(t("Recording too short, speak for at least 0.3 s"))
            return

        path = write_wav(pcm)
        self.stopped.emit(path, frames / RATE, rms)


def write_wav(pcm, rate=RATE, channels=CHANNELS, width=SAMPLE_WIDTH):
    fd, path = tempfile.mkstemp(prefix="dikte-", suffix=".wav")
    with open(fd, "wb") as raw, wave.open(raw, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return path


def recording_command(target=""):
    """A raw-s16 capture command for the sound system on this machine."""
    return sound().record(target)


def meeting_commands(mic_target, system_target):
    """The capture processes that produce one stereo meeting stream.

    One of them on PulseAudio, which merges both inputs itself; one per device
    on a Mac, because two AVFoundation sessions in a process starve each other.
    Which of the two it is stays in the table with everything else the sound
    system decides, and MeetingRecorder reads the count rather than the machine.
    """
    return sound().meeting(mic_target, system_target)


class AudioDeviceError(RuntimeError):
    """A saved capture device can no longer be selected safely."""


class MeetingRecorder(QObject):
    """Microphone and speaker output into one stereo file: left is you, right is
    everyone else.

    Who said what then needs no guessing at all, because the two voices never
    shared a channel to begin with. The recording is written to disk as it
    arrives rather than held in memory, so length is not a problem and a crash
    costs the tail of the meeting instead of all of it.
    """

    levels = pyqtSignal(float, float)      # mine, theirs
    stopped = pyqtSignal(str, float)       # wav path, duration (s)
    died = pyqtSignal()                    # ffmpeg quit on its own
    warned = pyqtSignal(str)               # recorded, but something was wrong
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._procs = []
        self._thread = None
        self._wav = None
        self._logs = []
        self._path = ""
        self._frames = 0
        self._mic_zero_frames = 0
        self._split_inputs = False
        self._cancelled = False
        self._stopping = False
        self._lock = threading.Lock()

    @property
    def active(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, path, mic_target="", system_target="", max_seconds=14400):
        if self.active:
            return
        if not shutil.which("ffmpeg"):
            self.failed.emit(t("ffmpeg not found. Install it to record a meeting."))
            return
        if not system_target:
            system_target = default_monitor()
        if not system_target:
            self.failed.emit(t("Could not work out which speaker output to record. "
                               "Pick one in Settings → Meeting."))
            return

        try:
            commands = meeting_commands(mic_target, system_target)
        except AudioDeviceError as exc:
            self.failed.emit(str(exc))
            return

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._wav = wave.open(path, "wb")
            self._wav.setnchannels(2)
            self._wav.setsampwidth(SAMPLE_WIDTH)
            self._wav.setframerate(RATE)
            # ffmpeg keeps talking to stderr for as long as it runs; a pipe
            # nobody drains would eventually block it, so it writes to a file.
            self._logs = [tempfile.TemporaryFile() for _ in commands]
            self._procs = []
            for command, log in zip(commands, self._logs):
                self._procs.append(subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=log, bufsize=0
                ))
        except (OSError, wave.Error) as exc:
            # One of two capture processes may already be running, and a Mac
            # left holding an open AVFoundation session records nothing else.
            self._terminate_processes()
            self._procs = []
            self._close_file()
            self._drop_log()
            try:
                os.unlink(path)   # an empty header nobody will ever read
            except OSError:
                pass
            self.failed.emit(t("Could not start recording: {error}", error=exc))
            return

        self._path = path
        self._frames = 0
        self._mic_zero_frames = 0
        self._split_inputs = len(self._procs) == 2
        self._cancelled = False
        self._stopping = False
        self._max_frames = int(max_seconds * RATE)
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        if self._split_inputs:
            self._pump_split()
        else:
            self._pump_merged()
        # Nobody asked it to end: the sound device went away, or ffmpeg fell
        # over. An hour into a meeting that has to be said out loud rather than
        # discovered afterwards.
        if not self._stopping:
            self.died.emit()

    def _pump_merged(self):
        stdout = self._procs[0].stdout
        block = CHUNK_FRAMES * SAMPLE_WIDTH * 2
        try:
            while True:
                chunk = stdout.read(block)
                if not chunk:
                    break
                if not self._write_chunk(chunk):
                    break
        except (OSError, ValueError, wave.Error):
            pass

    def _pump_split(self):
        # A reader thread per process. Taking turns on the two pipes from one
        # thread would let a starved microphone hold up the far side too: its
        # blocks would sit unread until the pipe filled and its ffmpeg stopped
        # writing, and an hour of meeting would freeze with nothing said. Each
        # stream is read as fast as it arrives, and a side that goes quiet for
        # STALL_SECONDS ends the recording rather than hanging it.
        block = CHUNK_FRAMES * SAMPLE_WIDTH
        streams = [queue.Queue(maxsize=QUEUE_BLOCKS) for _ in self._procs]
        for proc, blocks in zip(self._procs, streams):
            threading.Thread(target=_read_blocks, daemon=True,
                             args=(proc.stdout, blocks, block)).start()
        try:
            while True:
                mine = _next_block(streams[0])
                theirs = _next_block(streams[1])
                if not mine or not theirs:
                    break
                frames = min(len(mine), len(theirs)) // SAMPLE_WIDTH
                mine = mine[:frames * SAMPLE_WIDTH]
                theirs = theirs[:frames * SAMPLE_WIDTH]
                self._mic_zero_frames += _zero_samples(mine)
                if not self._write_chunk(interleave_mono(mine, theirs)):
                    break
        except (OSError, ValueError, wave.Error):
            pass

    def _write_chunk(self, chunk):
        mine, theirs = stereo_levels(chunk)
        with self._lock:
            if self._wav is None:
                return False
            self._wav.writeframes(chunk)
            self._frames += len(chunk) // (SAMPLE_WIDTH * 2)
            too_long = self._frames >= self._max_frames
        self.levels.emit(mine, theirs)
        if too_long:
            self._terminate()
            return False
        return True

    def _terminate(self):
        self._stopping = True
        self._terminate_processes()

    def _terminate_processes(self):
        running = [proc for proc in self._procs if proc.poll() is None]
        for proc in running:
            try:
                proc.send_signal(signal.SIGINT)
            except OSError:
                pass
        for proc in running:
            try:
                proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass

    def _close_file(self):
        with self._lock:
            wav, self._wav = self._wav, None
        if wav is not None:
            try:
                wav.close()
            except (OSError, wave.Error):
                pass

    def _error_tail(self):
        tails = []
        for log in self._logs:
            try:
                log.seek(0)
                text = log.read().decode("utf-8", "replace").strip()
            except OSError:
                continue
            lines = [line for line in text.splitlines() if line.strip()]
            if lines:
                tails.append(lines[-1])
        return " | ".join(tails)

    def _finish_process(self):
        self._terminate()
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None
        codes = [proc.poll() for proc in self._procs]
        code = next((value for value in codes if value), 0)
        self._procs = []
        self._close_file()
        return code

    def cancel(self):
        self._cancelled = True
        self._finish_process()
        self._drop_log()
        try:
            os.unlink(self._path)
        except OSError:
            pass

    def stop(self):
        if not self._procs:
            return
        # The count is read after the join: the pump thread is still appending
        # the last blocks up to the moment it ends.
        code = self._finish_process()
        frames = self._frames
        if self._cancelled:
            self._drop_log()
            return

        # SIGINT is how the recording ends, and ffmpeg reports being interrupted
        # as a failure; only complain when nothing was captured either.
        if frames < MIN_FRAMES:
            tail = self._error_tail()
            self._drop_log()
            try:
                os.unlink(self._path)
            except OSError:
                pass
            self.failed.emit(
                t("Nothing was recorded: {error}", error=tail or f"ffmpeg → {code}")
                if tail or code else t("Recording too short, speak for at least 0.3 s")
            )
            return
        self._drop_log()
        # A microphone that handed nothing over costs the left channel, and the
        # recording is kept anyway: the right one is everyone else, and an hour
        # of them is worth more than an empty channel costs. Only the split
        # capture can starve a device this way; one ffmpeg reading both cannot.
        if (self._split_inputs and frames >= RATE * QUIET_MIC_SECONDS
                and self._mic_zero_frames / frames > QUIET_MIC_SHARE):
            self.warned.emit(t(
                "The microphone handed over almost nothing ({percent}% of the "
                "recording was empty), so your own side of the meeting will be "
                "mostly missing. Check the device before the next one.",
                percent=round(self._mic_zero_frames / frames * 100),
            ))
        self.stopped.emit(self._path, frames / RATE)

    def _drop_log(self):
        for log in self._logs:
            try:
                log.close()
            except OSError:
                pass
        self._logs = []


def chunk_levels(chunk):
    """(peak, rms) in 0..1. Peak drives the waveform, RMS drives the silence check."""
    samples = array.array("h")
    usable = len(chunk) - (len(chunk) % 2)
    if usable <= 0:
        return 0.0, 0.0
    samples.frombytes(chunk[:usable])
    peak = max(abs(min(samples)), abs(max(samples))) / 32768.0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0
    return min(1.0, peak), min(1.0, rms)


def stereo_levels(chunk):
    """(left peak, right peak) in 0..1 from interleaved stereo s16."""
    samples = array.array("h")
    usable = len(chunk) - (len(chunk) % 4)
    if usable <= 0:
        return 0.0, 0.0
    samples.frombytes(chunk[:usable])
    left, right = samples[0::2], samples[1::2]
    return _peak(left), _peak(right)


def interleave_mono(left, right):
    """Two mono-s16 buffers into one stereo-s16 buffer, the shorter one setting
    the length."""
    left_samples, right_samples = _samples(left), _samples(right)
    frames = min(len(left_samples), len(right_samples))
    stereo_samples = array.array("h", bytes(frames * 2 * SAMPLE_WIDTH))
    stereo_samples[0::2] = left_samples[:frames]
    stereo_samples[1::2] = right_samples[:frames]
    return stereo_samples.tobytes()


def _read_blocks(stream, blocks, size):
    """One stream's blocks onto its queue, ending with the empty one.

    A queue that stays full is the pump having given up on this recording, and
    then there is nobody left to hand anything to.
    """
    try:
        while True:
            block = _read_exact(stream, size)
            blocks.put(block, timeout=STALL_SECONDS)
            if not block:
                return
    except (OSError, ValueError, queue.Full):
        pass


def _next_block(blocks):
    """The next block of a stream, empty once it ends or falls silent."""
    try:
        return blocks.get(timeout=STALL_SECONDS)
    except queue.Empty:
        return b""


def _read_exact(stream, size):
    """Read one meter-sized block, tolerating short unbuffered pipe reads."""
    out = bytearray()
    while len(out) < size:
        chunk = stream.read(size - len(out))
        if not chunk:
            break
        out.extend(chunk)
    return bytes(out)


def _zero_samples(chunk):
    return _samples(chunk).count(0)


def _samples(chunk):
    samples = array.array("h")
    samples.frombytes(chunk[:len(chunk) - len(chunk) % SAMPLE_WIDTH])
    return samples


def _peak(samples):
    if not samples:
        return 0.0
    return min(1.0, max(abs(min(samples)), abs(max(samples))) / 32768.0)


# --- the sound system, one group per machine -------------------------------

# How the one PulseAudio process merges: each input down to mono at our own
# rate, then the two of them into the left and right of one stream. A Mac does
# the first half per process and the second half itself, in interleave_mono().
MERGE_FILTER = (
    f"[0:a]aresample={RATE}:async=1,aformat=sample_fmts=s16:channel_layouts=mono[m];"
    f"[1:a]aresample={RATE}:async=1,aformat=sample_fmts=s16:channel_layouts=mono[s];"
    "[m][s]amerge=inputs=2[out]"
)


def _pulse_record(target):
    """parec, or pw-record where PulseAudio's tools were left out.

    parec works with both PulseAudio and PipeWire's PulseAudio compatibility
    service, and its source names are the same ones shown by list_sources().
    Keep pw-record as the fallback for minimal native-PipeWire installations.
    """
    if shutil.which("parec"):
        cmd = [
            "parec", "--record", "--raw", f"--rate={RATE}",
            f"--channels={CHANNELS}", "--format=s16le",
            # Left alone, parec holds about two seconds before handing anything
            # over, and then hands over all of it at once: the level meter sits
            # still and jumps, and the tail of a recording can be lost on the
            # way out. A chunk of the meter is the unit the rest of this file
            # is measured in, so ask for that.
            f"--latency-msec={CHUNK_LATENCY_MS}",
        ]
        if target:
            cmd.append(f"--device={target}")
        return cmd
    if shutil.which("pw-record"):
        cmd = [
            "pw-record", *_pw_record_raw_option(), f"--rate={RATE}",
            f"--channels={CHANNELS}", "--format=s16",
        ]
        if target:
            cmd.append(f"--target={target}")
        cmd.append("-")
        return cmd
    return []


def _pw_record_raw_option():
    """Use --raw only on pw-record releases that provide it.

    PipeWire gained --raw in 1.4, and in the same release stopped treating a
    filename of "-" as raw on its own: before it, the option is refused and the
    recorder dies before any sound arrives; after it, leaving the option out
    wraps the stream in a container the rest of this file would read as noise.
    Ubuntu 24.04 and anything else still on 1.0 or 1.2 sit on the near side of
    that line, so ask the installed binary which form it understands.
    """
    try:
        result = subprocess.run(
            ["pw-record", "--help"], capture_output=True, text=True, timeout=2
        )
        help_text = (result.stdout or "") + (result.stderr or "")
    except (subprocess.SubprocessError, OSError):
        return ["--raw"]  # preserve the existing command when probing itself fails
    if not help_text.strip():
        return ["--raw"]
    return ["--raw"] if "--raw" in help_text else []


def _pulse_meeting(mic_target, system_target):
    """One process for both devices: PulseAudio keeps them aligned itself."""
    return [[
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-f", "pulse", "-thread_queue_size", "4096", "-i", mic_target or "default",
        "-f", "pulse", "-thread_queue_size", "4096", "-i", system_target,
        "-filter_complex", MERGE_FILTER, "-map", "[out]",
        "-f", "s16le", "-ar", str(RATE), "-",
    ]]


def _pactl_sources():
    if not shutil.which("pactl"):
        return []
    try:
        out = subprocess.run(
            ["pactl", "-f", "json", "list", "sources"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        return json.loads(out)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        return []


def _pulse_inputs():
    return [
        (src.get("name", ""), src.get("description") or src.get("name", ""))
        for src in _pactl_sources()
        if not src.get("name", "").endswith(".monitor")
    ]


def _pulse_outputs():
    return [
        (src.get("name", ""), src.get("description") or src.get("name", ""))
        for src in _pactl_sources()
        if src.get("name", "").endswith(".monitor")
    ]


def _pulse_default_output():
    if not shutil.which("pactl"):
        return ""
    try:
        sink = subprocess.run(
            ["pactl", "get-default-sink"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""
    if not sink:
        return ""
    monitor = f"{sink}.monitor"
    names = {name for name, _ in _pulse_outputs()}
    return monitor if not names or monitor in names else ""


# macOS hands out no monitor of its own: what the speakers are playing is not
# an input, and the only way to record it is a driver that pretends to be one.
# These are the three people install.
LOOPBACK_DEVICES = ("blackhole", "loopback", "soundflower")


def _avfoundation_record(target):
    if not shutil.which("ffmpeg"):
        return []
    target = _resolve_avfoundation_target(target)
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
        # AVFoundation names an input "video:audio", so the empty half in front
        # of the colon is what says this recording has no picture in it.
        "-f", "avfoundation", "-i", f":{target or 'default'}",
        "-ac", str(CHANNELS), "-ar", str(RATE), "-f", "s16le", "-",
    ]


def _avfoundation_meeting(mic_target, system_target):
    """A process per device, both names read off the same device listing.

    Asking ffmpeg what is plugged in costs a process of its own, and a listing
    taken twice could renumber in between: the two targets have to be resolved
    against the same one to name the same machine the user picked from.
    """
    inputs = _avfoundation_inputs()
    return [_avfoundation_meeting_capture(mic_target, inputs),
            _avfoundation_meeting_capture(system_target, inputs)]


def _avfoundation_meeting_capture(target, inputs=None):
    target = _resolve_avfoundation_target(target, inputs)
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-thread_queue_size", "4096",
        "-f", "avfoundation", "-i", f":{target or 'default'}",
        "-af", (f"aresample={RATE}:async=1:first_pts=0,"
                "aformat=sample_fmts=s16:channel_layouts=mono"),
        "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-",
    ]


def _avfoundation_inputs():
    """[(index, name)] for every capture device AVFoundation offers.

    The index is what the recorder is given, because that is what ffmpeg takes;
    it changes when devices are plugged in, which is why the name is shown.
    """
    if not shutil.which("ffmpeg"):
        return []
    try:
        # Listing devices is not a thing ffmpeg can do without an input, so it
        # is asked for one it cannot open: the list comes out on stderr and the
        # command then fails, which is the documented way of doing this.
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "avfoundation",
             "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=8, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return []

    devices, listing = [], False
    for line in result.stderr.splitlines():
        if "AVFoundation audio devices:" in line:
            listing = True
            continue
        if not listing:
            continue
        match = re.search(r"\[(\d+)\]\s+(.+)$", line)
        if match:
            devices.append((match.group(1), match.group(2).strip()))
    return devices


def _avfoundation_named_inputs():
    """Stable settings values: the name is saved, never the moving index."""
    return [(description, description)
            for _index, description in _avfoundation_inputs()]


def _resolve_avfoundation_target(target, inputs=None):
    """Resolve a stored device name to its current, positional ffmpeg index."""
    if not target or target == "default":
        return "default"
    if str(target).isdigit():
        raise AudioDeviceError(t(
            "The saved macOS audio device uses an old numeric index. Open "
            "Settings and select the device again before recording."
        ))
    if inputs is None:
        inputs = _avfoundation_inputs()
    matches = [index for index, description in inputs
               if description == target]
    if not matches:
        raise AudioDeviceError(t(
            "The saved macOS audio device is no longer connected: {device}. "
            "Open Settings and select another device.", device=target,
        ))
    if len(matches) > 1:
        raise AudioDeviceError(t(
            "More than one macOS audio device is named {device}. Disconnect the "
            "duplicate or choose a different device.", device=target,
        ))
    return matches[0]


def _avfoundation_default_output():
    for _index, description in _avfoundation_inputs():
        if any(word in description.lower() for word in LOOPBACK_DEVICES):
            return description
    return ""


Sound = collections.namedtuple(
    "Sound",
    # How to capture one source and how to capture two at once, that one as the
    # list of processes it takes, the two device lists, which device a meeting
    # records the far side from, and what to say when the programs for any of
    # it are not installed.
    "record meeting inputs outputs default_output missing",
)

PULSE = Sound(
    record=_pulse_record,
    meeting=_pulse_meeting,
    inputs=_pulse_inputs,
    outputs=_pulse_outputs,
    default_output=_pulse_default_output,
    missing="No audio recorder found. Install pulseaudio-utils or pipewire-audio.",
)

COREAUDIO = Sound(
    record=_avfoundation_record,
    meeting=_avfoundation_meeting,
    inputs=_avfoundation_named_inputs,
    # Every macOS capture device is offered as the far side of a meeting, the
    # loopback driver among them: there is no way to tell them apart, and an
    # empty list would leave nothing to pick.
    outputs=_avfoundation_named_inputs,
    default_output=_avfoundation_default_output,
    missing="ffmpeg not found. Install it with: brew install ffmpeg",
)


def sound():
    """The programs this machine records through."""
    return COREAUDIO if sys.platform == "darwin" else PULSE


def list_sources():
    """[(name, description)] for every real input source."""
    return sound().inputs()


def list_monitors():
    """[(name, description)] for whatever can be recorded as the other side.

    On Linux that is the monitor of an output, and recording it is recording
    whatever is being played: in a meeting the other participants, and nothing
    of your own microphone.
    """
    return sound().outputs()


def default_monitor():
    """The device the far side of a meeting comes from, or ''."""
    return sound().default_output()
