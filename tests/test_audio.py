"""Level metering, the WAV writer, and what the sound system is asked for.

The device list is where a platform port lands first, so the parsing is pinned
here: on Linux a source that is not a monitor is an input, one that is belongs
to the speakers, and neither list may go missing when pactl is absent. macOS
answers the same three questions out of one ffmpeg listing.

Each class says which machine it is standing on, so both halves run on either
one: nothing here reaches a real sound server.
"""

import array
import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import unittest
import wave
from unittest import mock

import audio
from tests.support import (
    DikteTest,
    FakeCompleted,
    only_these_tools,
    pcm,
    silence,
    stereo,
    tone,
)


class OnLinux:
    """A test that runs as if the machine ran PulseAudio or PipeWire."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(sys, "platform", "linux"))


class OnMacOS:
    """A test that runs as if the machine were a Mac."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(sys, "platform", "darwin"))


class ChunkLevels(unittest.TestCase):
    def test_silence(self):
        self.assertEqual(audio.chunk_levels(silence(0.1)), (0.0, 0.0))

    def test_nothing_at_all(self):
        self.assertEqual(audio.chunk_levels(b""), (0.0, 0.0))

    def test_half_a_sample_is_not_a_sample(self):
        self.assertEqual(audio.chunk_levels(b"\x00"), (0.0, 0.0))

    def test_an_odd_trailing_byte_is_ignored_rather_than_fatal(self):
        peak, _ = audio.chunk_levels(pcm([16384, 16384]) + b"\x7f")
        self.assertAlmostEqual(peak, 0.5, places=3)

    def test_the_peak_is_the_loudest_sample_either_way(self):
        peak, _ = audio.chunk_levels(pcm([0, 0, -32768, 100]))
        self.assertEqual(peak, 1.0)

    def test_the_rms_of_a_constant_signal_is_that_constant(self):
        _, rms = audio.chunk_levels(pcm([16384] * 100))
        self.assertAlmostEqual(rms, 0.5, places=3)

    def test_the_rms_sits_below_the_peak_for_a_tone(self):
        peak, rms = audio.chunk_levels(tone(0.1, amplitude=16384))
        self.assertLess(rms, peak)
        self.assertGreater(rms, 0.0)

    def test_neither_number_ever_passes_one(self):
        peak, rms = audio.chunk_levels(pcm([-32768] * 100))
        self.assertEqual(peak, 1.0)
        self.assertEqual(rms, 1.0)


class StereoLevels(unittest.TestCase):
    def test_the_channels_are_read_apart(self):
        left, right = audio.stereo_levels(stereo(pcm([16384] * 50),
                                                 pcm([0] * 50)))
        self.assertAlmostEqual(left, 0.5, places=3)
        self.assertEqual(right, 0.0)

    def test_nothing_at_all(self):
        self.assertEqual(audio.stereo_levels(b""), (0.0, 0.0))

    def test_a_partial_frame_is_ignored(self):
        self.assertEqual(audio.stereo_levels(b"\x00\x01\x00"), (0.0, 0.0))

    def test_a_meeting_with_both_sides_talking(self):
        left, right = audio.stereo_levels(stereo(pcm([8192] * 50),
                                                 pcm([-16384] * 50)))
        self.assertAlmostEqual(left, 0.25, places=3)
        self.assertAlmostEqual(right, 0.5, places=3)

    def test_two_mono_streams_are_interleaved_left_then_right(self):
        self.assertEqual(
            list(array.array("h", audio.interleave_mono(
                pcm([100, 200, 300]), pcm([-100, -200, -300])
            ))),
            [100, -100, 200, -200, 300, -300],
        )

    def test_interleaving_stops_at_the_shorter_stream(self):
        self.assertEqual(
            list(array.array("h", audio.interleave_mono(
                pcm([100, 200]), pcm([-100])
            ))),
            [100, -100],
        )


class WriteWav(DikteTest):
    def test_the_header_says_what_the_recorder_captured(self):
        path = audio.write_wav(silence(0.5))
        self.addCleanup(os.unlink, path)
        with contextlib.closing(wave.open(path, "rb")) as wav:
            self.assertEqual(wav.getnchannels(), audio.CHANNELS)
            self.assertEqual(wav.getsampwidth(), audio.SAMPLE_WIDTH)
            self.assertEqual(wav.getframerate(), audio.RATE)
            self.assertEqual(wav.getnframes(), int(audio.RATE * 0.5))

    def test_the_samples_survive(self):
        path = audio.write_wav(pcm([1000, -1000, 2000]))
        self.addCleanup(os.unlink, path)
        with contextlib.closing(wave.open(path, "rb")) as wav:
            samples = array.array("h")
            samples.frombytes(wav.readframes(3))
        self.assertEqual(list(samples), [1000, -1000, 2000])

    def test_a_meeting_is_written_at_two_channels(self):
        path = audio.write_wav(stereo(silence(0.1), silence(0.1)), channels=2)
        self.addCleanup(os.unlink, path)
        with contextlib.closing(wave.open(path, "rb")) as wav:
            self.assertEqual(wav.getnchannels(), 2)


SOURCES = [
    {"name": "alsa_input.pci-0000_00_1f.3.analog-stereo",
     "description": "Built-in Audio Analog Stereo"},
    {"name": "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor",
     "description": "Monitor of Built-in Audio"},
    {"name": "bluez_input.AA_BB.headset", "description": ""},
]


class Devices(OnLinux, DikteTest):
    @contextlib.contextmanager
    def pactl(self, sources=None, sink=None, tools=("pactl",)):
        payloads = {
            "list": FakeCompleted(stdout=json.dumps(
                SOURCES if sources is None else sources)),
            "get-default-sink": FakeCompleted(stdout=(sink or "") + "\n"),
        }

        def run(cmd, **kwargs):
            return payloads["get-default-sink" if "get-default-sink" in cmd
                            else "list"]

        with only_these_tools(*tools), \
                mock.patch.object(subprocess, "run", side_effect=run):
            yield

    def test_no_pactl_installed(self):
        with only_these_tools():
            self.assertEqual(audio.list_sources(), [])
            self.assertEqual(audio.list_monitors(), [])
            self.assertEqual(audio.default_monitor(), "")

    def test_inputs_leave_the_monitors_out(self):
        with self.pactl():
            names = [name for name, _ in audio.list_sources()]
        self.assertEqual(names, [SOURCES[0]["name"], SOURCES[2]["name"]])

    def test_monitors_are_the_other_half(self):
        with self.pactl():
            self.assertEqual([name for name, _ in audio.list_monitors()],
                             [SOURCES[1]["name"]])

    def test_a_device_with_no_description_is_shown_by_its_name(self):
        with self.pactl():
            sources = dict(audio.list_sources())
        self.assertEqual(sources[SOURCES[2]["name"]], SOURCES[2]["name"])

    def test_pactl_output_that_is_not_json(self):
        with only_these_tools("pactl"), \
                mock.patch.object(subprocess, "run",
                                  return_value=FakeCompleted(stdout="not json")):
            self.assertEqual(audio.list_sources(), [])

    def test_pactl_that_will_not_run(self):
        with only_these_tools("pactl"), \
                mock.patch.object(subprocess, "run", side_effect=OSError("nope")):
            self.assertEqual(audio.list_sources(), [])

    def test_pactl_that_exits_non_zero(self):
        with only_these_tools("pactl"), \
                mock.patch.object(subprocess, "run",
                                  side_effect=subprocess.CalledProcessError(1, "pactl")):
            self.assertEqual(audio.list_sources(), [])

    def test_the_default_output_is_found_by_its_monitor(self):
        with self.pactl(sink="alsa_output.pci-0000_00_1f.3.analog-stereo"):
            self.assertEqual(audio.default_monitor(),
                             SOURCES[1]["name"])

    def test_a_default_sink_with_no_monitor_of_its_own(self):
        with self.pactl(sink="alsa_output.usb-something"):
            self.assertEqual(audio.default_monitor(), "")

    def test_no_default_sink_at_all(self):
        with self.pactl(sink=""):
            self.assertEqual(audio.default_monitor(), "")

    def test_a_monitor_is_trusted_when_the_list_is_empty(self):
        """pactl answered about the sink but not about the sources."""
        with self.pactl(sources=[], sink="alsa_output.usb-something"):
            self.assertEqual(audio.default_monitor(),
                             "alsa_output.usb-something.monitor")


class FakeProcess:
    """A pw-record that hands over a fixed buffer and then ends."""

    def __init__(self, data):
        self.stdout = io.BytesIO(data)
        self.stderr = io.BytesIO(b"")
        self.signals = []
        self.returncode = 0
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def send_signal(self, sig):
        self.signals.append(sig)
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return 0

    def kill(self):
        self._alive = False


class StalledProcess(FakeProcess):
    """A capture that hands over a buffer and then stops answering at all.

    Not the same thing as one that ends: the device is still there and the pipe
    is still open, and a read of it never comes back.
    """

    def __init__(self, data):
        super().__init__(data)
        self.stdout = _StalledStream(data)


class _StalledStream:
    def __init__(self, data):
        self._data = io.BytesIO(data)
        self._released = threading.Event()

    def read(self, size):
        chunk = self._data.read(size)
        if chunk:
            return chunk
        self._released.wait()
        return b""

    def release(self):
        self._released.set()


class RecordingCommand(OnLinux, DikteTest):
    """Which program captures the microphone, and how it is asked to."""

    def setUp(self):
        super().setUp()
        # Whether pw-record takes --raw is read off the installed binary, and
        # what is being tested here is the command rather than the machine the
        # test is running on. PwRecordRawOption covers the reading itself.
        self.enterContext(mock.patch.object(
            audio, "_pw_record_raw_option", return_value=["--raw"]))

    def test_parec_is_preferred(self):
        """It speaks to PulseAudio and to PipeWire's compatibility service, so
        it is the one that works on both desktops."""
        with only_these_tools("parec", "pw-record"):
            self.assertEqual(audio.recording_command()[0], "parec")

    def test_pw_record_is_the_fallback(self):
        with only_these_tools("pw-record"):
            self.assertEqual(audio.recording_command()[0], "pw-record")

    def test_neither_is_installed(self):
        with only_these_tools():
            self.assertEqual(audio.recording_command(), [])

    def test_both_capture_the_format_the_rest_of_the_code_expects(self):
        for tool in ("parec", "pw-record"):
            with self.subTest(tool=tool), only_these_tools(tool):
                cmd = audio.recording_command()
                joined = " ".join(cmd)
                self.assertIn(str(audio.RATE), joined)
                self.assertIn(str(audio.CHANNELS), joined)
                self.assertIn("s16", joined)

    def test_parec_is_asked_for_the_level_meter_s_own_chunk(self):
        """Left alone it buffers about two seconds, which the waveform shows as
        a still bar that jumps once a second, and which can cost the tail of a
        recording when the process is asked to stop."""
        with only_these_tools("parec"):
            self.assertIn(f"--latency-msec={audio.CHUNK_LATENCY_MS}",
                          audio.recording_command())

    def test_the_latency_asked_for_is_the_chunk_the_meter_reads(self):
        self.assertEqual(audio.CHUNK_LATENCY_MS,
                         round(audio.CHUNK_FRAMES / audio.RATE * 1000))

    def test_a_chosen_microphone_reaches_either_one(self):
        with only_these_tools("parec"):
            self.assertIn("--device=alsa_input.usb", audio.recording_command(
                "alsa_input.usb"))
        with only_these_tools("pw-record"):
            self.assertIn("--target=alsa_input.usb", audio.recording_command(
                "alsa_input.usb"))

    def test_no_microphone_named_means_no_device_flag(self):
        for tool, flag in (("parec", "--device="), ("pw-record", "--target=")):
            with self.subTest(tool=tool), only_these_tools(tool):
                self.assertFalse([arg for arg in audio.recording_command()
                                  if arg.startswith(flag)])


class PwRecordRawOption(DikteTest):
    """Two pw-record generations want opposite commands for the same stream.

    PipeWire 1.4 added --raw and stopped treating a filename of "-" as raw on
    its own, so the option is refused by everything older and needed by
    everything newer. The help text is the only thing that tells them apart.
    """

    def option(self, **run):
        with mock.patch.object(audio.subprocess, "run", **run):
            return audio._pw_record_raw_option()

    def test_a_version_that_offers_raw_is_asked_for_it(self):
        self.assertEqual(["--raw"], self.option(
            return_value=FakeCompleted(stdout="  -a, --raw   RAW mode\n")))

    def test_a_version_without_it_is_not(self):
        self.assertEqual([], self.option(
            return_value=FakeCompleted(stdout="  --rate  Sample rate\n")))

    def test_help_that_could_not_be_read_keeps_the_option(self):
        """Whatever is installed, the command that worked before this check
        existed is the safer guess."""
        self.assertEqual(["--raw"], self.option(side_effect=OSError))
        self.assertEqual(["--raw"], self.option(
            side_effect=subprocess.TimeoutExpired("pw-record", 2)))

    def test_help_that_said_nothing_keeps_it_too(self):
        self.assertEqual(["--raw"], self.option(return_value=FakeCompleted()))


class RecorderChain(OnLinux, DikteTest):
    """Start to WAV, with pw-record faked out."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(
            audio, "_pw_record_raw_option", return_value=["--raw"]))

    def record(self, data, target="", max_seconds=300):
        recorder = audio.Recorder()
        results = []
        failures = []
        recorder.stopped.connect(lambda *args: results.append(args))
        recorder.failed.connect(failures.append)
        proc = FakeProcess(data)
        with only_these_tools("pw-record"), \
                mock.patch.object(subprocess, "Popen", return_value=proc) as popen:
            recorder.start(target=target, max_seconds=max_seconds)
            recorder._thread.join(timeout=5)
            recorder.stop()
        return recorder, results, failures, popen

    def test_the_capture_format_is_what_the_rest_of_the_code_expects(self):
        _, _, _, popen = self.record(silence(1.0))
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[0], "pw-record")
        self.assertIn(f"--rate={audio.RATE}", cmd)
        self.assertIn(f"--channels={audio.CHANNELS}", cmd)
        self.assertIn("--format=s16", cmd)
        self.assertEqual(cmd[-1], "-")

    def test_no_target_means_no_target_flag(self):
        _, _, _, popen = self.record(silence(0.5))
        self.assertFalse([arg for arg in popen.call_args.args[0]
                          if arg.startswith("--target=")])

    def test_a_chosen_microphone_is_passed_on(self):
        _, _, _, popen = self.record(silence(0.5), target="alsa_input.usb")
        self.assertIn("--target=alsa_input.usb", popen.call_args.args[0])

    def test_a_recording_ends_as_a_wav_with_its_duration_and_levels(self):
        _, results, failures, _ = self.record(tone(1.0))
        self.assertEqual(failures, [])
        path, duration, rms = results[0]
        self.addCleanup(os.unlink, path)
        self.assertAlmostEqual(duration, 1.0, places=2)
        self.assertTrue(rms)
        self.assertGreater(max(rms), 0.0)
        with contextlib.closing(wave.open(path, "rb")) as wav:
            self.assertEqual(wav.getnframes(), audio.RATE)

    def test_a_stray_keypress_is_not_a_recording(self):
        _, results, failures, _ = self.record(silence(0.1))
        self.assertEqual(results, [])
        self.assertIn("0.3", failures[0])

    def test_a_cancelled_recording_produces_nothing(self):
        recorder = audio.Recorder()
        results = []
        recorder.stopped.connect(lambda *args: results.append(args))
        proc = FakeProcess(tone(1.0))
        with only_these_tools("pw-record"), \
                mock.patch.object(subprocess, "Popen", return_value=proc):
            recorder.start()
            recorder._thread.join(timeout=5)
            recorder.cancel()
            recorder.stop()
        self.assertEqual(results, [])

    def test_a_recording_that_runs_past_the_limit_is_cut_off(self):
        _, results, _, _ = self.record(tone(3.0), max_seconds=1)
        path, duration, _ = results[0]
        self.addCleanup(os.unlink, path)
        self.assertLessEqual(duration, 1.1)

    def test_a_recorder_that_is_not_installed_at_all(self):
        recorder = audio.Recorder()
        failures = []
        recorder.failed.connect(failures.append)
        with only_these_tools():
            recorder.start()
        self.assertEqual(len(failures), 1)
        self.assertIn("pulseaudio-utils", failures[0])

    def pump(self, data=b"", stderr=b"", stopping=False, cancelled=False):
        """Run the pump in this thread, where a queued signal would need an
        event loop nobody is running here."""
        recorder = audio.Recorder()
        failures = []
        recorder.failed.connect(failures.append)
        proc = FakeProcess(data)
        proc.stderr = io.BytesIO(stderr)
        proc._alive = False
        recorder._proc = proc
        recorder._max_bytes = 10 ** 9
        recorder._stopping = stopping
        recorder._cancelled = cancelled
        recorder._pump()
        return failures

    def test_a_recorder_that_died_on_its_own_says_so(self):
        """parec refused the device, or the sound server went away."""
        failures = self.pump(stderr=b"connection refused\n")
        self.assertEqual(len(failures), 1)
        self.assertIn("connection refused", failures[0])

    def test_a_death_with_nothing_on_stderr_still_names_the_exit_code(self):
        failures = self.pump()
        self.assertIn("exit code", failures[0])

    def test_a_recording_we_ended_ourselves_is_not_a_death(self):
        """Otherwise a stray keypress produces two errors, and the first one
        sends the user looking for a broken sound server."""
        self.assertEqual(self.pump(stopping=True), [])

    def test_a_cancelled_recording_is_not_a_death(self):
        self.assertEqual(self.pump(cancelled=True), [])

    def test_a_recorder_that_captured_something_first_is_not_a_death(self):
        self.assertEqual(self.pump(data=silence(0.5)), [])

    def test_a_short_recording_reports_only_that(self):
        _, results, failures, _ = self.record(silence(0.1))
        self.assertEqual(results, [])
        self.assertEqual(len(failures), 1)
        self.assertIn("0.3", failures[0])

    def test_a_recorder_that_could_not_start(self):
        recorder = audio.Recorder()
        failures = []
        recorder.failed.connect(failures.append)
        with only_these_tools("pw-record"), \
                mock.patch.object(subprocess, "Popen", side_effect=OSError("nope")):
            recorder.start()
        self.assertEqual(len(failures), 1)
        self.assertFalse(recorder.active)


class MeetingCommands(unittest.TestCase):
    """Pulse can share a process; AVFoundation sessions cannot."""

    def commands(self, platform, mic="", system="them"):
        with mock.patch.object(sys, "platform", platform), \
                mock.patch.object(audio, "_avfoundation_inputs", return_value=[]), \
                mock.patch.object(
                    audio, "_resolve_avfoundation_target",
                    side_effect=lambda target, inputs=None: target or "default"):
            return audio.meeting_commands(mic, system)

    def test_linux_reads_both_through_pulse(self):
        commands = self.commands("linux", mic="mine")
        self.assertEqual(len(commands), 1)
        cmd = commands[0]
        self.assertEqual(cmd.count("pulse"), 2)
        self.assertEqual(cmd[cmd.index("mine") - 1], "-i")
        self.assertEqual(cmd[cmd.index("them") - 1], "-i")

    def test_a_mac_gives_each_avfoundation_device_its_own_process(self):
        commands = self.commands("darwin", mic="mine")
        self.assertEqual(len(commands), 2)
        self.assertTrue(all(command.count("avfoundation") == 1
                            for command in commands))
        self.assertIn(":mine", commands[0])
        self.assertIn(":them", commands[1])

    def test_no_microphone_named_means_the_default_one(self):
        self.assertIn("default", self.commands("linux")[0])
        self.assertIn(":default", self.commands("darwin")[0])

    def test_pulse_merges_the_two_into_one_stereo_stream(self):
        cmd = self.commands("linux")[0]
        self.assertIn(audio.MERGE_FILTER, cmd)
        self.assertEqual(cmd[cmd.index("-map") + 1], "[out]")
        self.assertEqual(cmd[cmd.index("-f", cmd.index("-map")) + 1], "s16le")

    def test_each_mac_process_produces_clock_corrected_mono_pcm(self):
        for cmd in self.commands("darwin"):
            self.assertIn("first_pts=0", cmd[cmd.index("-af") + 1])
            self.assertEqual(cmd[cmd.index("-ac") + 1], "1")
            self.assertEqual(cmd[-2:], ["1", "-"])

    def test_neither_lets_ffmpeg_read_the_terminal(self):
        """It shares stdin with Dikte, and would eat a keypress meant for it."""
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform):
                for command in self.commands(platform):
                    self.assertIn("-nostdin", command)

    def test_both_mac_devices_are_read_off_one_listing(self):
        """Asking twice costs an ffmpeg run, and the second answer could have
        renumbered between the two."""
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(audio, "_avfoundation_inputs",
                                  return_value=[("0", "mine"),
                                                ("1", "them")]) as inputs:
            audio.meeting_commands("mine", "them")
        inputs.assert_called_once_with()


class MacMeetingRecorder(OnMacOS, DikteTest):
    # Whichever way the machine has them ordered, a name is what is saved and
    # the index it happens to hold now is what ffmpeg is given.
    DEVICES = [("0", "External Headset"), ("1", "BlackHole 2ch"),
               ("2", "MacBook Pro Microphone")]

    def devices(self):
        return mock.patch.object(audio, "_avfoundation_inputs",
                                 return_value=self.DEVICES)

    def record(self, mine, theirs):
        path = str(self.path("meeting.wav"))
        recorder = audio.MeetingRecorder()
        stopped, failed, warnings = [], [], []
        recorder.stopped.connect(lambda *args: stopped.append(args))
        recorder.failed.connect(failed.append)
        recorder.warned.connect(warnings.append)
        processes = [FakeProcess(mine), FakeProcess(theirs)]
        with only_these_tools("ffmpeg"), self.devices(), \
                mock.patch.object(subprocess, "Popen", side_effect=processes) as popen:
            recorder.start(path, "MacBook Pro Microphone", "BlackHole 2ch")
            recorder._thread.join(timeout=5)
            recorder.stop()
        return path, warnings, stopped, failed, processes, popen

    def test_the_two_capture_processes_become_one_stereo_file(self):
        path, _, stopped, failed, _, _ = self.record(
            tone(1.0, freq=440), tone(1.0, freq=880)
        )
        self.assertEqual(failed, [])
        self.assertEqual(len(stopped), 1)
        with contextlib.closing(wave.open(path, "rb")) as wav:
            self.assertEqual(wav.getnchannels(), 2)
            self.assertEqual(wav.getframerate(), audio.RATE)
            self.assertEqual(wav.getnframes(), audio.RATE)

    def test_each_avfoundation_device_is_opened_by_a_different_process(self):
        _, _, _, _, _, popen = self.record(tone(0.5), tone(0.5))
        commands = [call.args[0] for call in popen.call_args_list]
        self.assertEqual(len(commands), 2)
        self.assertTrue(all(command.count("avfoundation") == 1
                            for command in commands))
        self.assertIn(":2", commands[0])
        self.assertIn(":1", commands[1])

    def test_a_mostly_empty_microphone_is_said_out_loud_and_still_kept(self):
        """Half the file is everyone else, and an hour of them is worth more
        than the empty channel costs."""
        path, warnings, stopped, failed, _, _ = self.record(
            silence(11.0), tone(11.0)
        )
        self.assertEqual(failed, [])
        self.assertEqual(len(stopped), 1)
        self.assertTrue(os.path.exists(path))
        self.assertIn("empty", warnings[0])

    def test_a_microphone_that_was_merely_quiet_is_not_complained_about(self):
        _, warnings, stopped, _, _, _ = self.record(tone(11.0), tone(11.0))
        self.assertEqual(warnings, [])
        self.assertEqual(len(stopped), 1)

    def test_a_capture_that_falls_silent_ends_the_meeting_rather_than_hanging(self):
        """One thread taking turns on both pipes would sit on the dead read
        until somebody noticed, an hour later."""
        path = str(self.path("meeting.wav"))
        recorder = audio.MeetingRecorder()
        stopped = []
        recorder.stopped.connect(lambda *args: stopped.append(args))
        mine, theirs = StalledProcess(tone(0.512)), FakeProcess(tone(30.0))
        with only_these_tools("ffmpeg"), self.devices(), \
                mock.patch.object(audio, "STALL_SECONDS", 0.2), \
                mock.patch.object(subprocess, "Popen", side_effect=(mine, theirs)):
            try:
                recorder.start(path, "MacBook Pro Microphone", "BlackHole 2ch")
                recorder._thread.join(timeout=2)
                self.assertFalse(recorder.active)
                recorder.stop()
            finally:
                mine.stdout.release()
        self.assertAlmostEqual(stopped[0][1], 0.512, places=3)
        self.assertTrue(os.path.exists(path))

    def test_stopping_ends_both_capture_processes(self):
        _, _, _, _, processes, _ = self.record(tone(0.5), tone(0.5))
        self.assertTrue(all(process.signals for process in processes))

    def test_a_legacy_numeric_target_fails_before_recording(self):
        recorder = audio.MeetingRecorder()
        failed = []
        recorder.failed.connect(failed.append)
        with only_these_tools("ffmpeg"), self.devices(), \
                mock.patch.object(subprocess, "Popen") as popen:
            recorder.start(str(self.path("meeting.wav")), "2", "1")
        popen.assert_not_called()
        self.assertIn("old numeric index", failed[0])

    def test_a_second_capture_process_that_cannot_start_cleans_up_the_first(self):
        """A Mac left holding an open AVFoundation session records nothing
        else until it is let go."""
        path = str(self.path("meeting.wav"))
        recorder = audio.MeetingRecorder()
        failed = []
        recorder.failed.connect(failed.append)
        first = FakeProcess(tone(1.0))
        with only_these_tools("ffmpeg"), self.devices(), \
                mock.patch.object(subprocess, "Popen",
                                  side_effect=(first, OSError("refused"))):
            recorder.start(path, "MacBook Pro Microphone", "BlackHole 2ch")
        self.assertTrue(first.signals)
        self.assertIn("refused", failed[0])
        self.assertFalse(os.path.exists(path))


class MacDevices(OnMacOS, DikteTest):
    """The one ffmpeg listing all three device questions are answered from."""

    LISTING = (
        "[AVFoundation indev @ 0x7fb] AVFoundation video devices:\n"
        "[AVFoundation indev @ 0x7fb] [0] FaceTime HD Camera\n"
        "[AVFoundation indev @ 0x7fb] [1] Capture screen 0\n"
        "[AVFoundation indev @ 0x7fb] AVFoundation audio devices:\n"
        "[AVFoundation indev @ 0x7fb] [0] MacBook Pro Microphone\n"
        "[AVFoundation indev @ 0x7fb] [1] BlackHole 2ch\n"
        ": Input/output error\n"
    )

    @contextlib.contextmanager
    def listing(self, stderr=None, tools=("ffmpeg",)):
        completed = FakeCompleted(
            returncode=1, stderr=self.LISTING if stderr is None else stderr)
        with only_these_tools(*tools), \
                mock.patch.object(subprocess, "run", return_value=completed):
            yield

    def test_the_audio_half_of_the_listing_is_the_only_half_read(self):
        with self.listing():
            self.assertEqual(audio.list_sources(),
                             [("MacBook Pro Microphone", "MacBook Pro Microphone"),
                              ("BlackHole 2ch", "BlackHole 2ch")])

    def test_the_name_is_both_saved_and_shown(self):
        with self.listing():
            name, description = audio.list_sources()[1]
        self.assertEqual(name, "BlackHole 2ch")
        self.assertIn("BlackHole", description)

    def test_no_ffmpeg_installed(self):
        with only_these_tools():
            self.assertEqual(audio.list_sources(), [])
            self.assertEqual(audio.list_monitors(), [])
            self.assertEqual(audio.default_monitor(), "")

    def test_an_ffmpeg_that_will_not_run(self):
        with only_these_tools("ffmpeg"), \
                mock.patch.object(subprocess, "run", side_effect=OSError("nope")):
            self.assertEqual(audio.list_sources(), [])

    def test_a_listing_with_no_audio_section(self):
        with self.listing(stderr="[AVFoundation indev @ 0x7fb] [0] FaceTime\n"):
            self.assertEqual(audio.list_sources(), [])

    def test_the_far_side_of_a_meeting_is_offered_the_same_devices(self):
        """macOS calls none of them an output, so the loopback one is in here."""
        with self.listing():
            self.assertEqual(audio.list_monitors(), audio.list_sources())

    def test_the_loopback_driver_is_picked_out_by_name(self):
        with self.listing():
            self.assertEqual(audio.default_monitor(), "BlackHole 2ch")

    def test_the_other_two_drivers_people_install(self):
        for name in ("Loopback Audio", "Soundflower (2ch)"):
            with self.subTest(name=name):
                listing = ("AVFoundation audio devices:\n"
                           f"[0] Built-in Microphone\n[1] {name}\n")
                with self.listing(stderr=listing):
                    self.assertEqual(audio.default_monitor(), name)

    def test_a_mac_with_nothing_to_record_the_far_side_from(self):
        listing = "AVFoundation audio devices:\n[0] MacBook Pro Microphone\n"
        with self.listing(stderr=listing):
            self.assertEqual(audio.default_monitor(), "")

    def test_a_saved_name_is_resolved_against_the_current_index(self):
        with self.listing():
            self.assertEqual(audio._resolve_avfoundation_target("BlackHole 2ch"), "1")

    def test_a_saved_name_follows_the_device_when_an_earlier_one_disappears(self):
        listing = ("AVFoundation audio devices:\n"
                   "[0] BlackHole 2ch\n[1] MacBook Pro Microphone\n")
        with self.listing(stderr=listing):
            self.assertEqual(
                audio._resolve_avfoundation_target("MacBook Pro Microphone"), "1"
            )

    def test_an_old_numeric_setting_is_not_silently_reused(self):
        with self.assertRaises(audio.AudioDeviceError) as caught:
            audio._resolve_avfoundation_target("1")
        self.assertIn("old numeric index", str(caught.exception))

    def test_a_device_that_went_away_is_said_out_loud(self):
        with self.listing(), self.assertRaises(audio.AudioDeviceError) as caught:
            audio._resolve_avfoundation_target("USB Microphone")
        self.assertIn("no longer connected", str(caught.exception))

    def test_duplicate_names_are_not_guessed_between(self):
        listing = ("AVFoundation audio devices:\n"
                   "[0] USB Microphone\n[1] USB Microphone\n")
        with self.listing(stderr=listing), \
                self.assertRaises(audio.AudioDeviceError) as caught:
            audio._resolve_avfoundation_target("USB Microphone")
        self.assertIn("More than one", str(caught.exception))


class MacRecordingCommand(OnMacOS, DikteTest):
    def test_the_microphone_is_read_through_avfoundation(self):
        with only_these_tools("ffmpeg"):
            cmd = audio.recording_command()
        self.assertEqual(cmd[0], "ffmpeg")
        self.assertEqual(cmd[cmd.index("-f") + 1], "avfoundation")

    def test_the_empty_half_in_front_of_the_colon_is_the_missing_picture(self):
        with only_these_tools("ffmpeg"):
            self.assertIn(":default", audio.recording_command())
        listing = "AVFoundation audio devices:\n[2] USB Microphone\n"
        completed = FakeCompleted(returncode=1, stderr=listing)
        with only_these_tools("ffmpeg"), \
                mock.patch.object(subprocess, "run", return_value=completed):
            self.assertIn(":2", audio.recording_command("USB Microphone"))

    def test_it_captures_the_format_the_rest_of_the_code_expects(self):
        with only_these_tools("ffmpeg"):
            cmd = audio.recording_command()
        self.assertEqual(cmd[cmd.index("-ar") + 1], str(audio.RATE))
        self.assertEqual(cmd[cmd.index("-ac") + 1], str(audio.CHANNELS))
        self.assertEqual(cmd[-2:], ["s16le", "-"])

    def test_no_ffmpeg_installed(self):
        with only_these_tools():
            self.assertEqual(audio.recording_command(), [])

    def test_what_a_mac_is_told_to_install(self):
        recorder = audio.Recorder()
        failures = []
        recorder.failed.connect(failures.append)
        with only_these_tools():
            recorder.start()
        self.assertIn("brew install ffmpeg", failures[0])
        self.assertFalse(recorder.active)


class OnWindows:
    """A test that runs as if the machine ran Windows."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(sys, "platform", "win32"))


class WindowsDevices(OnWindows, DikteTest):
    """The one ffmpeg listing the device questions are answered from.

    dshow names devices rather than numbering them, and the names carry
    whatever alphabet the machine speaks, so the listing here does too.
    """

    LISTING = (
        '[dshow @ 0000020c] "Integrated Camera" (video)\n'
        '[dshow @ 0000020c]   Alternative name "@device_pnp_\\...."\n'
        '[dshow @ 0000020c] "Mikrofon Dizisi (Intel Smart Sound)" (audio)\n'
        '[dshow @ 0000020c]   Alternative name "@device_cm_{33D9A762}...."\n'
        '[dshow @ 0000020c] "Kulaklık (Soundcore Life Q30)" (audio)\n'
        "dummy: Immediate exit requested\n"
    ).encode("utf-8")

    @contextlib.contextmanager
    def listing(self, stderr=None, tools=("ffmpeg",)):
        completed = FakeCompleted(
            returncode=1, stderr=self.LISTING if stderr is None else stderr)
        with only_these_tools(*tools), \
                mock.patch.object(subprocess, "run", return_value=completed):
            yield

    def test_windows_records_through_dshow(self):
        self.assertIs(audio.sound(), audio.DSHOW)

    def test_the_audio_lines_are_the_only_ones_read(self):
        with self.listing():
            self.assertEqual(audio.list_sources(), [
                ("Mikrofon Dizisi (Intel Smart Sound)",
                 "Mikrofon Dizisi (Intel Smart Sound)"),
                ("Kulaklık (Soundcore Life Q30)",
                 "Kulaklık (Soundcore Life Q30)"),
            ])

    def test_no_ffmpeg_installed(self):
        with only_these_tools():
            self.assertEqual(audio.list_sources(), [])
            self.assertEqual(audio.recording_command(), [])

    def test_the_name_is_what_the_recorder_is_given_back(self):
        with self.listing():
            cmd = audio.recording_command("Kulaklık (Soundcore Life Q30)")
        self.assertEqual(cmd[cmd.index("-f") + 1], "dshow")
        self.assertIn("audio=Kulaklık (Soundcore Life Q30)", cmd)

    def test_no_microphone_named_means_the_first_one_listed(self):
        """dshow has no default device for an empty target to mean."""
        with self.listing():
            self.assertIn("audio=Mikrofon Dizisi (Intel Smart Sound)",
                          audio.recording_command())

    def test_a_machine_with_no_microphone_at_all(self):
        with self.listing(stderr=b'[dshow @ 0] "Integrated Camera" (video)\n'):
            self.assertEqual(audio.recording_command(), [])

    def test_an_ffmpeg_that_will_not_run(self):
        with only_these_tools("ffmpeg"), \
                mock.patch.object(subprocess, "run", side_effect=OSError("nope")):
            self.assertEqual(audio.list_sources(), [])

    def test_nothing_offers_the_far_side_of_a_meeting(self):
        """What the speakers play is not a capture device Windows hands out."""
        with self.listing():
            self.assertEqual(audio.list_monitors(), [])
            self.assertEqual(audio.default_monitor(), "")
            self.assertEqual(audio.meeting_commands("mic", "sys"), [])


if __name__ == "__main__":
    unittest.main()
