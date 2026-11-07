# Testing notes

Everything here is the state of the harness at the end of the hardening pass. The short
version: `bin/check` runs exactly what CI runs; on this machine that means the whole suite
on a headless X display, and it must be run **with the Wayland backend disabled** or the app's
window lands on the user's live desktop.

## Running

```sh
bin/check                      # what CI runs: the full suite on a display
python3 tests/run_all.py       # the suite as it is; skips the GUI checks without a display
python3 tests/fixtures.py      # build + re-validate the synthetic fixtures standalone
```

`bin/check` picks the display: an existing `DISPLAY`, else `xvfb-run`, else it runs the
suite without the GUI checks and says so. It always strips `WAYLAND_DISPLAY` and sets
`GDK_BACKEND=x11`, because a GTK4 process prefers Wayland when both are set — see below.

## The display trap (found during the hardening pass)

A Hermes/CI shell on this machine inherits `WAYLAND_DISPLAY=wayland-1` as well as a
`DISPLAY`. `Gdk.Display.get_default()` then returns **wayland-1** even when `DISPLAY=:99`
was exported for the run, so every check drew its window on the user's live session,
and the compositor tiled the window to ~917x1030 — the fit check measured that tile and
reported a failure that had nothing to do with the code under test. Verified with a probe:
`env -u WAYLAND_DISPLAY GDK_BACKEND=x11 DISPLAY=:99` is the correct incantation;
`Gdk.Display.get_default().get_name()` must read `:99`.

Consequence for the fit gate: it asserts against the size the harness *asked for*. Under a
WM-less Xvfb the window maps at exactly `PESFORGE_GTK_SIZE`. Under a real compositor the
tile size is what matters; on this machine the user's tile is 917x1030, and the layout
policy was changed in Phase 4 to hold there too (see below).

## Layout: how the panes are decided

`PESFORGE_GTK_SELFTEST=fit` samples at 1.2 s, after the layout has settled — a pane measured
on the first frame can be a few pixels short of what it needs a second later, which is how
the old 0.7 s gate passed while the bio pane was scrolling. It asserts both editor panes fit
their scroller at 941x1030, 917x1030 and 1504x1030.

`PESFORGE_GTK_SELFTEST=sweep` (run at `PESFORGE_GTK_SIZE=700x1030`) resizes the live window up
across the split layout's boundary — 700, 880, 960, 1100, 1400 — and then asks the policy
again for each size on the way back. (It *asks* rather than performs because a window with no
window manager will not shrink; the test below performs the real thing.) It fails on a size deciding differently the second time,
a list narrower than the columns it shows, a squeezed list column (which is what a blank
column looks like), or an editor pane scrolling while the split layout is in use. The way
back is *asked* rather than performed because a window without a window manager will not
shrink; the decision is what the sweep compares, and the geometry follows it.

The decision itself (`pesforge_gtk/layout.py`) is a function of (width, height, the measured
minima): the list's width comes from its own column policy, the editor's minimum from
`measure(...)[0]` on the panes' content, and the vertical signal from the panes' scroll
adjustments. The boundary it produces on this machine is 914px — the old hard-coded 910 was a
fair guess, and is now gone. Two measured figures are remembered (`_editor_min_w`, and the
height band `_tight_below` at which the split layout was last seen costing a scrollbar); both
are used on both crossings, so the layout cannot chatter or become a one-way door.

`test_gtk4_resizes_under_a_window_manager` performs the down-and-back-up pass for real: it
starts its own `Xvfb :100` and `xfwm4 --compositor=off` (both installed here; the test skips
where they are not), launches the app with `PESFORGE_GTK_SELFTEST=watch` — a mode that prints its
geometry every 400 ms and asserts nothing itself — and drives `xdotool windowsize` through
1400 → 900 → 1400 → 917 → 941 wide at 1030 tall. It fails when a size seen twice decided
differently, when an editor pane scrolls while the split layout is in use, or when 917 is not
split. Measured: 1400 and its second visit print the identical line (split, form/bio 934x464, no
scroll), 900 stacks (the panes scroll, as designed), 917 and 941 are split with no scroll.

## What the suite covers

- 91 original tests: data layer, CLIs (each on its own sandbox copy of the real CPK),
  the app through `PESFORGE_GTK_SELFTEST`.
- Synthetic fixture tests (no game data, so CI can gate on them): container validity via an
  independent TOC/WESYS reader, deterministic builds, no-edit save byte-identity, single
  field edit touching only its record and byte span, batch edit touching only its records,
  release detection for the CPK releases plus a mislabeled file and a table-less stub,
  and the data layer's freedom from `gi`.
- PES 5 (the classic-era option file): the same round-trip rules against a synthetic
  `KONAMI-WIN32PES5OPT` written through `pes5.save()` (signature, checksums, no-edit
  byte-identity, one-record confinement, restore), plus the shipped/live file decoded
  against known cards and the app's own `write5` save check when one is installed.

Tests that need a real install raise `FileNotFoundError`/`RuntimeError` and the runner
counts them as skipped; read the skip lines, not just the "N/N passed" tail.

## Self-check modes

`PESFORGE_GTK_SELFTEST=` `1` | `empty` | `cycle` | `select` | `hub` | `write` | `write5` |
`write2015` | `write2021` | `fit` | `sweep` | `a11y` | `scale` | `theme`, plus
`PESFORGE_GTK_SIZE=WxH`
and `--db <cpk|folder>`.
`fit` is the layout gate: it fails when either editor pane's content exceeds its scroller.
`sweep` is the geometry-and-history gate: see the layout section above.
The write modes never touch the game: they copy to a scratch install, hash the source CPK
before and after, and fail if it moved. `write` (the PES 2019 bulk matrix) goes further and
*refuses* to run at all unless the file it would write sits under the temporary directory —
it is the one mode that is pointed at a file by the caller rather than building its own copy,
and a real install is not its to touch (this is what the sandbox in
`test_gtk4_bulk_matrix_stages_and_commits` provides).

## CI

`.github/workflows/tests.yml` installs `python3-gi`, `gir1.2-gtk-4.0` and `xvfb`, then runs
`bin/check`. On a machine without the game, the install-dependent tests skip and the fixture
tests carry the container coverage.
