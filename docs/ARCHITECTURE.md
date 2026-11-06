# PESForge GTK4 — architecture

Read this before changing anything. It describes the tree as found at the start of the
hardening pass (tag `gtk4-pre-hardening`), module by module, and the contracts that must
survive any refactor.

## What the app is

A desktop editor for Pro Evolution Soccer player databases. You point it at a CPK container
or a folder; it identifies the release **from the bytes** (record size of the `Player`
table: 312 = PES 2021 / SP Football Life, 188 = PES 2019, 180 = PES 2015), loads the roster,
and edits abilities, BIO fields, skills, playing styles, positions and names. Ticking rows
allows one batch change across them. Save writes with a whole-file backup first and a
read-back check after.

Two GUIs share the data layer: the older Tk build (`editor.py`) and this GTK4 build
(`editor_gtk.py`). The CLIs (`cli.py`, `cli2015.py`) drive the same modules.

## Modules

| Module | Lines | Responsibility |
|---|---|---|
| `cpk.py` | 240 | CRI CPK container: header `@UTF` at 0x10, TOC lookup (`TOC ` magic + `@UTF`, or an encrypted `@UTF` for the PES 2015-2017 vintage), the `Utf` table parser/patcher, CRILAYLA decompression, `set_row_int` for in-place TOC edits. No toolkit, no project imports. |
| `pesdb.py` | 772 | PES 2021 / SP Football Life data: the 312-byte record map (`ATTRS`, `PHYSICAL`, `CAREER`, `POSITIONS`, `SKILLS`, `MOTIONS`, `APPEARANCE`), bit-field get/set, WESYS pack/parse (`table_blob`, `table_rows`, `wesys_pack`, `_find_block`, `_table_block`), table classes (`LeadTable` → `PlayerTable`, `TeamTable`, `CoachTable`, `AssignmentTable`, `AppearanceTable`), `patch_table` (write-into-slot or append+repoint), country names, the per-position OVR model. |
| `pes2019.py` | 849 | PES 2019: 188-byte records, its own `Cpk2019` (payload at `FileOffset + 0x800`), the per-stem table families (`Player`, `Player1`..`Player6`, same for Team/Coach/Assignment), `DpFileList.bin` mounting (`mounted_packs`), per-file backup/restore. |
| `pes2015.py` | 729 | PES 2015: 180-byte records, encrypted `@UTF` TOC via `cpk`, its own patch path and backup/restore. |
| `pes2021.py` | 297 | Adapter that makes `pesdb` speak the shared panel vocabulary the 2019/2015 modules established: aliased `FIELD_BY_LABEL` (`Height`→`Height (cm)`, `Position`→`Registered Position`, `Skill: X` keys), `Cpk`/`PlayerTable`/`TeamTable`/`AssignmentTable` wrappers taking `(blob, prefix)`, `NAME_SLOTS`, `STYLE_NAMES`, `backup_list`/`restore_backup`, `find_game_dir`, `database_cpks`. |
| `pesdb_maps.py` | 91 | Vendor-derived ability/skill/playing-style lists from `data/pesdb_maps.json`; `read_data(name)` serves `data/*.json` both from the source tree and from inside the zipapp. |
| `settings.py` | 222 | Config (`~/.config/pesforge/config.json`, env overrides `SPFL_DIR`/`SPFL_STATE`/`SPFL_CONFIG`), backups directory + `prune_backups`, install auto-discovery, EDIT-save detection. |
| `theme.py` | 213 | Palettes (`amber`, `crimson`, `ice`, `pitch`, plus a light base) and `tokens()`: every colour the UI draws, each derived token blended to hold a contrast floor. Contrast maths: `contrast`, `toward`, `away`, `ink_on`. |
| `editor.py` | 5260 | The older Tk build. A separate app; not part of the GTK4 build's runtime. |
| `editor_gtk.py` | 3196 | The GTK4 app (details below). |
| `cli.py` | 524 | Headless PES 2021 verbs: export/import CSV, transfers, coach/stadium/squad edits, dry-run + save, backups. |
| `cli2015.py` | 672 | Headless PES 2015 verbs, same shape. |
| `tests/run_all.py` | 2508 | 91 assert-based tests, autodiscovered by `test_*` name. Real-data tests run against the installed game; they skip (FileNotFoundError/RuntimeError) when an install is absent. |

Total 13,065 lines in the 12 runtime modules (the brief's figure; tests extra).

## editor_gtk.py

One module, in build order:

- **Import resolution** (`probe_database`, `cpk_files`, `rank_cpk`, `probe_candidates`,
  `find_database`, `describe_pick`, `describe_database`): identify a pick by reading its
  tables, never the filename; a folder pick walks the folder, its `Data/` and `download/`,
  plus the same pair one level down; a file pick with no table falls back to the file that
  has one. `PROBE_LIMIT` caps how many CPKs are opened.
- **Helpers**: `a11y` (accessible names), `dim_label` (fixed-width field labels),
  `ellipsizing_dropdown`, ability grouping (`ABILITY_GROUPS`, `grouped_ability_order`),
  `overall_rating` (pesdb's OVR model through each vintage's own labels), `select_all_on_focus`,
  `spin_steps`, `field_range`, `bulk_fields`.
- **CSS** (`css()`): the full stylesheet as a Python f-string built from `TOK`
  (theme tokens). Loaded by `install_css(display, mode)` into one `Gtk.CssProvider`
  registered at `Gtk.STYLE_PROVIDER_PRIORITY_USER + 1`. `system_theme()` picks light/dark
  from `PESFORGE_THEME`, then the app's pinned `mode` setting, then the GTK theme name,
  then `org.gnome.desktop.interface color-scheme`, then the freedesktop portal.
- **`Row`**: the list row model (index, pid, name, club, pos, edited).
- **`AbilityBar`**: Cairo-drawn value bar reading colour tokens at draw time.
- **`ImportHub`**: the modal that shows what a pick holds before loading.
- **`PesForgeGtk`** (Gtk.ApplicationWindow): header (brand, Import, Unload, search,
  Settings popover), `Gtk.Stack` of empty page / data page, batch tray, footer
  (Revert, dirty badge, status, Save). The data page is `_build_panes()`:
  list pane (`_list()` — `Gtk.ColumnView` with a tick cell, ID/NAME/CLUB/POS/STATE,
  `SingleSelection` as the current row, `CustomFilter`, `SortListModel`) beside an editor
  column of `_form()` (player card, name entries, ability grid, position) and `_bio()`
  (bio grid, skills/styles chips, chip filter).
  - Selection model: the *current row* drives the form; the bulk pick is `checked`
    (a set of table indexes) painted by the tick cell, so filter/sort can never move a tick.
  - Edits: every write goes through `_set_value`/`table.set`, records `originals`
    (pristine value per (index, key)), pushes one `(index, key, old, new)` step per user
    action onto a bounded undo stack, and marks the row dirty. `_mark_dirty` re-splices the
    row (a mutated `Row` repaints nothing).
  - Save (`_save`): builds visitor jobs for every copy of the table in the install
    (the loaded file first, then the install's other CPKs), applies the pid-keyed edits,
    then per copy: writability check → backup + `patch` → re-read from disk → verify the
    edits read back. Any failure is reported in the status line and a dialog, and the
    pending edits stay. `settings.prune_backups` enforces the cap.
  - Layout: `do_size_allocate` calls `_size_panes(width, height)`. One structure — list
    beside (form+bio) or list as a strip above — turned by orientation, driven by the
    editor's own measured minimum (`_editor.measure`) and `_form_overflows()` (does the
    form's `hadjustment` report overflow). `_col_min` is the list's width policy: which of
    the six columns are affordable at the current pane width.
- **Self-checks** (`selftest` + `_selftest_*`), driven by `PESFORGE_GTK_SELFTEST`:
  `1`/`2`/`write` (build + search + chrome + club repair + write cycle), `empty`, `cycle`,
  `select`, `hub`, `write2015`, `write2021`, `fit`, `theme`. Failure appends to module-level
  `SELFTEST_FAILURES`, which `main()` turns into a non-zero exit code.
- **Entry**: `check()` for `--check`; `main()` parses `--db`, `PESFORGE_GTK_SIZE` (WxH) and
  the selftest mode, and runs the `Gtk.Application` under a per-pid application id when a
  selftest is asked for (so a check never takes over the user's own instance).

## Write-safety contract (do not weaken; changes need tests and a call-out)

1. **Backup before every save.** `pes2021.Cpk.patch` / `pes2019` / `pes2015` copy the whole
   CPK to `<state>/backups` with a timestamp (`%Y%m%d-%H%M%S`) plus a `.json` sidecar naming
   the source, before any byte is written. `prune_backups` keeps the newest `max_backups`
   (0 = keep everything) and never splits a backup from its sidecar.
2. **Only intended bytes move.** `pesdb.patch_table` writes the re-packed table into its own
   slot (padded, so no other file moves) or, when it no longer fits, appends it 2 KiB-aligned
   at the end and repoints only that TOC row. `slots.json` remembers the largest slot ever
   seen so a blank-table write cannot shrink a future save's room.
3. **Read-back verification.** After `patch`, the app re-reads the table from disk and
   asserts the edited fields read back for every edited pid; only then does the save count
   as written.
4. **A failed write leaves the original in place.** Copies are read and edits applied in
   memory first; a copy that cannot be read or is not writable is skipped and reported, and
   `patch_table` only opens the file for writing after the backup exists. Pending edits are
   kept when a save fails.
5. **Release detection is by content.** `probe_database` reads the `Player` table with each
   vintage's reader and matches the record size; filenames are never trusted.
6. **The game's own files are read-only in tests.** Every write test copies to a scratch
   install; the selftests hash the source CPK before and after and fail if it moved.

## Testing

`python3 tests/run_all.py` — 91 tests, plain asserts, autodiscovered by `test_*` globals,
each CLI test on its own sandbox copy of the CPK. Tests that need a game install raise
`FileNotFoundError`/`RuntimeError` to skip; the runner counts them as skipped, not failed
(so a pass line of "91/91" can include skips — read the log). The GTK4 tests drive the real
app via `PESFORGE_GTK_SELFTEST` subprocesses and assert on the printed markers and exit code.

**Display note.** The GTK4 subprocesses use the ambient `DISPLAY`/`WAYLAND_DISPLAY`. On a
Wayland session, `WAYLAND_DISPLAY` wins even when `DISPLAY` is set, so a test run launched
"on Xvfb" can land on the user's live desktop. Force the headless path with
`env -u WAYLAND_DISPLAY GDK_BACKEND=x11 DISPLAY=:99`. Under a WM-less Xvfb the window maps
at its requested size, so the fit check exercises exact sizes; under a real compositor the
tile size is what matters (on this machine ~917x1030, where the form pane is 13px short —
see `docs/testing.md`).

## Build

`bin/build-executable` stages the runtime modules + `data/` + a `__main__.py` into a temp
dir and runs `python3 -m zipapp` with a `/usr/bin/python3` shebang → `dist/pesforge-gtk4`
(~336 KB). A zipapp cannot `open()` its own files, which is why `pesdb_maps.read_data`
serves `data/*.json` from the archive. `bin/pesforge-gtk4` is the dev launcher; it probes
for an interpreter that can import `gi`.
