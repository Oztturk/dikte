#!/usr/bin/env bash
# Dikte installer: dependency check, launchers, global shortcuts.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$(command -v python3)"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
AUTOSTART_DIR="$HOME/.config/autostart"
SHORTCUT="${1:-Ctrl+Space}"
# Without the colon, so that a second argument given as "" stays empty. That is
# how update.sh says "this one was turned off", as against not saying anything.
CANCEL_SHORTCUT="${2-Ctrl+Alt+Space}"

say()  { printf '  %s\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }

echo
echo "Installing Dikte"
echo "────────────────"

# 1. Dependencies ----------------------------------------------------------
missing=()
audio_cmds=(ffmpeg)
if command -v parec >/dev/null || command -v pw-record >/dev/null; then
  :
else
  missing+=("pulseaudio-utils-or-pipewire-audio")
fi
if [[ "${XDG_SESSION_TYPE:-}" == "x11" ]]; then
  desktop_cmds=(xclip xdotool)
else
  desktop_cmds=(wl-copy wl-paste ydotool)
fi
for cmd in "${audio_cmds[@]}" "${desktop_cmds[@]}"; do
  command -v "$cmd" >/dev/null || missing+=("$cmd")
done
python3 -c 'import PyQt6.QtWidgets' 2>/dev/null || missing+=("python-pyqt6")

if ((${#missing[@]})); then
  warn "Missing: ${missing[*]}"
  say  "Ubuntu X11:    sudo apt install pulseaudio-utils xclip xdotool ffmpeg"
  say  "Arch Wayland:  sudo pacman -S --needed pipewire-audio wl-clipboard ydotool ffmpeg python-pyqt6"
  echo
else
  ok "All dependencies present"
fi

# 2. ydotoold --------------------------------------------------------------
if [[ "${XDG_SESSION_TYPE:-}" != "x11" ]] && command -v ydotool >/dev/null; then
  if systemctl --user is-active --quiet ydotool 2>/dev/null \
     || systemctl --user is-active --quiet ydotoold 2>/dev/null; then
    ok "ydotoold is running (auto-paste ready)"
  else
    warn "ydotoold is not running, auto-paste will not work"
    say  "systemctl --user enable --now ydotool"
  fi
fi

# 3. Launchers -------------------------------------------------------------
mkdir -p "$BIN_DIR" "$APP_DIR" "$AUTOSTART_DIR"
ln -sf "$DIR/dikte.py" "$BIN_DIR/dikte"
chmod +x "$DIR/dikte.py"
ok "Command installed: $BIN_DIR/dikte"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not on your PATH. For fish: fish_add_path $BIN_DIR" ;;
esac

cat > "$APP_DIR/dikte.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Dikte
Comment=Voice dictation: record, transcribe, clean up, paste
Exec=$PY $DIR/dikte.py
Icon=audio-input-microphone
Categories=Utility;AudioVideo;
StartupNotify=false
EOF
ok "Application menu entry added"

cat > "$AUTOSTART_DIR/dikte.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Dikte
Exec=$PY $DIR/dikte.py
Icon=audio-input-microphone
X-GNOME-Autostart-enabled=true
StartupNotify=false
EOF
ok "Will start automatically on login"

# 4. Global shortcuts ------------------------------------------------------
# Two of them: one to start and stop, one to throw the recording away. The
# second is worth a key of its own because stopping is the step there is no
# taking back, being what sends the audio off to be transcribed.
#
# Dikte registers them rather than this script writing the files itself: it
# knows which desktop it is on, and it stores the combination in the settings
# as well, which is where the built-in listener reads it from. A key written
# to only one of the two places is a key that half works.
if [[ "$SHORTCUT" == "$CANCEL_SHORTCUT" ]]; then
  warn "Both arguments are $SHORTCUT, so the discard key was left out."
  say  "Pass two different combinations, or set it in Settings → Shortcuts."
  CANCEL_SHORTCUT=""
fi

register() {   # which  combination  label
  if out="$("$PY" "$DIR/dikte.py" shortcut install "$1" --combo "$2" 2>&1)"; then
    ok "$3: $2"
  else
    # One line: the rest of what it has to say about KWin is printed below.
    warn "${out%%$'\n'*}"
  fi
}

if python3 -c 'import PyQt6.QtWidgets' 2>/dev/null; then
  register toggle "$SHORTCUT" "Start and stop"
  if [[ -n "$CANCEL_SHORTCUT" ]]; then
    register cancel "$CANCEL_SHORTCUT" "Discard the recording"
  fi
  if [[ "${XDG_CURRENT_DESKTOP:-}" != *[Gg][Nn][Oo][Mm][Ee]* ]]; then
    warn "KWin only reads these at startup, so they go live after your next"
    say  "login. Until then open Settings → Shortcuts and turn on the"
    say  "built-in listener to use them right away."
  fi
else
  warn "PyQt6 is missing, so no shortcut was registered. Install it, then run:"
  say  "dikte shortcut install toggle --combo '$SHORTCUT'"
fi

echo
ok "Done. Start it with:  dikte"
say "The settings window opens on first run: download a speech model, or add"
say "an OpenAI or OpenRouter key instead."
echo
