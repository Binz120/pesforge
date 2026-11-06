# The Tk build and the GTK4 build, side by side

Written when the Tk build was tagged `tk-final` and moved to `legacy/` (Phase 6 of the
hardening pass). The Tk build is unmaintained from that tag on; this is what it still does
that the GTK4 build does not, and what the GTK4 build added.

## Nothing has to move

Checked before the move: neither the CLIs (`cli.py`, `cli2015.py`), nor the data layer
(`pesdb`, `pes2021`, `pes2019`, `pes2015`, `cpk`, `pesdb_maps`, `settings`, `theme`), nor any
module of `pesforge_gtk/` imports `editor`. The Tk build is a leaf: `editor.py` imports the
data layer, nothing imports `editor.py`. Moving it to `legacy/` needed no code moved into the
data layer — only the tests' import path (they drive the Tk build through real windows) and
the launcher.

## In the Tk build only

Read off its menus at the tag; the CLIs cover two of them.

| Tk feature | Where the capability lives | Note |
|---|---|---|
| Export roster CSV… / Import roster CSV… | `cli.py` (`export`/`import` verbs) | the GTK4 app has no button for it; the CLIs do it headlessly |
| Compare selected players… | nowhere else | a side-by-side two-player comparison dialog |
| Coaches… | data layer only (`pesdb.CoachTable`) | the GTK4 app shows players; coaches are unexposed |
| Restore vanilla | data layer only (`backup_list`, `restore_backup`) | rolling a CPK back from its backups; the GTK4 app's Revert only drops pending edits, it does not restore a backup |
| Open backups folder | — | opens the backups directory in the file manager |
| Reload database | — | the GTK4 app's Unload + Import covers the same ground |
| Separate PES 2015 / 2019 / 2021 database… pickers | — | the GTK4 import hub takes *any* pick and probes it for the release, so one button replaces three |
| About / Theme menu | — | the GTK4 app follows the desktop's light/dark setting and pins a theme in Settings |

## In the GTK4 build only

- **The import hub**: shows what a pick holds (release, roster size, which CPKs it would
  write) before the window changes, and refuses a pick with no player table by pointing at
  the file that has one instead of loading the stub.
- **The ability chips and their filter**: skills and playing styles as searchable toggles
  beside the BIO grid.
- **The batch tray** with a range that follows the field (a spin clamped to the field's own
  limits through every vintage).
- **The list's column policy**: the pane shows the columns it can afford and never squeezes
  one blank.
- **A layout that is a function of size**, measured (see `docs/testing.md`).
- **Accessibility**: names for every control and list column, focus rings, accelerators,
  text that follows the desktop's text scale (`docs/accessibility.md`).
- **The write flow's change report** written next to each backup.

## What deleting the Tk build would remove

`legacy/editor.py`, its tests (they skip cleanly once it is gone — the suite puts `legacy/`
on `sys.path` and installs a stub that raises `FileNotFoundError`, which the runner counts as
a skip), the compare-players dialog, the coaches editor and the "restore vanilla" action.
The commands, for the owner to decide:

```sh
git rm -r legacy/ bin/pesforge
git commit -m "remove the unmaintained Tk build"
```
