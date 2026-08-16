"""Putting a downloaded build into the desktop it landed on.

A checkout has install.sh for this: the menu entry, the entry that starts Dikte
at login, the icon both of those name, and the `dikte` command. Somebody who
downloaded an AppImage or dragged Dikte.app out of a disk image ran no
installer at all, so the application writes those files itself, on its first
run and again whenever the file it was started from has moved.

Nothing here runs from a checkout. install.sh has already written the same
files there, pointing at the interpreter that checkout was installed against,
and overwriting them with a guess would be a downgrade.

Everything is written from the path Dikte is running as, which is why it is
also run again on every start rather than once: an AppImage that was moved out
of ~/Downloads leaves behind a menu entry naming a file that is no longer
there, and the run after the move is the only moment that can be noticed.
"""

import os
import pathlib
import plistlib
import shlex
import subprocess
import sys

# The same identifier install-mac.sh uses, because it is the name of the login
# item and both must mean the one thing when a Mac has been installed to twice.
AGENT_ID = "io.github.yusufipk.dikte"
ICON_NAME = "dikte"
DESKTOP_FILE = "dikte.desktop"


def packaged():
    """Whether this is one of the built downloads rather than a checkout."""
    return bool(getattr(sys, "frozen", False))


def target():
    """The file a launcher has to name to start this build again.

    The AppImage itself, or Dikte.app, rather than the executable inside
    either: the mount an AppImage runs from is gone by the next login, and a
    Mac starts an application through its bundle.
    """
    if os.environ.get("APPIMAGE"):
        return pathlib.Path(os.environ["APPIMAGE"])
    executable = pathlib.Path(sys.executable).resolve()
    if sys.platform == "darwin":
        for parent in executable.parents:
            if parent.suffix == ".app":
                return parent
    return executable


# The two the dynamic loader reads, and what PyInstaller renames the old value
# to when it takes one over. Only the platform's own is ever set, so looking
# for both costs nothing and keeps the two builds saying the same thing.
LIBRARY_PATHS = ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH")


def restore_library_path():
    """Put the loader's environment back to what it was. Whether it had moved.

    A build points this at the libraries it carries, and every process started
    from it inherits that. Ours are the wrong libraries for anything else on
    the machine: ffmpeg, ydotool, wl-copy and pactl are the distribution's own
    binaries built against the distribution's libstdc++, and handed the copy
    from the machine this was built on they refuse to start. So does
    AppImageLauncher, which is what running the AppImage again goes through,
    and running it again is how the command line becomes the application.

    Safe to do here because nothing of ours is looked up this way. The
    libraries this process runs on are loaded before any of this code does, and
    the ones Qt opens later, its platform plugins and image formats, are found
    through the RPATH written into them.
    """
    moved = False
    for name in LIBRARY_PATHS:
        if name not in os.environ:
            continue
        original = os.environ.pop(name + "_ORIG", None)
        # No _ORIG means there was nothing there to put back: the variable is
        # the build's own, and what the machine expects is for it to be unset.
        if original:
            os.environ[name] = original
        else:
            del os.environ[name]
        moved = True
    return moved


def bundled_bin():
    """Where a build keeps the helper programs it carries, if it carries any.

    The disk image ships an ffmpeg because macOS records through one and has
    nothing like it preinstalled, so a Mac that downloaded Dikte and nothing
    else would otherwise not be able to record at all. The AppImage carries
    none: Linux records through parec or pw-record, which come with the sound
    server, and the distributions all package ffmpeg for the rest.
    """
    binary = pathlib.Path(sys.executable).parent
    if sys.platform == "darwin" and binary.name == "MacOS":
        return binary.parent / "Resources" / "bin"
    return binary / "bin"


def add_bundled_tools():
    """Put that directory in front of PATH. Whether there was one.

    Everything that reaches for ffmpeg goes through shutil.which, so this is
    the whole of the arrangement. In front rather than behind on purpose: a Mac
    with its own ffmpeg from Homebrew still gets ours, which is the build the
    format strings in audio.py and filetranscribe.py are known to work against.
    """
    directory = bundled_bin() if packaged() else None
    if directory is None or not directory.is_dir():
        return False
    os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ.get('PATH', '')}"
    return True


def ensure():
    """Write whatever is missing or out of date. The paths that changed.

    Called on every start of a packaged build, and quiet when there is nothing
    to do, so that the cost of being started from a new location is one run
    with the wrong shortcuts rather than a reinstall.
    """
    if not packaged():
        return []
    try:
        return install()
    except OSError:
        # A read-only home, a full disk, a $HOME that is not ours. None of it
        # is a reason to refuse to start: Dikte works without a menu entry.
        return []


def install(force=False):
    """Write the launchers for this platform. The paths that changed.

    An installation that is already on the machine and still works is left
    alone unless `force`, which is what typing `dikte integrate` means. Three
    other things write these same files: install.sh for a checkout, its macOS
    half, and AppImageLauncher, which many desktops ship and which writes an
    entry of its own the first time an AppImage is run. Writing over any of
    them because somebody tried a download once would move the machine onto
    that download without saying so, and take the menu entry down with it when
    the file is deleted again.
    """
    if sys.platform == "darwin":
        return _macos_install(target(), force)
    return _linux_install(target(), force)


def remove():
    """Take them away again. The paths that were there to delete."""
    if sys.platform == "darwin":
        return _macos_remove()
    return _linux_remove()


# --- the files ------------------------------------------------------------

def _xdg(var, default):
    return pathlib.Path(os.environ.get(var) or os.path.expanduser(default))


def _paths():
    data = _xdg("XDG_DATA_HOME", "~/.local/share")
    return {
        "menu": data / "applications" / DESKTOP_FILE,
        "autostart": _xdg("XDG_CONFIG_HOME", "~/.config") / "autostart" / DESKTOP_FILE,
        "icons": data / "icons",
        "command": pathlib.Path.home() / ".local" / "bin" / "dikte",
    }


def _write(path, text):
    """Write it if it says something else. Whether it was written.

    The comparison is the point rather than an optimisation: these are read at
    login, and rewriting an unchanged autostart entry on every start is a
    modification time that backup tools and the desktop both notice.
    """
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def _exec_field(*args):
    """One Exec= line. A path with a space in it is what this is for.

    The desktop entry specification quotes with double quotes and escapes with
    a backslash, which is close enough to POSIX that shlex gets the hard part
    right, and the difference only shows up in characters no download path has.
    """
    return " ".join(
        f'"{arg}"' if any(c in arg for c in ' \t"\\$`') else arg
        for arg in args
    )


def _desktop_entry(command, autostart=False):
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=Dikte",
        f"Exec={command}",
        f"Icon={ICON_NAME}",
        "StartupNotify=false",
    ]
    if autostart:
        lines.insert(4, "X-GNOME-Autostart-enabled=true")
    else:
        lines.insert(4, "Comment=Voice dictation: record, transcribe, clean up, paste")
        lines.insert(6, "Categories=Utility;AudioVideo;")
    return "\n".join(lines) + "\n"


def _exec_targets(entry):
    """The files an Exec= line names, out of a desktop entry's text.

    The specification quotes with double quotes and escapes with a backslash,
    which is close enough to a shell that shlex gets the hard part right and
    the difference only shows up in characters no install path has.
    """
    for line in entry.splitlines():
        if line.startswith("Exec="):
            try:
                return shlex.split(line[len("Exec="):])
            except ValueError:
                return []
    return []


def _another_dikte(directory, mine):
    """A menu entry for an installation that is not this one and still works.

    Read across the whole directory rather than at our own file name, because
    AppImageLauncher does not use it: it writes appimagekit_<hash>-dikte.desktop
    and moves the AppImage under ~/Applications, and ours beside it would be a
    second Dikte in the menu naming a file that has been moved away.
    """
    if not directory.is_dir():
        return None
    for path in sorted(directory.glob("*.desktop")):
        try:
            entry = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "\nName=Dikte" not in "\n" + entry:
            continue
        words = _exec_targets(entry)
        if str(mine) in words:
            continue
        if any(os.path.exists(word) for word in words):
            return path
    return None


def _linux_install(appimage, force=False):
    paths, written = _paths(), []
    if not force:
        other = _another_dikte(paths["menu"].parent, appimage)
        if other is not None:
            return []

    command = _exec_field(str(appimage))
    if _write(paths["menu"], _desktop_entry(command)):
        written.append(paths["menu"])
    if _write(paths["autostart"], _desktop_entry(command, autostart=True)):
        written.append(paths["autostart"])
    if _icon(paths["icons"]):
        written.append(paths["icons"] / "hicolor")

    # A symlink rather than a copy, so that replacing the AppImage in place
    # replaces the command too. Anything else already sitting there is left
    # where it is: install.sh puts a symlink into a checkout here, and that
    # checkout is a working installation this has no business redirecting.
    link = paths["command"]
    ours = link.is_symlink() and os.readlink(link).endswith(".AppImage")
    if not link.exists() and not link.is_symlink() or ours or force:
        if not link.is_symlink() or os.readlink(link) != str(appimage):
            link.parent.mkdir(parents=True, exist_ok=True)
            link.unlink(missing_ok=True)
            link.symlink_to(appimage)
            written.append(link)
    return written


def _linux_remove():
    paths, gone = _paths(), []
    for key in ("menu", "autostart"):
        if paths[key].exists():
            paths[key].unlink()
            gone.append(paths[key])
    link = paths["command"]
    if link.is_symlink() and pathlib.Path(os.readlink(link)).suffix == ".AppImage":
        link.unlink()
        gone.append(link)
    for size in _icon_sizes():
        icon = paths["icons"] / "hicolor" / f"{size}x{size}" / "apps" / f"{ICON_NAME}.png"
        if icon.exists():
            icon.unlink()
            gone.append(icon)
    return gone


def _icon_sizes():
    from . import trayicon
    return trayicon.HICOLOR_SIZES


def _icon(directory):
    """Draw the icon into hicolor, if there is a GUI to draw with.

    A QPixmap needs a QGuiApplication under it, and `dikte integrate` typed at
    a terminal has only the QCoreApplication the command line builds. Nothing
    is lost by skipping it there: the next start of the application itself
    draws it, and until then the entries fall back to a generic icon.
    """
    from PyQt6.QtGui import QGuiApplication
    if not isinstance(QGuiApplication.instance(), QGuiApplication):
        return False
    from . import trayicon
    first = directory / "hicolor" / "256x256" / "apps" / f"{ICON_NAME}.png"
    if first.exists():
        return False
    trayicon.write_hicolor(directory, ICON_NAME)
    return True


# --- macOS ----------------------------------------------------------------

def _agent_path():
    return pathlib.Path.home() / "Library" / "LaunchAgents" / f"{AGENT_ID}.plist"


def _agent_plist(app):
    """Through `open` rather than the executable inside the bundle, so that the
    process is one LaunchServices started: that is what gives it the bundle's
    identity, and so the microphone and Accessibility permissions that were
    granted to Dikte rather than to launchd."""
    return plistlib.dumps({
        "Label": AGENT_ID,
        "ProgramArguments": ["/usr/bin/open", "-a", str(app)],
        "RunAtLoad": True,
        # Off on purpose: quitting from the menu bar should quit it, not
        # hand it back to launchd to start again.
        "KeepAlive": False,
        "ProcessType": "Interactive",
    })


def _macos_agent_app(agent):
    """The bundle a login item already there starts, if it still exists.

    install-mac.sh writes this same file for a checkout, pointing at the bundle
    it built under ~/Applications, where a disk image is dragged to
    /Applications instead. Two bundles both starting at login is one too many.
    """
    try:
        arguments = plistlib.loads(agent.read_bytes()).get("ProgramArguments", [])
    except (OSError, ValueError):
        return None
    for argument in arguments:
        if argument.endswith(".app") and os.path.exists(argument):
            return argument
    return None


def _macos_install(app, force=False):
    written = []
    agent = _agent_path()
    if not force and agent.exists():
        theirs = _macos_agent_app(agent)
        if theirs is not None and theirs != str(app):
            return []

    plist = _agent_plist(app)
    if not agent.exists() or agent.read_bytes() != plist:
        agent.parent.mkdir(parents=True, exist_ok=True)
        agent.write_bytes(plist)
        written.append(agent)
        _launchctl_reload(agent)

    # The command, as a wrapper rather than a symlink: the executable has to be
    # run from inside the bundle for macOS to file its permissions under Dikte,
    # and a symlink somewhere else is a different process to macOS.
    command = pathlib.Path.home() / ".local" / "bin" / "dikte"
    binary = app / "Contents" / "MacOS" / "Dikte"
    marker = "# Written by Dikte itself. Delete it to be rid of it.\n"
    script = f'#!/bin/sh\n{marker}exec {shlex.quote(str(binary))} "$@"\n'
    # install-mac.sh writes its own wrapper here, naming the checkout's Python.
    # Ours only replaces a wrapper it wrote before, or nothing at all.
    ours = command.exists() and marker in command.read_text(encoding="utf-8")
    if (not command.exists() or ours or force) and _write(command, script):
        command.chmod(0o755)
        written.append(command)
    return written


def _macos_remove():
    gone = []
    agent = _agent_path()
    if agent.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_ID}"],
                       capture_output=True, check=False)
        agent.unlink()
        gone.append(agent)
    return gone


def _launchctl_reload(agent):
    """Load the login item now, so that it does not first take effect a login
    from now. bootout first because bootstrapping a label that is already
    loaded fails, and a reinstall is exactly that case."""
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_ID}"],
                   capture_output=True, check=False)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(agent)],
                   capture_output=True, check=False)
