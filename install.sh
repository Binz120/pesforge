#!/usr/bin/env bash
# Install the editor for the current user on any Linux desktop.
#
#   ./install.sh              link the launcher, add a menu entry
#   ./install.sh --uninstall  remove both (your config and backups are kept)
#
# Nothing is installed system-wide, so no root is needed.
set -uo pipefail

here=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
BIN="$HOME/.local/bin"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
LAUNCHER="$BIN/pesforge"
LAUNCHER2015="$BIN/pesforge2015"
DESKTOP="$APPS/pesforge.desktop"

if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$LAUNCHER" "$LAUNCHER2015" "$DESKTOP"
  for s in 16 32 48 64 128 256 512; do
    rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/${s}x${s}/apps/pesforge.png"
  done
  command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null
  echo "removed $LAUNCHER and $DESKTOP (config and backups left in place)"
  exit 0
fi

mkdir -p "$BIN" "$APPS"
chmod +x "$here/bin/pesforge" "$here/editor.py" 2>/dev/null

# A symlink keeps `git pull` working; a copy keeps the launcher alive if the checkout moves.
if ln -sf "$here/bin/pesforge" "$LAUNCHER" 2>/dev/null; then :; else
  cp "$here/bin/pesforge" "$LAUNCHER"
fi
chmod +x "$LAUNCHER" 2>/dev/null

chmod +x "$here/bin/pesforge2015" 2>/dev/null
if ln -sf "$here/bin/pesforge2015" "$LAUNCHER2015" 2>/dev/null; then :; else
  cp "$here/bin/pesforge2015" "$LAUNCHER2015"
fi
chmod +x "$LAUNCHER2015" 2>/dev/null

# The window icon comes from assets/pesforge.png; the menu entry needs it in the
# icon theme so `Icon=pesforge` resolves.
ICONBASE="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
for s in 16 32 48 64 128 256 512; do
  mkdir -p "$ICONBASE/${s}x${s}/apps"
  cp "$here/assets/pesforge.png" "$ICONBASE/${s}x${s}/apps/pesforge.png"
done

cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=PESForge
GenericName=Player editor
Comment=Edit players, appearance and clubs in SP Football Life 2027
Exec=$LAUNCHER
TryExec=$LAUNCHER
Icon=pesforge
Terminal=false
Categories=Game;
Keywords=pes;pesdb;football;spfl;editor;
StartupNotify=true
EOF
chmod 644 "$DESKTOP"

command -v desktop-file-validate >/dev/null 2>&1 && desktop-file-validate "$DESKTOP"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null

echo "installed:"
echo "  launcher : $LAUNCHER"
echo "  PES 2015 : $LAUNCHER2015"
echo "  menu     : $DESKTOP"
echo
if ! "$LAUNCHER" --check; then
  echo
  echo "PESForge is installed; start it once and pick your game folder when asked."
fi
echo
echo "Start it from your app menu (\"PESForge\") or by running: pesforge"
