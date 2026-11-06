# Hardening pass — report

Branch `gtk4-hardening`, one concern per commit, clean tree, **101/101 tests green**. The Tk
build's last state is tagged `tk-final`; the state the pass started from is tagged
`gtk4-pre-hardening` with a bundle at `../pesforge-gtk4-pre-hardening.bundle` (7 refs).

Nothing has been pushed anywhere. The command, when you want it:

```sh
git push origin gtk4-hardening        # or: git switch gtk4 && git merge gtk4-hardening
```

## Before and after

| | before | after |
|---|---|---|
| tests | 91 | **101** (10 new: seven fixtures, layout sweep, a11y, text scale) |
| `editor_gtk.py` | 3,196 lines | 70 (a shim: package on the path, names re-exported, `main()`) |
| largest GTK module | 3,196 (one file) | 793 (`selftest.py`, imported only when asked) — nothing over 800 |
| GTK4 app total | 3,196 + data layer | 3,814 lines across 16 modules + 119 lines of CSS |
| stylesheet | a Python f-string | `pesforge_gtk/style.css` as a package resource (rendered output byte-identical) |
| load, 26,395 players | 0.90 s (0.61 rows, 0.15 list) | **0.93 s** (0.64 rows, 0.14 list) |
| zipapp | 335,666 bytes | **367,867 bytes** (+9.6 %; the package, the stylesheet and the self-checks are inside it) |
| layout decision | `width < 910 or height < 950` | measured: the content's `measure()` minimum (588 px) and the panes' scroll adjustments — boundary 914 px |
| 917×1030 (a tiled window here) | form pane 13 px short, stacked | split layout, both panes fit |
| text scaling | ignored (`px` sizes stayed put: 22 px at every scale) | followed (name line 24/30/36/48 px at 1.0/1.25/1.5/2.0) |
| accessible names | list columns and several dropdowns unnamed | every app-built control named; a gate that fails on one |
| save | backup + read-back | backup + read-back + a human-readable change report |

Both budgets the brief set are met: load time +3 % (limit 20 %), zipapp +9.6 % (limit 20 %).

## Real game data — hashes

Nine files (the three installs' database CPKs, including the 810 MB and 1.5 GB 2019 packs)
were hashed at the start of the pass and again at the end. **All nine are byte-identical.**

| file | SHA-256 (start and end) |
|---|---|
| SPFL 2027 `download/data_s2627.cpk` | `5af8d528b7238dd0d38f1542ae608ac6149ade3839d70c356e3ed1a92bbc8fb5` |
| PES 2019 `Data/dt10_x64.cpk` | `5a82ab5d3093f008bf757b4a40fa16691e114189bf0cf9bf250222824e7cf0d2` |
| PES 2019 `download/dt80_100E_x64.cpk` | `0c09a8b40ec0d75e2c247e9c8a042fe3cdeba3abfd8a18218b0e6fbea9ad8a1f` |
| PES 2019 `download/dt80_101E_x64.cpk` | `a11cab8e85259b1a1a7aec6754060b727a564c78e60af55f1ce688d99a10051c` |
| PES 2019 `download/dt80_102E_x64.cpk` | `d00ca15f4d847ab6d0d40b73019dd3991c266652e68e9698a57b5f7b83d4b2ca` |
| PES 2019 `download/dt80_103E_x64.cpk` | `87126c51daf91d2d7ae9f50471078c707efa14fe03477930277dd7f9f662ff5d` |
| PES 2019 `download/dt80_200E_x64.cpk` | `cfeb6738a59aaa785d11bd24e4d5eb130fa9cc2ad6cfad63ed4cff0088615597` |
| PES 2015 `Data/dt10.cpk` | `6589c26d881a3a71cc5d7a368c26ce1b9919ab35caeb9112e7355d852181e1a0` |
| PES 2015 `download/dt80_100E.cpk` | `04f80f9d5f619b4e02ae11fd58c4c055ea976a1093b1879343b36def53764794` |

**One incident, reported in full.** Mid-pass I ran the `write` self-check by hand with
`PES2019_DIR` pointed at the real install (the mode's own docstring said to aim it at a copy;
I aimed it at SPFL, and it auto-detected the 2019 install). It wrote to two real 2019 CPKs:
three players' Speed +5 (and a +7 on one). The check had taken its region backups moments
before, both files were restored from them, and both re-hashed to the values above. The check
now **refuses to run at all** unless the file it would write sits under the temporary
directory, so the mistake cannot be repeated. Nothing else in the pass wrote outside a copy.

## The phases

**Phase 0 — protect the work.** `.gitignore` covers `dist/`, caches, `backups/`, `captures/`,
`*.cpk`, `*.bin` and top-level screenshots; the uncommitted GTK4 body of work was committed as
it stood, tagged `gtk4-pre-hardening`, bundled (7 refs, verified) to
`../pesforge-gtk4-pre-hardening.bundle`, and `gtk4-hardening` branched from it.

**Phase 1 — baseline.** Every module and the suite were read; `docs/ARCHITECTURE.md` written.
Baseline numbers above. Screenshots were captured at 941×1030, 917×1030 and 1504×1030
(outside git, in the scratch dir).

**Phase 2 — fixtures and CI.** `tests/fixtures.py` (440 lines) builds minimal faithful CPKs
from the three record maps, including the older vintages' XOR-obfuscated TOC and the
whole-file (2021) vs payload-at-+2048 (2015/2019) conventions: a seeded roster per release, a
table-less stub, a file whose name claims one release while its bytes are another, and a
folder. The tests re-read written containers with an **independent** TOC/WESYS parser and
assert: no-edit save is byte-identical, a single-field edit changes only that field's byte
range, a batch edit changes only those records. Import purity (no `gi`/`tkinter` in the data
layer) is a test. `bin/check` — which is exactly what CI runs — covers the lot.

**Phase 3 — the split.** `editor_gtk.py` became a shim over `pesforge_gtk/`: `importing`,
`styling`, `widgets`, `reference`, `listpane`, `formpane`, `batchbar`, `editing`, `saving`,
`layout`, `window`, `selftest`, `app`. One extraction per commit, verbatim moves (an AST-based
extractor, not hand-editing), the suite green after each. The CSS moved to `style.css` and is
read through `importlib.resources`, proven byte-identical against the rendered f-string and
proven inside the zipapp. Two things this pass found the hard way:

* **PyGObject does not install a `do_*` vfunc inherited from a mixin.** `do_size_allocate` was
  silently never called after the layout moved out, and the window froze at its first
  allocation (a 46 px list pane). It lives on the class in `window.py` and delegates.
* A missing name inside a GLib callback is a `NameError` in a console nobody reads: the app
  hangs instead of failing. An AST scan for unbound globals plus a 15-second smoke check now
  run after every extraction.

**Phase 4 — layout as a function of size.** The decision is now measured, with no guessed
thresholds: the list's width from its own column policy, the editor's minimum from
`measure(...)[0]` on the panes' *content* (the scrollers' `min_content_width(0)` hides it),
the vertical signal from the panes' own scroll adjustments with an explicit, debounced band.
Two measured figures are remembered and used on both crossings, so the layout cannot chatter
or become a one-way door. New `PESFORGE_GTK_SELFTEST=sweep` resizes a live window up across
the boundary (700→880→960→1100→1400) and re-asks the policy at each size on the way back.

Two real bugs fell out of it, both fixed: the fit gate had been sampling 0.7 s in, while the
panes settle a few pixels taller — at 941×1030 the bio pane was 43 px over its page and the
gate called it a pass; and at 917×1030 (the size a window gets tiled to on this desktop) the
form pane was 13 px short, so the app fell back to the stacked layout. The bio pane fits now
(7 chip columns instead of 6 in the compact shape, 1 px less spacing), the form's grid is
584 px (labels 110 px, 4 px column gap), and the gate samples at settle. 917, 941 and
1504 all show the whole editor with no scrollbar at scale 1.0.

**Phase 5 — text scale and accessibility.** Measured first: GTK resolves a stylesheet's `px`
font sizes against the *screen*, not the desktop's text-scaling factor — an unstyled label
doubled (18→36 px) while the app's own name line stayed at 22 px. So all fifteen `font-size`
declarations are `em` now, and the fixed pixel widths (column widths, label widths, the list
pane's thresholds) go through `widgets.scaled()`. `PESFORGE_GTK_SELFTEST=scale` walks the tree
at the running scale, failing on anything drawn outside its parent or on top of a sibling; it
caught the list's fixed column widths clipping their headers at 1.5 and 2.0. Measured now:
name line 24/30/36/48 px at 1.0/1.25/1.5/2.0, nothing clipped.

Accessibility: every app-built control has an accessible name (the six list columns carry
names and tooltips on their headers — PyGObject does not expose `GtkColumnViewColumn`, and the
header is what a screen reader lands on), the position/style/foot and batch-tray controls were
unnamed and now are not, `Ctrl+B` applies the batch change, and
`PESFORGE_GTK_SELFTEST=a11y` walks the tree and fails on an unnamed control or a squeezed
cell. `docs/accessibility.md` has the Orca walkthrough and says plainly what was not verified.

**Phase 6 — the Tk build.** Tagged `tk-final`, moved to `legacy/`, marked unmaintained,
launcher and installer updated, out of the zipapp. Nothing had to move into the data layer:
nothing in the CLIs, the data layer or the GTK4 app imports `editor`, and `editor` imports the
data layer — it is a leaf. `docs/tk-vs-gtk.md` lists what only the Tk build has (compare
players, coaches, restore vanilla, CSV import/export via the CLIs, opening the backups folder)
and what the GTK4 build added. **Deleting it is your call**; the commands are in that file.

**Phase 7 — verification and the in-game side.** The GUI checks and the sweep run headless on
Xvfb under `bin/check` (exactly what CI runs, on every push and pull request). Each save now
writes `changes-<stamp>.txt` next to its backup — pid, field, old → new, the release and the
CPKs written — and the status line names it. `docs/in-game-checklist.md` says what to open in
game, in what order, and what to compare against that report.

**Phase 8 — packaging.** The zipapp now fails with a clear message and install hints for
Debian/Ubuntu, Fedora and Arch when `gi` or GTK 4 is missing (verified by simulating both),
and it was rebuilt and run from a clean directory. Minimum Python: no 3.14-only syntax or
stdlib use was found by inspection; it is *tested* on 3.14.9 only, so the safe claim is
"3.14 tested, older untested" — if you need a floor, run `bin/check` on the oldest interpreter
you care about and I will pin it in the README. Flatpak: evaluated, not built — see below.

## What remains unverified

* **The game itself.** Nothing in this pass booted PES. The write path is exercised end-to-end
  through the real code on copies, and the copies' bytes are asserted; what the game *shows*
  is not. `docs/in-game-checklist.md` is the manual step that closes this.
* **A real compositor, on Wayland.** The X11 half is now covered by
  `test_gtk4_resizes_under_a_window_manager`: its own `Xvfb :100` plus `xfwm4 --compositor=off`
  (a window manager honours a shrink, which a bare X server does not) driving `xdotool
  windowsize` through 1400 → 900 → 1400 → 917 → 941 wide. 1400 and its second visit print the
  identical geometry line, 900 stacks, 917 and 941 are split with both panes fitting. What is
  still untested is Wayland specifically: `weston`/`mutter`/`sway` are not installed here, so
  your Hyprland session is the remaining run — `bin/check` plus
  `PESFORGE_GTK_SELFTEST=sweep PESFORGE_GTK_SIZE=700x1030`.
* **Orca.** No AT-SPI bus and no Orca here: the a11y checks prove the names exist and the list
  renders, not how the app sounds. Reading order and pronunciation are untested.
* **Columns, not overlapping siblings, at every size.** The scale check asserts sibling
  overlap; the sweep asserts the list is never squeezed and the panes fit; a full
  pixel-level visual diff is not part of the suite.
* **The 1.5/2.0 text scales in the real app.** They are checked for structure (nothing
  clipped, nothing overlapping, panes scroll) — not for legibility at a glance.
* **24-bit colour, fonts.** Everything assumed this machine's theme and fonts.

## Decisions for the owner

1. **The Tk build is not deleted** (hard rule 7). It is in `legacy/`, unmaintained, and the
   two commands to remove it are in `docs/tk-vs-gtk.md`. I recommend deleting it: it is 5,260
   lines that no other module imports, and its tests are the slowest tests in the suite.
2. **No `Gtk.LayoutManager`, no `Adw.BreakpointBin`.** The brief prefers a LayoutManager unless
   the code already depends on Adw; it does not, and making libadwaita a requirement is a new
   runtime dependency (rule 4). The panes' sizes come from GTK's own box measurement, and the
   arrangement is one measured decision in `pesforge_gtk/layout.py`. Reimplementing the
   measurement in a custom LayoutManager would be a large, risky change to arrange two widgets.
3. **The write check refuses a real install.** New behaviour, from a real mistake: it now
   requires the target to be under the temporary directory. If you ever *want* to point the
   self-checks at a real install, that is a code change, not an env var — deliberate.
4. **`changes-<stamp>.txt` next to the backups.** A new file type in the backups directory; it
   is not pruned with the backups (it is tiny and it is the audit trail). Say the word and it
   can be pruned by the same `max_backups` rule.
5. **Flatpak: not built.** The evaluation: the GNOME runtime (org.gnome.Platform 47+) ships
   GTK 4 and PyGObject, so a `finish-args` manifest would need `--filesystem=home` (or a
   narrower path for the games folder) and the app would run from a writable copy of the
   package — the zipapp path is unchanged by it. What stopped me: no Flatpak runtime, builder
   or `flatpak` binary on this machine, and no network install of the runtime, so a manifest
   would be untested guesswork. The zipapp is the shippable artifact and it works.
6. **Text scaling is followed, not overridden.** The app's own px sizes are `em`; if you would
   rather the editor keep its own sizes regardless of the desktop setting, that is one CSS
   change — but then the desktop's "large text" stops working, which is what the pill of this
   brief asked for.
7. **Where the code contradicted the brief** (as instructed, the code won):
   * The brief's "the data layer has no toolkit in it" checks out: `cpk`, `pesdb`, `pesdb_maps`,
     `pes2015`, `pes2019`, `pes2021`, `settings` and `theme` import stdlib and each other only
     (`pes2021` imports `pes2019` and `settings`; none imports `gi` or `tkinter`), and a test
     asserts it. `tests/run_all.py` itself imports `gi` at module level, which is why the
     purity test scans the data modules rather than the test file.
   * The brief's four text scales assume scrolling is acceptable at 1.25 and above. Measured:
     at 2.0 the chrome leaves a 214 px editor page, so the panes scroll a lot. If you want
     1.5-2.0 to be *comfortable* rather than merely unclipped, the compact layout needs a
     further look at the section bands and the chips grid.
   * `docs/ARCHITECTURE.md` (written in Phase 1) claimed the panes fit 941×1030 exactly; they
     did not once the frame settled. That is fixed and the doc now says where the numbers come
     from.
