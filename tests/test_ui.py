"""The two windows, on Qt's offscreen platform.

Not a look at the pixels: what these hold onto is the round trip through the
settings window. Every tab loads a setting into a widget and writes it back on
save, so a setting added to one half and not the other is silently reset the
next time anybody presses Save. That is the failure this catches.
"""

import sys
import unittest
from typing import ClassVar
from unittest import mock

from PyQt6.QtWidgets import QApplication, QMessageBox

import config as cfg
import overlay as overlay_module
import paste
import settings_ui
from tests.support import DikteTest, only_these_tools

# One application for the whole run; Qt allows no second one.
_app = QApplication.instance() or QApplication([])


# A valid non-default value for every setting the window shows. Anything the
# window does not touch is left out: the round trip cannot lose what it never
# reads.
CHANGED = {
    "ui_language": "tr",
    "language": "tr",
    "auto_paste": False,
    "paste_shortcut": "ctrl+shift+v",
    "restore_clipboard": True,
    "overlay_corner": "top-right",
    "max_seconds": 120,
    "skip_silent": False,
    "silence_db": -42.0,
    "filter_hallucinations": False,
    "keep_audio": True,
    "openai_api_key": "sk-test-key",
    "openrouter_api_key": "sk-or-test-key",
    "transcribe_provider": "openrouter",
    "transcribe_model": "whisper-1",
    "openrouter_transcribe_model": "openai/whisper-1",
    "cleanup_enabled": False,
    "cleanup_model": "some/other-model",
    "cleanup_reasoning": "high",
    "cleanup_prompt": "Only fix the punctuation.",
    "file_cleanup_prompt": "Keep the stamps where they are.",
    "transcribe_prompt": "Paraşüt, OpenFrame",
    "assistant_provider": "codex",
    "assistant_model": "opus",
    "assistant_permission_mode": "manual",
    "assistant_codex_model": "gpt-5",
    "assistant_codex_sandbox": "read-only",
    "assistant_openrouter_model": "some/agent-model",
    "assistant_reasoning": "high",
    "assistant_dir": "/tmp",
    "assistant_timeout": 600,
    "assistant_session_minutes": 90,
    "assistant_paste": False,
    "assistant_cleanup": True,
    "assistant_prompt": "Answer in one sentence.",
    "assistant_shortcut": "Meta+A",
    "meeting_self_name": "Yusuf",
    "meeting_other_name": "Ayşe",
    "meeting_participants": "Mehmet",
    "meeting_model": "some/meeting-model",
    "meeting_reasoning": "medium",
    "meeting_language": "tr",
    "meeting_cleanup": False,
    "meeting_max_seconds": 7200,
    "meeting_keep_audio": True,
    "meeting_shortcut": "Meta+M",
    "meeting_prompt": "Write it as bullet points.",
    "file_timestamps": True,
    "file_cleanup": False,
    "shortcut": "Ctrl+Alt+Space",
    "evdev_hotkey": True,
    "history_limit": 50,
}


class Settings(DikteTest):
    # What a Mac shows instead, where the combination on offer is a different
    # one. Everything else about the window is the same on both.
    changed = CHANGED
    platform = "linux"

    def setUp(self):
        super().setUp()
        # No pactl, no model lists over the network, and no modal dialogue
        # waiting for somebody to press OK.
        self.enterContext(mock.patch.object(sys, "platform", self.platform))
        self.enterContext(only_these_tools())
        self.enterContext(mock.patch.object(QMessageBox, "information"))
        self.enterContext(mock.patch.object(settings_ui.SettingsWindow,
                                            "_load_models"))
        self.enterContext(mock.patch.object(settings_ui.SettingsWindow,
                                            "_load_transcribe_models"))
        self.enterContext(mock.patch.object(settings_ui.hotkey, "APPLICATIONS_DIR",
                                            self.path("applications")))
        self.enterContext(mock.patch.object(settings_ui.hotkey, "SHORTCUTS_FILE",
                                            self.path("kglobalshortcutsrc")))

    def window(self, conf):
        window = settings_ui.SettingsWindow(conf, "dikte toggle")
        self.addCleanup(window.deleteLater)
        self.addCleanup(window.close)
        return window

    def test_the_window_opens_with_every_tab_on_it(self):
        window = self.window(cfg.Config())
        tabs = window.findChildren(settings_ui.QTabWidget)[0]
        self.assertEqual(tabs.count(), 9)
        self.assertEqual(window.windowTitle(), "Dikte Settings")

    def test_saving_without_touching_anything_changes_nothing(self):
        """Every widget has to load what is stored, or Save writes its default
        over it. This says so for the whole table at once."""
        conf = cfg.Config()
        before = dict(conf.data)
        self.window(conf)._save()
        self.assertEqual(conf.data, before)

    def test_a_setting_of_your_own_survives_the_round_trip(self):
        self.write_config(self.changed)
        conf = cfg.Config()
        self.window(conf)._save()
        stored = self.read_config_file()
        for key, value in self.changed.items():
            with self.subTest(key=key):
                self.assertEqual(stored[key], value)

    def test_the_settings_the_window_does_not_show_are_left_alone(self):
        """A tab nobody wrote must not reset what the command line set."""
        self.write_config({"silence_db": -42.0, "speech_margin_db": 15.0,
                           "openrouter_base_url": "http://localhost:1234/v1"})
        conf = cfg.Config()
        self.window(conf)._save()
        stored = self.read_config_file()
        self.assertEqual(stored["speech_margin_db"], 15.0)
        self.assertEqual(stored["openrouter_base_url"], "http://localhost:1234/v1")

    def test_a_prompt_left_at_its_default_is_stored_as_empty(self):
        """So that switching the interface language switches the prompt too."""
        conf = cfg.Config()
        self.window(conf)._save()
        self.assertEqual(conf["cleanup_prompt"], "")
        self.assertEqual(conf["meeting_prompt"], "")
        self.assertEqual(conf["assistant_prompt"], "")

    def test_each_provider_keeps_its_own_transcription_model(self):
        self.write_config({"transcribe_provider": "openai",
                           "transcribe_model": "gpt-4o-transcribe",
                           "openrouter_transcribe_model": "openai/whisper-1"})
        conf = cfg.Config()
        window = self.window(conf)
        window.transcribe_provider.setCurrentIndex(
            window.transcribe_provider.findData("openrouter"))
        window._save()
        self.assertEqual(conf["transcribe_provider"], "openrouter")
        self.assertEqual(conf["transcribe_model"], "gpt-4o-transcribe")

    def test_saving_applies_the_lowered_history_limit_at_once(self):
        for index in range(10):
            cfg.append_history({"ts": "now", "text": str(index)})
        self.write_config({"history_limit": 3})
        self.window(cfg.Config())._save()
        self.assertEqual(len(cfg.read_history()), 3)

    def test_saving_tells_whoever_is_listening(self):
        conf = cfg.Config()
        window = self.window(conf)
        applied = []
        window.applied.connect(lambda: applied.append(True))
        window._save()
        self.assertEqual(applied, [True])

    def test_the_window_is_readable_in_turkish_too(self):
        self.write_config({"ui_language": "tr"})
        window = self.window(cfg.Config())
        self.assertEqual(window.windowTitle(), "Dikte Ayarları")


class MacSettings(Settings):
    """The same window and the same round trip, standing on a Mac.

    Nothing here is about macOS: it is the rest of the window, checked on the
    platform where three of its widgets are gone and one offers other keys.
    """

    platform = "darwin"
    changed: ClassVar[dict] = {**CHANGED, "paste_shortcut": "cmd+shift+v"}

    def test_there_is_no_install_button_where_nothing_is_installed(self):
        window = self.window(cfg.Config())
        labels = [button.text() for button in
                  window.findChildren(settings_ui.QPushButton)]
        self.assertFalse([text for text in labels if "shortcut" in text.lower()])

    def test_the_listener_is_not_offered_as_a_choice(self):
        """It is the whole mechanism there; turning it off would leave nothing."""
        window = self.window(cfg.Config())
        self.assertFalse(window.evdev_enabled.isVisible())

    def test_the_paste_keys_on_offer_are_the_ones_a_mac_uses(self):
        window = self.window(cfg.Config())
        offered = [window.paste_shortcut.itemText(index)
                   for index in range(window.paste_shortcut.count())]
        self.assertEqual(offered, paste.MACOS.shortcuts)


class Overlay(DikteTest):
    def overlay(self, **kwargs):
        widget = overlay_module.Overlay(**kwargs)
        self.addCleanup(widget.deleteLater)
        self.addCleanup(widget.close)
        return widget

    def test_it_never_takes_focus(self):
        """It appears while you are typing; stealing the keyboard would be rude."""
        from PyQt6.QtCore import Qt
        flags = self.overlay().windowFlags()
        self.assertTrue(flags & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.assertTrue(flags & Qt.WindowType.WindowStaysOnTopHint)

    def test_recording_then_working_then_done(self):
        widget = self.overlay()
        widget.show_recording()
        self.assertTrue(widget.showing)
        widget.push_level(0.5)
        widget.set_seconds(3)
        widget.show_busy("Transcribing…")
        widget.show_done("Pasted")
        widget._conceal()
        self.assertFalse(widget.showing)

    def test_a_meeting_shows_both_sides(self):
        widget = self.overlay()
        widget.show_meeting()
        widget.push_levels(0.4, 0.7)
        self.assertTrue(widget.showing)

    def test_every_corner_is_understood(self):
        for corner in ("top-left", "top-right", "bottom-left", "bottom-right"):
            with self.subTest(corner=corner):
                widget = self.overlay(corner=corner)
                widget.show_recording()
                widget._reposition()

    def test_a_warning_and_an_error_both_show(self):
        widget = self.overlay()
        widget.show_warning("cleanup failed")
        widget.show_error("no microphone")

    def test_one_indicator_can_stack_above_another(self):
        first = self.overlay()
        first.show_recording()
        second = self.overlay(below=first)
        second.show_busy("Asking Claude…")
        self.assertTrue(second.showing)

    def test_a_job_in_progress_can_be_waved_away(self):
        """Ten minutes of work should not have to be watched for ten minutes."""
        widget = self.overlay(dismissable=True)
        widget.show_busy("Asking Claude…")
        widget.dismiss()
        self.assertFalse(widget.showing)

    def test_progress_stays_away_once_it_was_waved_off(self):
        widget = self.overlay(dismissable=True)
        widget.show_busy("Asking Claude…")
        widget.muted = True
        widget.dismiss()
        widget.show_busy("Reading a web page…")
        self.assertFalse(widget.showing)

    def test_the_outcome_shows_even_so(self):
        """Waving it away asks not to be watched, not to be kept in the dark."""
        widget = self.overlay(dismissable=True)
        widget.show_busy("Asking Claude…")
        widget.muted = True
        widget.dismiss()
        widget.show_done("Pasted")
        self.assertTrue(widget.showing)

    def test_a_new_run_starts_visible_whatever_the_last_one_did(self):
        widget = self.overlay(dismissable=True)
        widget.muted = True
        widget.show_recording()
        self.assertTrue(widget.showing)
        self.assertFalse(widget.muted)


if __name__ == "__main__":
    unittest.main()
