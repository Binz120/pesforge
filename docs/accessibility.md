# Accessibility

What the app provides, how it is checked, and what has not been checked by a human with a
screen reader. Written during the hardening pass (Phase 5).

## What the app does

**Names.** Every control the app builds gets a name through `pesforge_gtk/widgets.py:a11y()`,
which sets GTK's `Gtk.AccessibleProperty.LABEL`. That covers the form's value fields (each one
named after its label *and* its group, e.g. "Balance, Physical, 40 to 99"), the name entries,
the position/style/foot dropdowns, the batch tray's three controls, the chip toggles and the
chip filter. Widgets that carry their own text (a button with a label, an entry with a
placeholder) do not need one.

**List columns.** The headers are abbreviations on purpose — `✓`, `ID`, `NAME`, `CLUB`, `POS`,
`STATE` — which read as nothing. The app sets accessible names and tooltips on the header
widgets ("include in the batch change", "player id", "player name", "club", "registered
position", "edited state"), and the list itself is named "player list, one row per player".
The name goes on the header widget because PyGObject does not expose `Gtk.ColumnViewColumn` as
an accessible object; the header is what a screen reader lands on, so that is where the name
had to go. A row's own announcement is assembled by GTK from its cells — player id, name, club,
position and edited state, in that order.

**Keyboard.** `Ctrl+S` save, `Ctrl+Z` / `Ctrl+Y` / `Ctrl+Shift+Z` undo and redo, `Ctrl+F` focus
the search box, `Ctrl+B` apply the staged batch change to the ticked players, `Escape` clear
the search. Everything else is reachable with `Tab`, and the focus ring is styled
(`:focus-visible` rules in `style.css`) so it is visible on every control, including list rows.

**Colour is never the only signal.** A ticked row carries a checkmark, not just a tint; a chip
carries a tick when selected; an edited field carries a left border *and* the row's STATE
column says so *and* the footer counts the pending edits; the ability bars show the number
beside the bar.

**Contrast.** `theme.tokens()` builds every colour from a palette and blends each derived
token until it holds the contrast floor. `PESFORGE_GTK_SELFTEST=theme` computes the fg/bg
ratio from the live tokens and fails below 4.5:1; `tests/run_all.py` runs it in light and dark,
and the pinned-mode rule (the app's own theme setting outranks the desktop's declared
preference) is asserted too.

## How to check it

```sh
env -u WAYLAND_DISPLAY GDK_BACKEND=x11 DISPLAY=:99 PESFORGE_GTK_SELFTEST=a11y \
    python3 editor_gtk.py --db "$HOME/Games/SP Football Life 2027"
```

The walk reports `SELFTEST A11Y OK named=<n> controls=<n> columns=<n> rows=<n> cells=<n>` and
fails when an interactive control has no accessible name (its own text and tooltip count), when
the rendered row has a different number of cells than there are visible columns, or when a cell
has no width — a squeezed column is what a blank column looks like. `tests/run_all.py` runs it
at 941x1030 (four visible columns) and 1504x1030 (all six).

The walk stops at GTK's own internals (a spinbutton's stepper buttons, a dropdown's popover and
list, scrollbars): those are GTK's to name and they come with names of their own.

## Manual walkthrough (Orca)

Not run in this pass — see below. Steps to try, in order of what the app is used for:

1. Start Orca, launch the app, Import a database. The window should announce the player count
   and focus should land somewhere sensible on the data page.
2. `Tab` through the header: brand, Import, Unload, search ("search the player list"), Settings.
   Each should say what it is, not "button".
3. `Tab` into the list. Each row should read as id, name, club, position, state. `Ctrl+F`, type
   a few letters, `Escape` to clear.
4. `Tab` into the editor: the player name, then "Abilities" fields — each should announce its
   own label and range, e.g. "Balance, Physical, 40 to 99".
5. `Space` on a row's tick cell, then `Ctrl+B`: the batch change applies to the ticked players
   and the footer should announce the pending count.
6. `Ctrl+S`: the save flow's status text should be readable, and the modal alert (if any) should
   be announced with its buttons.

## Text scaling — measured, then fixed

GTK resolves a stylesheet's `px` font sizes against the *screen*, not against the desktop's
text-scaling factor: measured with `PESFORGE_GTK_SCALE`, an unstyled label doubles from 18 to
36 px between scale 1.0 and 2.0 while the app's own `.hero` line stayed exactly 22 px — the
app's text did not follow the setting at all. So:

* the stylesheet's fifteen `font-size` declarations are now in `em` (the theme's root size is
  10pt = 13.33px, so `11px` is `0.825em`, `12px` is `0.9em`, `18px` is `1.35em`);
* the fixed *pixel* widths — the list's column widths, `dim_label`'s width, the list pane's
  thresholds — go through `widgets.scaled()`, which multiplies by the factor GTK is applying.

`PESFORGE_GTK_SELFTEST=scale` walks the whole tree at the running scale and fails when a widget
is drawn outside its parent or on top of a visible sibling; `tests/run_all.py` runs it at
1.0/1.25/1.5/2.0 and asserts the player-name line grows with each step. Measured:
24/30/36/48 px. Scrolling at the larger scales is expected — the panes scroll, nothing clips.

`PESFORGE_GTK_SCALE=<factor>` is how the checks ask for a scale; it sets `gtk-xft-dpi`, the
same knob GNOME's "large text" setting moves.

## What this pass did not verify

- **Orca itself.** The checks above are a widget-tree walk, not a screen reader session: the
  container has no AT-SPI bus and no Orca, so nothing here proves how the app *sounds*. The
  walk proves the names exist and the list renders; pronunciation, reading order and focus
  announcements are untested.
- **Mnemonics.** GTK 4 dropped Alt-underlined mnemonics; the keyboard route is the accelerators
  above plus `Tab`. There is nothing to verify.
- **Contrast of state colours.** The `theme` check computes the fg/bg pair; the accent, dim and
  hint tokens are blended to hold their floors in `theme.tokens()`, but only that one pair is
  asserted at 4.5:1.
