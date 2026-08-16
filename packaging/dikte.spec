# PyInstaller's description of the build, shared by the AppImage and the disk
# image. Run it through build-appimage.sh or build-dmg.sh rather than by hand:
# each of those has a few steps of its own on either side of this.
#
# A directory rather than a single file, on both platforms. Onefile unpacks
# itself into /tmp on every start, which for something a global shortcut is
# meant to bring up is a second of nothing happening, and for the AppImage it
# would be an unpacking inside an unpacking. The single file people download is
# the AppImage and the .dmg; this only has to be tidy inside them.

import os
import pathlib
import re
import sys

ROOT = pathlib.Path(SPECPATH).parent            # noqa: F821  (PyInstaller's)

# Read rather than imported. Putting the checkout on sys.path to import dikte
# would put this directory there under the name `packaging`, which is a real
# library that PyInstaller itself uses, and a spec file is no place to find out
# whether that matters.
__version__ = re.search(r'^__version__ = "(.*)"$',
                        (ROOT / "dikte" / "__init__.py").read_text(),
                        re.M).group(1)

MACOS = sys.platform == "darwin"
BUNDLE_ID = "io.github.yusufipk.dikte"

# PyQt6's wheel is most of the build, and most of the wheel is modules nothing
# here imports: Qt ships a browser engine, three declarative UI stacks and a
# 3D renderer. Naming them keeps the download to something a person on a slow
# connection will actually finish. Only the four in dikte's imports are left.
UNUSED_QT = [
    "PyQt6." + name for name in (
        "Qt3DAnimation", "Qt3DCore", "Qt3DExtras", "Qt3DInput", "Qt3DLogic",
        "Qt3DRender", "QtBluetooth", "QtCharts", "QtDataVisualization",
        "QtDesigner", "QtHelp", "QtLocation", "QtMultimedia",
        "QtMultimediaWidgets", "QtNfc", "QtPdf", "QtPdfWidgets",
        "QtPositioning", "QtQml", "QtQuick", "QtQuick3D", "QtQuickWidgets",
        "QtRemoteObjects", "QtSensors", "QtSerialPort", "QtSpatialAudio",
        "QtSql", "QtTest", "QtTextToSpeech", "QtWebChannel", "QtWebEngineCore",
        "QtWebEngineQuick", "QtWebEngineWidgets", "QtWebSockets",
    )
]

analysis = Analysis(                            # noqa: F821
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    hiddenimports=["PyQt6.QtNetwork"],
    # tkinter is the other GUI toolkit CPython ships and would be dead weight;
    # dikte's own tests have no business in a build at all.
    excludes=UNUSED_QT + ["tkinter", "tests"],
    noarchive=False,
)

archive = PYZ(analysis.pure)                    # noqa: F821

executable = EXE(                               # noqa: F821
    archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="Dikte" if MACOS else "dikte",
    console=False,
    # Both platforms use whatever the machine is, because neither build is
    # cross-compiled: the workflow runs one job per architecture.
    target_arch=None,
    # Ad-hoc, and only on a Mac, where an arm64 binary that carries no
    # signature at all is refused by the kernel rather than merely warned
    # about. build-dmg.sh signs the finished bundle over the top of this.
    codesign_identity="-" if MACOS else None,
)

collection = COLLECT(                           # noqa: F821
    executable,
    analysis.binaries,
    analysis.datas,
    name="dikte",
)

if MACOS:
    # LSUIElement is the line that makes this a menu bar application: no Dock
    # icon, no menu of its own, nothing in the app switcher. The usage strings
    # are not decoration either, they are what the permission dialogs read out,
    # and a bundle that asks for the microphone without one is killed rather
    # than asked about.
    app = BUNDLE(                               # noqa: F821
        collection,
        name="Dikte.app",
        icon=os.environ.get("DIKTE_ICNS") or None,
        bundle_identifier=BUNDLE_ID,
        version=__version__,
        info_plist={
            "CFBundleName": "Dikte",
            "CFBundleDisplayName": "Dikte",
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "LSMinimumSystemVersion": "11.0",
            "LSUIElement": True,
            "NSHighResolutionCapable": True,
            "NSMicrophoneUsageDescription":
                "Dikte records what you dictate so that it can be transcribed.",
            "NSAppleEventsUsageDescription":
                "Dikte puts the transcript on the clipboard and pastes it into "
                "the window you were typing in.",
        },
    )
