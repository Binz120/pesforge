# PESForge — architecture

Read this before changing anything. It describes the tree as it stands after the first
hardening pass: the package the app lives in, the contracts that must survive a refactor,
and the data layer the two GUIs and the CLIs share.

## What the app is

A desktop editor for Pro Evolution Soccer player databases. You point it at a CPK container
or a folder; it identifies the release **from the bytes** (record size of the `Player`
table: 312 = PES 2021 / SP Football Life, 188 = PES 2019, 180 = PES 2015), loads the roster,
and edits abilities, BIO fields, skills, playing styles, positions and names. Ticking rows
allows one batch change across them. Save writes with a whole-file backup first and a
read-back check after.

Run it with `./bin/pesforge-gtk4`, `python3 editor_gtk.py`, or `dist/pesforge-gtk4` (the
zipapp). The older Tk build (`editor.py`) still exists in the tree; it is a separate app.

## The app: `editor_gtk.py` + `pesforge_gtk/`

`editor_gtk.py` is the entry point the launcher, the zipapp and the tests drive (they run it
as a script). It is a shim: it puts the package on the path, re-exports the names that used
to be defined there, and calls `pesforge_gtk.app.main()`. Everything else lives in
`pesforge_gtk/`:

| Module | Lines | Responsibility |
|---|---|---|
| `importing.py` | 276 | Identify a pick (CPK or folder) and the release behind it: the vintage table, the byte-based probes, candidate ranking, folder resolution, `--db`. No widgets. |
| `styling.py` | 102 | `style.css` (next to it) rendered from the theme tokens via `importlib.resources`; the light/dark choice; one `CssProvider` reloaded in place. `TOK` is a dict updated in place, so importers always see the current palette. |
| `widgets.py` | 195 | Small controls: fixed-width labels, accessible names, slim spins, eliding dropdowns, step/scroll behaviour, the Cairo ability bar, the widget-tree walker. |
| `reference.py` | 144 | Shared vocabulary and derived numbers: ability groups, the OVR model through each vintage's labels, countries, bulk field lists, field ranges. |
| `listpane.py` | 354 | The player list: `Row` model, `ColumnView` cells, the single-selection cursor, the tick set, filter/sort plumbing, the column width policy. |
| `formpane.py` | 319 | The editor panes: player card, ability grid, BIO grid, chips and their filter. |
| `batchbar.py` | 108 | The batch tray: one operation across the ticked rows, clamped and staged in memory. |
| `editing.py` | 341 | Form ↔ record: loading a player, pending edits (`originals`/`dirty`), undo/redo, every field handler. |
| `saving.py` | 168 | The write flow: per-copy read + apply, backup, patch, read-back, prune; revert; the modal alert and close guard. |
| `layout.py` | 99 | The pane arrangement decision (`_size_panes`, the form-overflow probe, the rebuild hook). |
| `window.py` | 485 | The shell: `__init__`, header, empty state, footer, settings menu, shortcuts, import/unload/load. Assembles `PesForgeGtk` from the mixins. |
| `selftest.py` | 558 | The self-check modes — imported **only** when `PESFORGE_GTK_SELFTEST` is set (the window's `selftest` dispatcher imports it on demand). |
| `app.py` | 86 | `main()` / `check()`: arguments, environment, `Gtk.Application`. |

The window class is assembled once, in `window.py`:

```python
class PesForgeGtk(BatchBarMixin, EditingMixin, FormPaneMixin, LayoutMixin, ListPaneMixin,
                  SavingMixin, Gtk.ApplicationWindow):
```

Each mixin is one responsibility above; they call each other through `self` as before. Two
traps that cost real time in this split:

* **A `do_*` vfunc must be defined on the class, not inherited from a mixin.** PyGObject
  installs vfunc overrides from the class itself; a mixin copy is silently never called, and
  the layout then freezes at whatever GTK allocated first (measured: a 46px list pane, no
  arrangement at all). `do_size_allocate` is therefore in `window.py` and delegates to
  `layout.LayoutMixin._size_panes`.
* **Every `self.X` becomes `X(win)` when code moves out of the class** — the self-checks are
  module functions now, and a missed rewrite is a `NameError` inside a GLib callback, which
  prints to a console nobody reads and leaves the app hanging instead of failing. The
  `PESFORGE_GTK_SELFTEST=empty` smoke (~15s) catches this class of breakage before the suite.

## Data layer (no toolkit in it)

| Module | Lines | Responsibility |
|---|---|---|
| `cpk.py` | 240 | CRI CPK container: header `@UTF`, TOC lookup (`TOC ` magic + `@UTF`, or an obfuscated `@UTF` for the 2015-2017 vintages), the `Utf` parser/patcher, `set_row_int`. |
| `pesdb.py` | 772 | PES 2021 / SP Football Life: the 312-byte record map, bit fields, WESYS pack/parse, the table classes, `patch_table` (write-into-slot or append+repoint), countries, the per-position OVR model. |
| `pes2019.py` | 849 | PES 2019: 188-byte records, `Cpk2019`, per-stem table families, `DpFileList` mounting, per-file backup/restore. |
| `pes2015.py` | 729 | PES 2015: 180-byte records, obfuscated TOC, its own patch path and backups. |
| `pes2021.py` | 297 | Adapter that makes `pesdb` speak the shared panel vocabulary the 2019/2015 modules established. |
| `pesdb_maps.py` | 91 | Vendor-derived ability/skill/style lists; `read_data` serves `data/*.json` from the tree *and* from inside the zipapp. |
| `settings.py` | 222 | Config, env overrides, backups dir + prune, install discovery, EDIT-save detection. |
| `theme.py` | 213 | Palettes and `tokens()` with contrast floors; contrast maths. |
| `editor.py` | 5260 | The older Tk build (separate app). |
| `cli.py` | 524 | Headless PES 2021 verbs. |
| `cli2015.py` | 672 | Headless PES 2015 verbs. |

## Write-safety contract (do not weaken; changes need tests and a call-out)

1. **Backup before every save.** Every vintage's `patch` copies the whole CPK into
   `<state>/backups` with a timestamp plus a `.json` sidecar before any byte is written.
   `prune_backups` keeps the newest `max_backups` (0 = keep everything).
2. **Only intended bytes move.** `pesdb.patch_table` writes the re-packed table into its own
   slot (padded, so no other file moves) or appends it 2 KiB-aligned and repoints only that
   TOC row; `slots.json` remembers the largest slot ever seen.
3. **Read-back verification.** After the patch, the app re-reads the table from disk and
   checks the edited fields; only then does the save count as written.
4. **A failed write leaves the original in place.** Copies are read and edits applied in
   memory first; unwritable copies are skipped and reported; a failed save keeps the edits.
5. **Release detection is by content** (`probe_database`), never by filename.
6. **The game's own files are read-only in tests.** Every write test copies to a scratch
   install; the selftests hash the source CPK before and after and fail if it moved.

## Testing

`bin/check` runs exactly what CI runs: the data layer, the CLIs, the Tk editor and
the app (through `PESFORGE_GTK_SELFTEST` subprocesses), plus synthetic-fixture tests
that need no game data (`tests/fixtures.py` builds minimal faithful CPKs for all three CPK
releases plus the classic option files (PES 5, PES 6), a table-less stub and a mislabeled
file, and re-reads written containers with an
independent parser). Details, the display trap and the selftest modes: `docs/testing.md`.

## Build

`bin/build-executable` stages the runtime modules, the `pesforge_gtk/` package (stylesheet
included) and `data/`, then runs `python3 -m zipapp` with a `/usr/bin/python3` shebang →
`dist/pesforge-gtk4`. A zipapp cannot `open()` its own files, which is why `pesdb_maps.read_data`
serves `data/*.json` from the archive and `styling.css()` reads the stylesheet through
`importlib.resources`.
