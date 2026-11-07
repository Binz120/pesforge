"""The self-check modes — imported only when `PESFORGE_GTK_SELFTEST` is set.

Each mode drives the real window and prints one marker line per step; a failure prints
SELFTEST FAIL and the process exits non-zero. Modes: `1` (build, search, chrome, club
repair), `empty`, `cycle`, `select`, `hub`, `write`, `write5`, `write6`, `write2015`, `write2021`,
`fit`, `theme`. The write modes never touch the game: they build a scratch install, edit it,
save, read back, and hash the game's own file before and after to prove it never moved.
"""
import hashlib
import os
import shutil
import tempfile

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

import pes2021  # noqa: E402
import pesdb  # noqa: E402
import settings  # noqa: E402
import theme  # noqa: E402

from .hub import ImportHub  # noqa: E402
from .importing import (  # noqa: E402
    GAME, VINTAGES, default_database, describe_database, describe_pick, install_of,
    vintage_database,
)
from .reference import ability_order, bulk_fields  # noqa: E402
from .styling import TOK, system_theme  # noqa: E402
from .widgets import tree  # noqa: E402

SELFTEST_FAILURES = []        # filled by a failing check, turned into an exit code by main()


def _fail(win, why):
    SELFTEST_FAILURES.append(why)
    print(f"SELFTEST FAIL {why}", flush=True)
    win.dirty.clear()             # a check must not sit on the close guard
    win.close()
    return False


def _selftest_theme(win):
    """`PESFORGE_GTK_SELFTEST=theme`: which palette is in force, and does it hold contrast."""
    ratio = theme.contrast(TOK["fg"], TOK["bg"])
    print(f"SELFTEST THEME {system_theme()} bg={TOK['bg']} fg={TOK['fg']} "
          f"accent={TOK['accent']} contrast={ratio:.2f}", flush=True)
    if ratio < 4.5:
        return _fail(win, f"fg/bg contrast {ratio:.2f} is below 4.5")
    win.close()
    return False


def _selftest_fit(win):
    """`PESFORGE_GTK_SELFTEST=fit`: neither editor pane may need a scrollbar at this size.

    The user's ask is the whole editor on screen at once — no scrolling, no resizing. The
    panes are inside scroll areas as a safety net, so the test is whether their content still
    exceeds the page the scroller shows.
    """
    sizes = []
    for label, scroller in (("form", win._form_pane), ("bio", win._bio_pane)):
        inner = scroller.get_first_child()          # the pane's own scroll area
        if inner is None:
            return _fail(win, f"{label} pane has no scroll area")
        if inner.get_width() <= 1 or inner.get_height() <= 1:
            # a zero-sized pane has nothing to overflow, so the check below would "pass"
            return _fail(win, f"{label} pane is not laid out yet")
        va, ha = inner.get_vadjustment(), inner.get_hadjustment()
        over = (int(ha.get_upper()), int(va.get_upper()))
        sizes.append(f"{label}={inner.get_width()}x{inner.get_height()}"
                     f" (needs {over[0]}x{over[1]})")
        if va.get_upper() > va.get_page_size() + 0.5 or ha.get_upper() > ha.get_page_size() + 0.5:
            return _fail(win, f"{label} pane scrolls: " + ", ".join(sizes))
    print(f"SELFTEST FIT OK window={win.get_width()}x{win.get_height()} "
          + " ".join(sizes), flush=True)
    win.close()
    return False


def _selftest_sweep(win):
    """`PESFORGE_GTK_SELFTEST=sweep`: the layout is a function of size, not of history.

    Walks the live window up through its boundaries, checking at each size that the list is
    never handed less width than the columns it is showing, that the list never squeezes a
    column (which is what a blank column looks like), and that the editor panes fit their page
    wherever the split layout is in use. Then it asks the policy again for each size on the way
    back down: a size seen twice must decide the same thing. The way back is asked of
    `_size_panes` rather than performed, because a window without a window manager will not
    shrink — the decision is what the sweep compares, and the geometry follows it.
    """
    widths, height = [], win.get_height() or 1030
    first, bad, lines = {}, [], []
    state = {"i": 0, "resized": False}

    def measure(width):
        cols = tuple(col.get_title() for col, need in win._col_min if col.get_visible())
        cols_w = 36 + sum(int(col.get_fixed_width()) for col, need in win._col_min
                          if col.get_visible())
        scroller = win._list_pane.get_first_child()
        ha = scroller.get_hadjustment() if scroller is not None else None
        list_w = win._list_pane.get_width()
        if list_w < cols_w:
            bad.append(f"{width}: the list is {list_w}px, its columns need {cols_w}px")
        if ha is not None and ha.get_upper() > ha.get_page_size() + 0.5:
            bad.append(f"{width}: the list squeezes a column")
        paned = win._paned[0]
        if paned == "h" and width >= 917:
            for label, pane in (("form", win._form_pane), ("bio", win._bio_pane)):
                inner = pane.get_first_child()
                if inner is None or inner.get_width() <= 1:
                    continue
                h2, v2 = inner.get_hadjustment(), inner.get_vadjustment()
                if (h2.get_upper() > h2.get_page_size() + 0.5
                        or v2.get_upper() > v2.get_page_size() + 0.5):
                    bad.append(f"{width}: the {label} pane scrolls in the split layout")
        answer = (paned, cols)
        if width in first and first[width] != answer:
            bad.append(f"{width}: {answer[0]}/{len(answer[1])}cols on the way back, "
                       f"{first[width][0]}/{len(first[width][1])}cols before")
        first.setdefault(width, answer)
        lines.append(f"{width}->{paned}/{len(cols)}cols")

    def tick():
        if not widths:
            widths.extend(w for w in (880, 960, 1100, 1400) if w > win.get_width())
            widths.insert(0, win.get_width() or 700)
        if state["i"] < len(widths):
            width = widths[state["i"]]
            if not state["resized"]:
                win.set_size_request(width, height)
                state["resized"] = True
                return True
            measure(width)
            state["i"] += 1
            state["resized"] = False
            return True
        for width in reversed(widths):          # the way back: same sizes, same decisions
            win._size_panes(width, height)      # twice: one call turns the page, the next
            win._size_panes(width, height)      # applies the column policy for the new shape
            measure(width)
        if bad:
            return _fail(win, "layout sweep: " + "; ".join(bad))
        print(f"SELFTEST SWEEP OK sizes={'x'.join(str(w) for w in widths)} "
              + " ".join(lines), flush=True)
        win.close()
        return False

    GLib.timeout_add(200, tick)
    return False


def _all(w, out=None):
    """Every widget under `w`, internals included (the rendered rows live inside those)."""
    if out is None:
        out = []
    out.append(w)
    child = w.get_first_child()
    while child is not None:
        _all(child, out)
        child = child.get_next_sibling()
    return out


def _walk_controls(w, out):
    """The app's own widgets, in tree order.

    GTK builds a lot of internals inside its composite widgets — a spinbutton's two stepper
    buttons, a dropdown's popover and list, a scrolled window's scrollbars — and those are GTK's
    to name (they already have names of their own). Walking stops at them so the check talks
    about the controls *this app* built.
    """
    out.append(w)
    if isinstance(w, (Gtk.SpinButton, Gtk.DropDown, Gtk.Entry, Gtk.SearchEntry, Gtk.ComboBox,
                      Gtk.MenuButton, Gtk.Popover, Gtk.ListView, Gtk.Scrollbar,
                      Gtk.WindowHandle)):
        return
    child = w.get_first_child()
    while child is not None:
        _walk_controls(child, out)
        child = child.get_next_sibling()


def _selftest_a11y(win):
    """`PESFORGE_GTK_SELFTEST=a11y`: every interactive control is announced as something.

    A control with no accessible name is announced as its role alone ("button", "text") — a
    screen reader user cannot tell one ability field from another, or the tick column from the
    id column. The name may come from the app (`a11y()`), from the control's own text, or from
    its tooltip; anything focusable with none of the three fails the check.
    """
    # GtkListView is deliberately absent: a column view's list is GTK's own widget (the rows
    # inside it are named from their cells), not a control this app built.
    interactive = (Gtk.Button, Gtk.Entry, Gtk.SpinButton, Gtk.DropDown, Gtk.CheckButton,
                   Gtk.SearchEntry, Gtk.ColumnView, Gtk.MenuButton, Gtk.LinkButton)
    nodes, unnamed, named = [], [], 0
    _walk_controls(win, nodes)
    for w in nodes:
        if not isinstance(w, interactive):
            continue
        if isinstance(w, Gtk.Button):
            own = w.get_label() or ""
        elif isinstance(w, Gtk.Entry):
            own = w.get_placeholder_text() or ""
        else:
            own = ""
        if getattr(w, "_a11y_name", None) or own or w.get_tooltip_text():
            named += 1
        else:
            unnamed.append(f"{type(w).__name__}{w.get_css_classes() or ''}")
    if unnamed:
        return _fail(win, f"{len(unnamed)} interactive control(s) with no accessible name: "
                          + ", ".join(sorted(set(unnamed))))
    # The rendered list: one cell widget per visible column per row, every one of them with a
    # width — a squeezed column is a zero-width cell, which is what a blank column looks like.
    visible = [c for c in win.column_view.get_columns() if c.get_visible()]
    rows = [w for w in _all(win.column_view) if type(w).__name__.endswith("RowWidget")]
    cells = []
    for row in rows:                       # the first row with cells built: the pool keeps
        cells = [w for w in _all(row) if type(w).__name__.endswith("CellWidget")]  # some empty
        if cells:
            break
    squeezed = [w.get_css_classes() for w in cells if w.get_width() <= 1]
    if rows and len(cells) != len(visible):
        return _fail(win, f"{len(cells)} cell(s) for {len(visible)} visible column(s)")
    if squeezed:
        return _fail(win, f"squeezed cell(s): {squeezed}")
    print(f"SELFTEST A11Y OK named={named} controls={len(nodes)} "
          f"columns={len(visible)} rows={len(rows)} cells={len(cells)}", flush=True)
    win.close()
    return False


def _selftest_align(win):
    """`PESFORGE_GTK_SELFTEST=align`: one label width and one value column in the abilities grid.

    A label's `set_size_request` is a minimum, so a term wider than it takes its own natural width
    and pushes its value box out of line with the boxes above it — the classic vocabularies'
    "Short Pass Accuracy" needs ~150px against the 110 the grid used to pay, which put boxes in
    one column 40px apart. This fails on a second label width or a second value-box x; the
    Position row counts too (it carried its own literal width and sat 4px out).
    """
    grid = None
    for w in _all(win._form_pane):
        if isinstance(w, Gtk.Grid) and w.get_height() > 150:
            grid = w
            break
    if grid is None:
        return _fail(win, "no abilities grid in the form pane")
    widths, boxes, cells = {}, {}, 0
    child = grid.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Box):
            lab = child.get_first_child()
            box = lab.get_next_sibling() if lab is not None else None
            if isinstance(lab, Gtk.Label) and box is not None:
                cells += 1
                widths[lab.get_width()] = widths.get(lab.get_width(), 0) + 1
                x = box.get_allocation().x
                boxes[x] = boxes.get(x, 0) + 1
        child = child.get_next_sibling()
    if not cells:
        return _fail(win, "the abilities grid has no label/value cells")
    if len(widths) != 1 or len(boxes) != 1:
        return _fail(win, f"the abilities grid is ragged: label widths {sorted(widths.items())}, "
                          f"value columns at x {sorted(boxes.items())}")
    print(f"SELFTEST ALIGN OK step={getattr(win, 'ability_label_step', 1.0)} "
          f"cells={cells} label_w={list(widths)[0]} "
          f"box_x={list(boxes)[0]} grid={grid.get_width()} "
          f"pane={win._form_pane.get_first_child().get_width()}", flush=True)
    win.close()
    return False


def _selftest_scale(win):
    """`PESFORGE_GTK_SELFTEST=scale`: the app follows the desktop's text scale, without clipping.

    GTK converts the stylesheet's px font sizes through the Xft DPI, so the desktop's text
    scaling factor moves every size in the app (measured: the player-name line is 18px tall at
    1.0 and 36px at 2.0). At a larger scale the panes scroll — that is allowed. What is not
    allowed is a widget drawn outside its parent, or two visible siblings drawn on top of each
    other, and the check walks the whole tree for both.
    """
    scale = os.environ.get("PESFORGE_GTK_SCALE", "1")
    bad, containers, checked = [], 0, 0
    nodes = _all(win)
    for w in nodes:
        if isinstance(w, (Gtk.ScrolledWindow, Gtk.Viewport, Gtk.ColumnView, Gtk.Popover,
                          Gtk.Overlay, Gtk.ListView)):
            continue      # a scroller's child may exceed the page: it clips, by design; a list
                          # view also prefetches rows below its viewport for the same reason
        kids = []
        child = w.get_first_child()
        while child is not None:
            if child.get_visible() and child.get_width() > 0 and child.get_height() > 0:
                kids.append(child)
            child = child.get_next_sibling()
        if not kids:
            continue
        ww, wh = w.get_width(), w.get_height()
        if ww <= 1 or wh <= 1:
            continue           # not laid out yet (a hidden column's header, a recycled row):
                               # there is nothing to compare a child's allocation against
        containers += 1
        rects = []
        for kid in kids:
            box = kid.get_allocation()
            checked += 1
            if box.x < -1 or box.y < -1 or box.x + box.width > ww + 1 or box.y + box.height > wh + 1:
                bad.append(f"{type(kid).__name__} outside its {type(w).__name__} "
                           f"({box.x},{box.y} {box.width}x{box.height} in {ww}x{wh})")
            rects.append((kid, box))
        for i, (a_kid, a) in enumerate(rects):
            for b_kid, b in rects[i + 1:]:
                if (a.x < b.x + b.width - 1 and b.x < a.x + a.width - 1
                        and a.y < b.y + b.height - 1 and b.y < a.y + a.height - 1):
                    bad.append(f"{type(a_kid).__name__} overlaps {type(b_kid).__name__} "
                               f"in {type(w).__name__}")
    hero = win.hero.get_height() if hasattr(win, "hero") else 0
    if bad:
        return _fail(win, f"text scale {scale}: " + "; ".join(bad[:4]))
    dpi = Gtk.Settings.get_default().get_property("gtk-xft-dpi")
    fresh = Gtk.Label(label="Hg")
    fresh_h = fresh.measure(Gtk.Orientation.VERTICAL, -1)[1]
    font = win.hero.get_pango_context().get_metrics(None, None).get_height() / 1024.0
    print(f"SELFTEST SCALE OK scale={scale} dpi={dpi} hero_h={hero} fresh_h={fresh_h} font_pt={font:.1f} "
          f"containers={containers} widgets={checked} "
          f"window={win.get_width()}x{win.get_height()}", flush=True)
    win.close()
    return False


def _selftest_watch(win):
    """`PESFORGE_GTK_SELFTEST=watch`: print the geometry every tick, for a resize driver.

    Nothing is asserted here — the driver outside resizes the window (xdotool under a window
    manager) and reads these lines. It exists because a window *without* a manager will not
    honour a shrink, so the sweep mode can only ask the policy what it would do; with a manager
    the down-and-back-up pass is performed for real, and this is the record of what happened.
    """
    def tick():
        parts = [f"WATCH {win.get_width()}x{win.get_height()} paned={win._paned[0]}"]
        for label, pane in (("form", win._form_pane), ("bio", win._bio_pane)):
            inner = pane.get_first_child()
            if inner is None:
                continue
            ha, va = inner.get_hadjustment(), inner.get_vadjustment()
            parts.append(f"{label}={inner.get_width()}x{inner.get_height()}"
                         + ("+h" if ha.get_upper() > ha.get_page_size() + 0.5 else "")
                         + ("+v" if va.get_upper() > va.get_page_size() + 0.5 else ""))
        parts.append(f"list={win._list_pane.get_width()}")
        parts.append("cols=" + ",".join(c.get_title() for c, _need in win._col_min
                                        if c.get_visible()))
        print(" ".join(parts), flush=True)
        return True                       # keep printing until the driver kills us
    GLib.timeout_add(400, tick)
    return False


def _selftest_select(win):
    """Clicking a player shows that player: GTK's selection-changed reports the *previous*
    position, which used to leave the form one click behind."""
    try:
        if win.table is None:
            win.load_database(default_database(os.environ.get("PES2019_DIR", "")))
        if win.table is None:
            return _fail(win, f"no database: {win.status.get_text()}")

        def shown():
            return win.index          # the table row the form is holding

        checks = []
        win.show_row(7)
        checks.append(("a click shows the row it hit", shown(), 7))
        win.selection.set_selected(3)            # GTK reports this one as pos=0
        checks.append(("a selection change shows that row", shown(), 3))
        for pos in (1, 2, 4):                     # ticks are the user's alone
            win._set_checked(win.sorted.get_item(pos).index, True)
        checks.append(("ticking does not move the form", shown(), 3))
        win.show_row(4)                          # a click among ticked rows
        checks.append(("a click among ticked rows still shows it", shown(), 4))
        checks.append(("the ticks are still counted", len(win.selected_indexes()), 3))
        for label, got, want in checks:
            if got != want:
                return _fail(win, f"{label}: got {got}, want {want}")
            print(f"SELECT {label}: {got}", flush=True)
        print("SELFTEST SELECT OK", flush=True)
    except Exception as exc:
        return _fail(win, f"{type(exc).__name__}: {exc}")
    win.close()
    return False


def _selftest_hub(win):
    """The hub says what a pick holds, and offers Load only when it can open it."""
    try:
        checks = []
        pack = default_database(os.environ.get("PES2019_DIR", ""))
        if pack:
            details, can_load = describe_database(pack)
            checks.append(("a PES 2019 pack is loadable", can_load, True))
            checks.append(("its players are counted", any("players" in d for d in details), True))
        db15 = vintage_database("pes2015")
        if db15 and os.path.isfile(db15):
            details, can_load = describe_database(db15)
            checks.append(("a PES 2015 database is loadable", can_load, True))
            checks.append(("and counted", any("players" in d for d in details), True))
            details, can_load, target = describe_pick(os.path.dirname(db15))
            checks.append(("its Data/ folder resolves to dt10.cpk",
                           os.path.basename(target), "dt10.cpk"))
            checks.append(("and that folder pick is loadable", can_load, True))
        spfl = os.path.join(os.environ.get("SPFL_DIR", ""), "download", "data_s2627.cpk")
        if not os.path.isfile(spfl):
            spfl = vintage_database("pes2021")
        if os.path.isfile(spfl):
            details, can_load = describe_database(spfl)
            checks.append(("SP Football Life is loadable", can_load, True))
            checks.append(("and is named", any("PES 2021" in d for d in details), True))
            details, can_load, target = describe_pick(install_of(spfl))
            checks.append(("its install folder finds download/data_s2627.cpk",
                           os.path.basename(target), "data_s2627.cpk"))
            checks.append(("which this build edits", can_load, True))
            checks.append(("and loading that folder works",
                           win.load_database(install_of(spfl)), True))
            checks.append(("the footer names the file it found",
                           os.path.basename(spfl) in win.status.get_text(), True))
            checks.append(("and counts its players",
                           f"{len(win.table):,} players" in win.status.get_text(), True))
            checks.append(("loaded as the 2021 vintage", win.vintage, "pes2021"))
            win.unload_database()
            details, can_load, target = describe_pick(os.path.join(install_of(spfl), "Data"))
            checks.append(("picking its Data/ folder still finds download/data_s2627.cpk",
                           os.path.basename(target), "data_s2627.cpk"))
        if not checks:
            return _fail(win, "no install to describe")
        win.hub = ImportHub(win)
        shown = pack or spfl
        win.hub.show_path(shown)
        # Load tracks whether *this* pick can be opened — SP Football Life included, which is
        # why the old "a PES 2019 pack exists" expectation no longer holds.
        checks.append(("the hub takes a loadable pick", win.hub.load_btn.get_sensitive(),
                       bool(shown) and describe_pick(shown)[1]))
        if pack:
            # a folder pick: the install resolves to the pack the game itself loads
            win.hub.show_path(install_of(pack))
            checks.append(("the hub takes a folder and finds the pack",
                           os.path.basename(win.hub.path), os.path.basename(pack)))
            checks.append(("and offers to load it", win.hub.load_btn.get_sensitive(), True))
            win.hub.show_path(os.path.dirname(pack))
            checks.append(("the folder holding that pack resolves to it",
                           os.path.basename(win.hub.path), os.path.basename(pack)))
        empty = os.path.join(os.environ.get("PES2019_DIR", ""), "Data", "dt00_4K_x64.cpk")
        if os.path.isfile(empty):
            win.hub.show_path(empty)
            checks.append(("a pick with no table offers the database of its folder",
                           os.path.basename(win.hub.path), "dt10_x64.cpk"))
            checks.append(("and it is loadable", win.hub.load_btn.get_sensitive(), True))
            win.hub.show_path(os.path.dirname(empty))
            checks.append(("picking that Data/ folder resolves past the dt00 stub",
                           os.path.basename(win.hub.path), "dt10_x64.cpk"))
        missing = os.path.join(os.environ.get("TMPDIR") or "/tmp", "pesforge-no-database-here")
        os.makedirs(missing, exist_ok=True)
        win.hub.show_path(missing)
        checks.append(("a folder with no database offers nothing",
                       (win.hub.load_btn.get_sensitive(), "no player table" in
                        win.hub.summary.get_text()), (False, True)))
        win.hub.close()
        for label, got, want in checks:
            if got != want:
                return _fail(win, f"{label}: got {got}, want {want}")
            print(f"HUB {label}: {got}", flush=True)
        print("SELFTEST HUB OK", flush=True)
    except Exception as exc:
        return _fail(win, f"{type(exc).__name__}: {exc}")
    win.close()
    return False


def _selftest_write2015(win):
    """Load a copy of the PES 2015 database, edit it, save, and read the edits back.

    The copy lives in a scratch folder shaped like an install, so the save writes that
    and never the game — the released file's hash is checked before it finishes.
    """
    import hashlib
    import shutil
    try:
        src = vintage_database("pes2015")
        if not src or not os.path.isfile(src):
            return _fail(win, f"no PES 2015 database (set PES2015_DIR): {src!r}")
        before = hashlib.sha256(open(src, "rb").read()).hexdigest()
        root = os.path.join(os.environ.get("TMPDIR") or "/tmp", "pesforge-2015-check")
        copy = os.path.join(root, "Data", "dt10.cpk")
        os.makedirs(os.path.dirname(copy), exist_ok=True)
        shutil.copyfile(src, copy)
        win.load_database(copy)
        if win.table is None:
            return _fail(win, f"could not load the copy: {win.status.get_text()}")
        if win.vintage != "pes2015" or len(win.rows) < 1000:
            return _fail(win, f"loaded {win.vintage} with {len(win.rows)} rows")
        i = 3
        pid = win.table.pid(i)
        label = "Speed" if "Speed" in win.mod.FIELD_BY_LABEL else ability_order(win.mod)[0]
        was = win.table.get(i, label)
        want = min(99, was + 3)
        win.table.set(i, label, want)
        win._mark_dirty(i, label)
        win.table.set_name(i, shirt="SELFTEST")
        win._mark_dirty(i, "name:shirt")
        win._save()
        if "saved" not in win.status.get_text():
            return _fail(win, f"save said {win.status.get_text()!r}")
        back = win.mod.Cpk(copy).read_table("Player")
        hits = back.find(str(pid))
        if not hits:
            return _fail(win, f"player {pid} vanished from the copy")
        j = hits[0]
        if back.get(j, label) != want or back.shirt(j) != "SELFTEST":
            return _fail(win, f"read back {back.get(j, label)!r}/{back.shirt(j)!r}, "
                                       f"want {want}/'SELFTEST'")
        if hashlib.sha256(open(src, "rb").read()).hexdigest() != before:
            return _fail(win, "the game's own database was modified")
        print(f"SELFTEST WRITE2015 OK players={len(win.rows)} pid={pid} "
              f"{label}={was}->{want} shirt=SELFTEST", flush=True)
    except Exception as exc:
        return _fail(win, f"{type(exc).__name__}: {exc}")
    win.close()
    return False


def _selftest_write2021(win):
    """Load a copy of the SP Football Life / PES 2021 database, edit it, save, read it back.

    The copy lives in a scratch folder shaped like an install (`download/data_s2627.cpk`), so
    the save writes that and never the game — the released file's hash is checked at the end.
    """
    import hashlib
    import shutil
    try:
        src = vintage_database("pes2021")
        if not src or not os.path.isfile(src):
            return _fail(win, f"no 2021 database (set SPFL_DIR): {src!r}")
        before = hashlib.sha256(open(src, "rb").read()).hexdigest()
        root = os.path.join(os.environ.get("TMPDIR") or "/tmp", "pesforge-2021-check")
        copy = os.path.join(root, "download", os.path.basename(src))
        os.makedirs(os.path.dirname(copy), exist_ok=True)
        shutil.copyfile(src, copy)
        win.load_database(copy)
        if win.table is None:
            return _fail(win, f"could not load the copy: {win.status.get_text()}")
        if win.vintage != "pes2021" or len(win.rows) < 10000:
            return _fail(win, f"loaded {win.vintage} with {len(win.rows)} rows")
        i = 3
        pid = win.table.pid(i)
        label = "Speed"
        was = win.table.get(i, label)
        want = min(99, was + 3)
        win.table.set(i, label, want)
        win._mark_dirty(i, label)
        skill = "Skill: " + pes2021.SKILLS[0]
        win.table.set(i, skill, 1)
        win._mark_dirty(i, skill)
        win.table.set_name(i, shirt="SELFTEST")
        win._mark_dirty(i, "name:shirt")
        win._save()
        if "saved" not in win.status.get_text():
            return _fail(win, f"save said {win.status.get_text()!r}")
        back = win.mod.Cpk(copy).read_table("Player")
        hits = back.find(str(pid))
        if not hits:
            return _fail(win, f"player {pid} vanished from the copy")
        j = hits[0]
        if (back.get(j, label), back.get(j, skill), back.shirt(j)) != (want, 1, "SELFTEST"):
            return _fail(win, 
                f"read back {back.get(j, label)!r}/{back.get(j, skill)!r}/{back.shirt(j)!r}, "
                f"want {want}/1/'SELFTEST'")
        if hashlib.sha256(open(src, "rb").read()).hexdigest() != before:
            return _fail(win, "the game's own database was modified")
        print(f"SELFTEST WRITE2021 OK players={len(win.rows)} pid={pid} "
              f"{label}={was}->{want} {skill}=1 shirt=SELFTEST", flush=True)
    except Exception as exc:
        return _fail(win, f"{type(exc).__name__}: {exc}")
    win.close()
    return False


def _selftest_cycle(win):
    """Import PES 2019, unload, import SP Football Life, unload, import PES 2015."""
    try:
        spfl = settings.database_path(os.environ.get("SPFL_DIR", "")) \
            if os.environ.get("SPFL_DIR") else ""
        legs = (("pes2019", vintage_database("pes2019"), "loaded"),
                ("spfl", spfl, "loaded"),
                ("pes2015", vintage_database("pes2015"), "loaded"))
        for n, (name, path, expect) in enumerate(legs, 1):
            if not path or not os.path.isfile(path):
                print(f"CYCLE step={n} name={name} result=skipped (no install)", flush=True)
                continue
            win.load_database(path)
            loaded = win.table is not None
            if loaded and len(win.rows) < 1000:
                return _fail(win, f"{name}: loaded {len(win.rows)} rows")
            result = "loaded" if loaded else "refused"
            print(f"CYCLE step={n} name={name} result={result} file={os.path.basename(path)} "
                  f"status={win.status.get_text()[:90]!r}", flush=True)
            if result != expect:
                return _fail(win, f"{name}: {result}, expected {expect}")
            want_name = {"spfl": "PES 2021", "pes2015": "PES 2015"}.get(name)
            if want_name and want_name not in win.status.get_text():
                return _fail(win, f"{name}: the status does not name it: "
                                           f"{win.status.get_text()!r}")
            if name in ("spfl", "pes2021") and win.vintage != "pes2021":
                return _fail(win, f"{name}: loaded as {win.vintage}")
            win.unload_database()
            if win.table is not None or win.rows or win.store.get_n_items():
                return _fail(win, f"{name}: unload left the database loaded")
            if win.stack.get_visible_child_name() != "empty":
                return _fail(win, f"{name}: unload did not return to the empty page")
            if win.save_btn.get_sensitive() or win.revert_btn.get_sensitive():
                return _fail(win, f"{name}: controls stayed live after unload")
        print("SELFTEST CYCLE OK", flush=True)
    except Exception as exc:
        return _fail(win, f"{type(exc).__name__}: {exc}")
    win.close()
    return False


def _selftest_empty(win):
    """The window must start with nothing loaded, and import a database on demand."""
    try:
        if win.table is not None or win.rows or win.store.get_n_items():
            return _fail(win, "a database was loaded without being asked")
        if win.stack.get_visible_child_name() != "empty":
            return _fail(win, f"visible page is "
                                       f"{win.stack.get_visible_child_name()!r}, want 'empty'")
        if win.save_btn.get_sensitive() or win.revert_btn.get_sensitive():
            return _fail(win, "Save/Revert are live with no database")
        if win.search.get_sensitive():
            return _fail(win, "the search box is live with no database")
        want = default_database(GAME)
        if not want or not win.load_database(want):
            return _fail(win, f"importing {want or 'nothing'} failed: "
                                       f"{win.status.get_text()!r}")
        if win.stack.get_visible_child_name() != "data":
            return _fail(win, "the panes did not come up after the import")
        if len(win.rows) < 10000 or win.store.get_n_items() != len(win.rows):
            return _fail(win, f"{len(win.rows)} rows, "
                                       f"{win.store.get_n_items()} in the model")
        if not (win.save_btn.get_sensitive() and win.revert_btn.get_sensitive()):
            return _fail(win, "Save/Revert stayed disabled after the import")
        print(f"SELFTEST EMPTY OK rows={len(win.rows)} source={win.source_name}", flush=True)
    except Exception as exc:
        return _fail(win, f"{type(exc).__name__}: {exc}")
    win.close()
    return False


def _selftest_write(win):
    """Bulk apply, commit and restore against whatever PES2019_DIR points at.

    Only ever run with that variable aimed at a copy: it does write the database.
    """
    try:
        return _selftest_write_run(win)
    except Exception as exc:                     # a broken check must not hang the app
        return _fail(win, f"{type(exc).__name__}: {exc}")


def _selftest_clubs(win):
    """No list row shows a national team where its club belongs.

    The country assignment row often comes first, so "first row wins" labelled 1,263 of the
    PES 2019 players with a nation instead of their club. Returns how many rows were in that
    position, or False when one is still wrong (the failure is reported here).
    """
    checked = 0
    for i in range(len(win.table)):
        rows = win.assign_by_pid.get(win.table.pid(i)) or []
        if not rows or win.assign.team(rows[0]) > pesdb.NATIONAL_MAX:
            continue                       # a club row already comes first
        checked += 1
        want = next((win.clubs.get(win.assign.team(r), "") for r in rows
                     if win.assign.team(r) > pesdb.NATIONAL_MAX), "")
        if win.rows[i].club != want:
            return _fail(win, f"row {i} ({win.table.name(i)}): shows club "
                                       f"{win.rows[i].club!r}, want {want!r}")
    return checked


def _selftest_chrome(win):
    """The window's plain buttons are exactly the ones that act.

    The spike carried inert Undo/Redo/Discard/Save-to-game buttons in the header, a footer
    "Apply to player" that never could be clicked and a decorative Players/Clubs/PES 2015 tab
    row; a button that does nothing reads as broken the moment the app is a daily driver.
    """
    want = sorted({"Import database…", "Unload", "Revert", "Save to game",
                   "Apply to selected", "Clear selection"})
    got = sorted({w.get_label() for w in tree(win)
                  if type(w) is Gtk.Button and w.get_label()})
    if got != want:
        return _fail(win, f"window buttons are {got}, want {want}")
    print(f"CHROME OK buttons={len(got)}", flush=True)
    return True


def _selftest_write_run(win):
    import hashlib
    path = win.source
    # This check writes the database. It refuses anything outside a temporary directory: an
    # install's own file is not this check's to touch, and there is no undo for a mistake there.
    # Point PES2019_DIR at a copy (the suite's `pes2019_box` does exactly that) before running it.
    scratch = os.path.realpath(tempfile.gettempdir())
    if not os.path.realpath(path).startswith(scratch + os.sep):
        return _fail(win, f"refusing to write {path} — this check only runs against a copy "
                          f"under {scratch}")

    before = hashlib.sha256(open(path, "rb").read()).hexdigest()
    backup_dir = settings.backups_dir()
    pre = set(win.mod.backup_list(backup_dir))
    picks = [0, 1, 2]
    for pos in picks:
        win._set_checked(win.sorted.get_item(pos).index, True)
    label = "Speed"
    win.bulk_field.set_selected(bulk_fields(win.mod).index(label))
    win.bulk_op.set_selected(1)                                  # Add
    win.bulk_amount.set_value(5)
    old = {i: win.table.get(i, label) for i in picks}
    print(f"WRITE stage=selected picks={picks}", flush=True)
    win._bulk_apply()
    print(f"WRITE stage=applied dirty={len(win.dirty)}", flush=True)
    for i, was in old.items():
        want = min(99, was + 5)
        if win.table.get(i, label) != want:
            return _fail(win, f"row {i}: {label} is {win.table.get(i, label)}, want {want}")
    if len(win.dirty) != 3:
        return _fail(win, f"staged {len(win.dirty)} edits, want 3")
    if not win.batch_label.get_text().startswith("3 selected"):
        return _fail(win, f"tray reads {win.batch_label.get_text()!r}")
    if not all(win.rows[i].edited for i in picks):
        return _fail(win, "a changed row is not flagged in the list")
    win._undo()
    if any(win.table.get(i, label) != was for i, was in old.items()):
        return _fail(win, "undo did not restore every row of the bulk step")
    win._redo()
    if any(win.table.get(i, label) != min(99, was + 5) for i, was in old.items()):
        return _fail(win, "redo did not reapply the bulk step")
    print("WRITE stage=undo/redo ok", flush=True)
    if win._on_close_request(ask=False) is not True:
        return _fail(win, "a close with pending edits was not held back")
    win._save()
    print(f"WRITE stage=saved status={win.status.get_text()!r}", flush=True)
    if "saved" not in win.status.get_text():
        return _fail(win, f"save said {win.status.get_text()!r}")
    for i in picks:
        # the STATE cell is repainted by swapping the row object, so a saved row must be a
        # fresh object marked clean — otherwise the list keeps saying "edited"
        if win.rows[i].edited or win.store.get_item(i) is not win.rows[i]:
            return _fail(win, f"row {i} still reads as edited after the save")
    back = win.mod.Cpk(path).read_table("Player")
    for i, was in old.items():
        if not win._reads_back(back, back.pid(i), {label: min(99, was + 5)}):
            return _fail(win, f"player {back.pid(i)}: {label} missing after save")
    fresh = [n for n in win.mod.backup_list(backup_dir) if n not in pre]
    if not fresh:
        return _fail(win, "the save took no backup")
    reports = sorted(n for n in os.listdir(backup_dir) if n.startswith("changes-"))
    if not reports:
        return _fail(win, "the save wrote no change report")
    text = open(os.path.join(backup_dir, reports[-1]), encoding="utf-8").read()
    for i, was in old.items():
        line = [ln for ln in text.splitlines() if ln.startswith(f"pid {back.pid(i)}\t")]
        if not line or "Speed" not in line[0] or f"{was} -> {min(99, was + 5)}" not in line[0]:
            return _fail(win, f"the change report misses player {back.pid(i)}: {line}")
    print(f"WRITE stage=report file={reports[-1]} lines={len(text.splitlines())}", flush=True)
    for name in fresh:
        win.mod.restore_backup(name)
    if hashlib.sha256(open(path, "rb").read()).hexdigest() != before:
        return _fail(win, "restoring the backups did not return the original bytes")
    os.chmod(path, 0o444)                 # a write that cannot happen must say so
    win.table.set(picks[0], label, min(99, old[picks[0]] + 7))
    win._mark_dirty(picks[0], label)
    win._save()
    if "could not write" not in win.status.get_text():
        return _fail(win, f"a read-only CPK was not reported: "
                                   f"{win.status.get_text()!r}")
    if not win.dirty:
        return _fail(win, "a failed save dropped the pending edits")
    os.chmod(path, 0o644)
    win.dirty.clear()
    print("WRITE stage=read-only reported", flush=True)
    print(f"SELFTEST WRITE OK players={len(win.rows)} edited={len(picks)} "
          f"backups={len(fresh)} restored=1 state=clean", flush=True)
    win.close()
    return False


def _selftest_write5(win):
    """Load a copy of the PES 5 option file, edit it, save, and read the edits back.

    The copy is a KONAMI-WIN32PES5OPT in a scratch folder of its own, so the save writes that
    and never the game's file — the original's hash is checked before it finishes.
    """
    try:
        src = vintage_database("pes5")
        if not src or not os.path.isfile(src):
            return _fail(win, f"no PES 5 option file (set PES5_DIR): {src!r}")
        before = hashlib.sha256(open(src, "rb").read()).hexdigest()
        root = os.path.join(os.environ.get("TMPDIR") or "/tmp", "pesforge-pes5-check")
        copy = os.path.join(root, "KONAMI-WIN32PES5OPT")
        os.makedirs(root, exist_ok=True)
        shutil.copyfile(src, copy)
        win.load_database(copy)
        if win.table is None:
            return _fail(win, f"could not load the copy: {win.status.get_text()}")
        if win.vintage != "pes5" or len(win.rows) < 5000:
            return _fail(win, f"loaded {win.vintage} with {len(win.rows)} rows")
        if not win.name_entries["Name"].get_text().strip():
            return _fail(win, "the first load put the option file's blank record in the form")
        i = 3
        pid = win.table.pid(i)
        label = "Speed" if "Speed" in win.mod.FIELD_BY_LABEL else ability_order(win.mod)[0]
        was = win.table.get(i, label)
        want = min(99, was + 3)
        win.table.set(i, label, want)
        win._mark_dirty(i, label)
        win.table.set_name(i, shirt="SELFTEST")
        win._mark_dirty(i, "name:shirt")
        win._save()
        if "saved" not in win.status.get_text():
            return _fail(win, f"save said {win.status.get_text()!r}")
        reopened = win.mod.Cpk(copy)
        back = reopened.read_table("Player")
        hits = back.find(str(pid))
        if not hits:
            return _fail(win, f"player {pid} vanished from the copy")
        j = hits[0]
        if back.get(j, label) != want or back.shirt(j) != "SELFTEST":
            return _fail(win, f"read back {back.get(j, label)!r}/{back.shirt(j)!r}, "
                               f"want {want}/'SELFTEST'")
        if reopened.bad_sums:
            return _fail(win, f"the written file's checksums do not add up: {reopened.bad_sums}")
        if hashlib.sha256(open(src, "rb").read()).hexdigest() != before:
            return _fail(win, "the game's own option file was modified")
        print(f"SELFTEST WRITE5 OK players={len(win.rows)} pid={pid} "
              f"{label}={was}->{want} shirt=SELFTEST", flush=True)
    except Exception as exc:
        return _fail(win, f"{type(exc).__name__}: {exc}")
    win.close()
    return False


def _selftest_write6(win):
    """Load a copy of the PES 6 option file, edit it, save, and read the edits back.

    The copy is a KONAMI-WIN32PES6OPT in a scratch folder of its own, so the save writes that
    and never the game's file — the original's hash is checked before it finishes.
    """
    try:
        src = vintage_database("pes6")
        if not src or not os.path.isfile(src):
            return _fail(win, f"no PES 6 option file (set PES6_DIR): {src!r}")
        before = hashlib.sha256(open(src, "rb").read()).hexdigest()
        root = os.path.join(os.environ.get("TMPDIR") or "/tmp", "pesforge-pes6-check")
        copy = os.path.join(root, "KONAMI-WIN32PES6OPT")
        os.makedirs(root, exist_ok=True)
        shutil.copyfile(src, copy)
        win.load_database(copy)
        if win.table is None:
            return _fail(win, f"could not load the copy: {win.status.get_text()}")
        if win.vintage != "pes6" or len(win.rows) < 5000:
            return _fail(win, f"loaded {win.vintage} with {len(win.rows)} rows")
        if not win.name_entries["Name"].get_text().strip():
            return _fail(win, "the first load put the option file's blank record in the form")
        i = 3
        pid = win.table.pid(i)
        label = "Speed" if "Speed" in win.mod.FIELD_BY_LABEL else ability_order(win.mod)[0]
        was = win.table.get(i, label)
        want = min(99, was + 3)
        win.table.set(i, label, want)
        win._mark_dirty(i, label)
        win.table.set_name(i, shirt="SELFTEST")
        win._mark_dirty(i, "name:shirt")
        win._save()
        if "saved" not in win.status.get_text():
            return _fail(win, f"save said {win.status.get_text()!r}")
        reopened = win.mod.Cpk(copy)
        back = reopened.read_table("Player")
        hits = back.find(str(pid))
        if not hits:
            return _fail(win, f"player {pid} vanished from the copy")
        j = hits[0]
        if back.get(j, label) != want or back.shirt(j) != "SELFTEST":
            return _fail(win, f"read back {back.get(j, label)!r}/{back.shirt(j)!r}, "
                               f"want {want}/'SELFTEST'")
        if reopened.bad_sums:
            return _fail(win, f"the written file's checksums do not add up: {reopened.bad_sums}")
        if hashlib.sha256(open(src, "rb").read()).hexdigest() != before:
            return _fail(win, "the game's own option file was modified")
        print(f"SELFTEST WRITE6 OK players={len(win.rows)} pid={pid} "
              f"{label}={was}->{want} shirt=SELFTEST", flush=True)
    except Exception as exc:
        return _fail(win, f"{type(exc).__name__}: {exc}")
    win.close()
    return False


def run(win):
    """`PESFORGE_GTK_SELFTEST=1`: prove the screen built against real data, then quit.

    Guards the two failures a traceback never shows: an empty detail pane beside a selected
    row, and a list model that does not match the table behind it. `=empty` checks the other
    half instead: nothing is read until it is asked for.
    """
    mode = os.environ.get("PESFORGE_GTK_SELFTEST", "").lower()
    if mode == "empty":
        return _selftest_empty(win)
    if mode == "cycle":
        return _selftest_cycle(win)
    if mode == "select":
        return _selftest_select(win)
    if mode == "hub":
        return _selftest_hub(win)
    if mode == "write2015":
        return _selftest_write2015(win)
    if mode == "write5":
        return _selftest_write5(win)
    if mode == "write6":
        return _selftest_write6(win)
    if mode == "write2021":
        return _selftest_write2021(win)
    if mode == "fit":
        return _selftest_fit(win)
    if mode == "sweep":
        return _selftest_sweep(win)
    if mode == "a11y":
        return _selftest_a11y(win)
    if mode == "scale":
        return _selftest_scale(win)
    if mode == "align":
        return _selftest_align(win)
    if mode == "watch":
        return _selftest_watch(win)
    if mode == "theme":
        return _selftest_theme(win)
    if not win.rows or len(win.store) != len(win.rows):
        return _fail(win, f"list holds {len(win.store)} rows, table has {len(win.rows)}")
    if win.index is None or not win.name_entries["Name"].get_text().strip():
        return _fail(win, "no player loaded into the form")
    total = win.filtered.get_n_items()
    win.search.set_text(win.rows[win.index].name[:5])
    win._refilter()                        # applied directly: the signal is async
    narrowed = win.filtered.get_n_items()
    if not 0 < narrowed < total:
        return _fail(win, f"search for {win.rows[win.index].name!r} matched {narrowed} of {total}")
    win.search.set_text("zzzznomatch")
    win._refilter()
    if win.filtered.get_n_items():
        return _fail(win, "a query nothing can match still listed players")
    if "no player matches" not in win.count.get_text():
        return _fail(win, f"an empty result read as: {win.count.get_text()!r}")
    win.search.set_text("")
    win._refilter()
    if win.filtered.get_n_items() != total:
        return _fail(win, "clearing the search did not restore the list")
    if os.environ.get("PESFORGE_GTK_SELFTEST", "").lower() in ("write", "2"):
        return _selftest_write(win)
    nations = _selftest_clubs(win)
    if nations is False:                    # the club check already reported what was wrong
        return False
    if _selftest_chrome(win) is False:
        return False
    print(f"SELFTEST OK players={total} selected={win.index} "
          f"abilities={len(win.abilities)} chips={len(win.chips)} "
          f"nations={nations} source={win.source_name}", flush=True)
    win.close()
    return False

