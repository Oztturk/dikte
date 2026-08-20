"""Where Dikte keeps its settings and its data, for the system it is on.

A module of its own because two others need the answer and one of them cannot
ask the other: config.py imports ggml.py, so ggml.py cannot import config.py
back. Left alone, each worked it out for itself, and only config.py knew about
macOS. The result on a Mac was settings under `~/Library/Application Support`
and several gigabytes of models under `~/.local/share`, which is not a place a
Mac user looks, and not a place `uninstall.sh --purge` would have deleted from.

Read at import, as the modules that use it already do. `directories()` takes the
platform as an argument so that a test can stand on the other one.
"""

import os
import pathlib
import subprocess
import sys

# The one other platform constant every subprocess site needs, kept in this
# leaf so no caller has to pull the audio stack in for it: console programs
# started from a windowless process would otherwise each open a console window
# of their own on Windows.
NO_WINDOW = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
             if sys.platform == "win32" else 0)


def _env(var, default):
    """The directory a variable names, or the one it stands in for."""
    return pathlib.Path(os.environ.get(var) or os.path.expanduser(default))


def directories(platform=None):
    """(settings, data), in the two places this system keeps them.

    macOS keeps both in the one directory a Mac user's backup already knows
    about. Windows keeps them apart on purpose: settings roam with the account,
    and several gigabytes of models are exactly what a roaming profile must not
    carry. Everywhere else they are separate and follow the XDG variables.
    """
    here = platform or sys.platform
    if here == "darwin":
        support = pathlib.Path.home() / "Library/Application Support/Dikte"
        return support, support
    if here == "win32":
        roaming = _env("APPDATA", "~/AppData/Roaming")
        local = _env("LOCALAPPDATA", "~/AppData/Local")
        return roaming / "Dikte", local / "Dikte"
    return (_env("XDG_CONFIG_HOME", "~/.config") / "dikte",
            _env("XDG_DATA_HOME", "~/.local/share") / "dikte")


CONFIG_DIR, DATA_DIR = directories()
