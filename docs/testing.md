# Testing notes

Everything here is the state of the harness at the end of the hardening pass. The short
version: `bin/check` runs exactly what CI runs; on this machine that means the whole suite
on a headless X display, and it must be run **with the Wayland backend disabled** or GTK
windows land on the user's live desktop.

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
was exported for the run, so every GTK check drew its window on the user's live session,
and the compositor tiled the window to ~917x1030 — the fit check measured that tile and
reported a failure that had nothing to do with the code under test. Verified with a probe:
`env -u WAYLAND_DISPLAY GDK_BACKEND=x11 DISPLAY=:99` is the correct incantation;
`Gdk.Display.get_default().get_name()` must read `:99`.

Consequence for the fit gate: it asserts against the size the harness *asked for*. Under a
WM-less Xvfb the window maps at exactly `PESFORGE_GTK_SIZE`. Under a real compositor the
tile size is what matters; on this machine the user's tile (917x1030) is 13px short of the
side-by-side form pane, which is a real layout finding for Phase 4, not a test artifact.

## What the suite covers

- 91 original tests: data layer, CLIs (each on its own sandbox copy of the real CPK),
  the Tk editor through real windows, and the GTK4 app through `PESFORGE_GTK_SELFTEST`.
- Synthetic fixture tests (no game data, so CI can gate on them): container validity via an
  independent TOC/WESYS reader, deterministic builds, no-edit save byte-identity, single
  field edit touching only its record and byte span, batch edit touching only its records,
  release detection for the three releases plus a mislabeled file and a table-less stub,
  and the data layer's freedom from `gi`/`tkinter`.

Tests that need a real install raise `FileNotFoundError`/`RuntimeError` and the runner
counts them as skipped; read the skip lines, not just the "N/N passed" tail.

## Self-check modes (GTK4)

`PESFORGE_GTK_SELFTEST=` `1` | `empty` | `cycle` | `select` | `hub` | `write` | `write2015` |
`write2021` | `fit` | `theme`, plus `PESFORGE_GTK_SIZE=WxH` and `--db <cpk|folder>`.
`fit` is the layout gate: it fails when either editor pane's content exceeds its scroller.
The write modes never touch the game: they copy to a scratch install, hash the source CPK
before and after, and fail if it moved.

## CI

`.github/workflows/tests.yml` installs `python3-gi`, `gir1.2-gtk-4.0` and `xvfb`, then runs
`bin/check`. On a machine without the game, the install-dependent tests skip and the fixture
tests carry the container coverage.
