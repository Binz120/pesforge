#!/usr/bin/env bash
# Install the editor for the current user on any Linux desktop.
#
#   ./install.sh              link the launcher, add a menu entry
#   ./install.sh --uninstall  remove them (your config and backups are kept)
#
# Nothing is installed system-wide, so no root is needed.
set -uo pipefail

here=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
BIN="$HOME/.local/bin"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
LAUNCHER2015="$BIN/pesforge2015"
LAUNCHER_GTK4="$BIN/pesforge-gtk4"
DESKTOP_GTK4="$APPS/pesforge-gtk4.desktop"
# The old Tk build's launcher and menu entry, cleaned up wherever they are found.
OLD_LAUNCHER="$BIN/pesforge"
OLD_DESKTOP="$APPS/pesforge.desktop"

if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$LAUNCHER2015" "$LAUNCHER_GTK4" "$DESKTOP_GTK4" "$OLD_LAUNCHER" "$OLD_DESKTOP"
  for s in 16 32 48 64 128 256 512; do
    rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/${s}x${s}/apps/pesforge.png"
  done
  command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null
  echo "removed the launchers and the menu entry (config and backups left in place)"
  exit 0
fi

mkdir -p "$BIN" "$APPS"
rm -f "$OLD_LAUNCHER" "$OLD_DESKTOP"     # leftovers from the removed Tk build

chmod +x "$here/bin/pesforge2015" 2>/dev/null
if ln -sf "$here/bin/pesforge2015" "$LAUNCHER2015" 2>/dev/null; then :; else
  cp "$here/bin/pesforge2015" "$LAUNCHER2015"
fi
chmod +x "$LAUNCHER2015" 2>/dev/null

chmod +x "$here/bin/pesforge-gtk4" 2>/dev/null
if ln -sf "$here/bin/pesforge-gtk4" "$LAUNCHER_GTK4" 2>/dev/null; then :; else
  cp "$here/bin/pesforge-gtk4" "$LAUNCHER_GTK4"
fi
chmod +x "$LAUNCHER_GTK4" 2>/dev/null

# The window icon comes from assets/pesforge.png; the menu entry needs it in the
# icon theme so `Icon=pesforge` resolves.
ICONBASE="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
for s in 16 32 48 64 128 256 512; do
  mkdir -p "$ICONBASE/${s}x${s}/apps"
  cp "$here/assets/pesforge.png" "$ICONBASE/${s}x${s}/apps/pesforge.png"
done

cat > "$DESKTOP_GTK4" <<EOF
[Desktop Entry]
Type=Application
Name=PESForge
GenericName=Player editor
Comment=Edit players and clubs in five PES releases
Exec=$LAUNCHER_GTK4
TryExec=$LAUNCHER_GTK4
Icon=pesforge
StartupWMClass=dev.pesforge.gtk4
Terminal=false
Categories=Game;
Keywords=pes;pesdb;football;pes2021;pes2019;pes2015;pes5;pes6;editor;
StartupNotify=true
EOF
chmod 644 "$DESKTOP_GTK4"

command -v desktop-file-validate >/dev/null 2>&1 && desktop-file-validate "$DESKTOP_GTK4"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null

echo "installed:"
echo "  PES 2015 : $LAUNCHER2015"
echo "  app      : $LAUNCHER_GTK4"
echo "  menu     : $DESKTOP_GTK4"
echo
if ! "$LAUNCHER_GTK4" --check; then
  echo
  echo "PESForge is installed; start it once and pick your game folder when asked."
fi
echo
echo "Start it from your app menu (\"PESForge\") or by running: pesforge-gtk4"
