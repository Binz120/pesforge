"""The player list: the row model, the ColumnView, its cells and its width policy.

The list is the only view onto the table. The *current row* is a Gtk.SingleSelection
(item-tracked, so a filter or a sort cannot re-point it) and the form follows it; the bulk
pick is a set of table indexes this module owns and paints, so a filter change can never
move a tick, and ticking a row never touches the current row.
"""
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GObject, Gtk, Pango  # noqa: E402

import pesdb  # noqa: E402

from .widgets import a11y, scaled, tree  # noqa: E402

LIST_MAX = 452       # the six list columns: check 36 + id 52 + name 158 + club 106 + pos 44 + state 56
LIST_MIN_H = 130     # the list strip's height when the editor sits under it


# ---------------------------------------------------------------- the row model
class Row(GObject.Object):
    """One list row: the values the view shows, plus the index into the player table."""

    __gtype_name__ = "PlayerRow"

    index = GObject.Property(type=int, default=0)
    pid = GObject.Property(type=int, default=0)
    name = GObject.Property(type=str, default="")
    club = GObject.Property(type=str, default="")
    pos = GObject.Property(type=str, default="")
    edited = GObject.Property(type=bool, default=False)

    @property
    def state(self):
        return "edited" if self.edited else ""



class ListPaneMixin:
    def _list(self):
        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        pane.add_css_class("pane")
        pane.set_vexpand(True)
        pane.set_overflow(Gtk.Overflow.HIDDEN)
        self.column_view = Gtk.ColumnView()
        self._col_min = []                       # (column, pane width it needs to be shown at)
        self._cols = {}                          # title -> column, for the width policy
        self.column_view.set_show_column_separators(False)
        self.column_view.set_show_row_separators(False)
        self.column_view.add_css_class("rows")
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)   # before the row's own handling
        keys.connect("key-pressed", self._on_view_key)
        self.column_view.add_controller(keys)
        check_factory = Gtk.SignalListItemFactory()
        check_factory.connect("setup", self._check_setup)
        check_factory.connect("bind", self._check_bind)
        check_col = Gtk.ColumnViewColumn(title="✓", factory=check_factory)
        check_col.set_fixed_width(scaled(36))
        self.column_view.append_column(check_col)
        self._check_col = check_col
        # no rating columns here: the abilities pane shows every number, at a readable size
        for title, width, attr, align, sortable in (
                ("ID", 52, "pid", 1.0, True), ("NAME", 158, "name", 0.0, True),
                ("CLUB", 106, "club", 0.0, True), ("POS", 44, "pos", 0.5, False)):
            factory = Gtk.SignalListItemFactory()
            factory.connect("setup", self._cell_setup)
            factory.connect("bind", self._cell_bind, attr)
            col = Gtk.ColumnViewColumn(title=title, factory=factory)
            col.set_fixed_width(scaled(width))    # fixed: a squeezed column renders blank
            col.set_resizable(True)
            if title in ("NAME", "CLUB"):
                col.set_expand(True)             # name and club share the slack the pane has
            if title in ("CLUB", "POS"):
                self._col_min.append((col, scaled(LIST_MAX)))  # all six or neither
            if title == "ID":
                self._col_min.append((col, scaled(250)))       # below this: no room for a name
            self._cols[title] = col
            if sortable:
                sorter = Gtk.CustomSorter.new(self._compare, attr)
                col.set_sorter(sorter)
            self.column_view.append_column(col)
        state_factory = Gtk.SignalListItemFactory()
        state_factory.connect("setup", self._cell_setup)
        state_factory.connect("bind", self._cell_bind, "state")
        state_col = Gtk.ColumnViewColumn(title="STATE", factory=state_factory)
        state_col.set_fixed_width(scaled(56))
        self.column_view.append_column(state_col)
        # What a screen reader announces. The headers are abbreviations on purpose, and PyGObject
        # does not expose GtkColumnViewColumn as an accessible object, so the names (and the long
        # forms, as tooltips) go on the header widgets the columns build.
        a11y(self.column_view, "player list, one row per player")
        spoken = {"✓": "include in the batch change", "ID": "player id", "NAME": "player name",
                  "CLUB": "club", "POS": "registered position", "STATE": "edited state"}
        header = self.column_view.get_first_child()
        for widget in tree(header) if header is not None else []:
            if isinstance(widget, Gtk.Label):
                text = widget.get_text()
            elif isinstance(widget, Gtk.Button):
                text = widget.get_label()
            else:
                continue
            if text in spoken:
                a11y(widget, spoken[text])
                widget.set_tooltip_text(spoken[text])
        scroller = Gtk.ScrolledWindow()
        # The three panes together want ~1970px, and a tiled window hands the app far less: each
        # pane scrolls on its own instead of squeezing the others' content out of view.
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_width(False)
        scroller.set_min_content_width(0)        # the pane's width is _size_panes' call, not the
        scroller.set_min_content_height(0)       # content's: otherwise one wide row sets it up
        scroller.set_child(self.column_view)
        scroller.set_vexpand(True)
        pane.append(scroller)
        previous = getattr(self, "count", None)          # a rebuilt page leaves its old count
        if previous is not None and previous.get_parent() is not None:   # behind in the footer,
            previous.get_parent().remove(previous)        # one copy per rebuild
        self.count = Gtk.Label(label="", xalign=0)
        self.count.add_css_class("hint")
        self.count.set_ellipsize(Pango.EllipsizeMode.END)   # its text was the pane's minimum
        self.count.set_max_width_chars(30)                  # ...and its natural, too
        self.count.set_margin_start(6)
        self.count.set_margin_end(6)
        footer = self.status.get_parent()
        if footer is not None:
            # in the footer, beside Revert: under the list it clipped the last row of a pane that
            # is exactly as tall as the window
            footer.insert_child_after(self.count, self.revert_btn)
        else:
            pane.append(self.count)
        return pane

    def _cell_setup(self, _f, item):
        label = Gtk.Label(xalign=0)
        label.add_css_class("cell")
        click = Gtk.GestureClick()
        click.connect("released", self._row_clicked, item)
        label.add_controller(click)
        item.set_child(label)

    def _cell_bind(self, _f, item, attr):
        row = item.get_item()
        label = item.get_child()
        value = getattr(row, attr)
        if attr == "state":
            # a dot, not the word: the count of pending edits lives in the footer
            label.set_text("")
            label.set_xalign(0.5)
            if value:
                label.add_css_class("dot")
            else:
                label.remove_css_class("dot")
            label.set_tooltip_text("edited — Save writes it" if value else None)
            label.update_property([Gtk.AccessibleProperty.LABEL],
                                  ["edited" if value else ""])
            return
        text = str(value)                    # ids read "26395", never "1,0…" elided
        empty = attr in ("club", "pos") and not text.strip()
        if empty:
            text = "free agent" if attr == "club" else "—"
            label.add_css_class("empty")
        else:
            label.remove_css_class("empty")
        label.set_text(text)
        # ten columns want more than the pane holds, so the view squeezes them: elide instead of
        # chopping the word, and keep the full value on hover
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_tooltip_text(text or None)
        label.set_xalign(1.0 if attr == "pid" else 0.0)
        label.set_width_chars(0)

    def _on_view_key(self, _c, keyval, _code, _state):
        """Space ticks the row under the cursor; Tab leaves the list instead of walking rows.

        The list can hold 26k rows: with every row a Tab stop the key was useless, so Tab moves on
        to the form (and back to the search) and the row under the cursor is ticked with Space.
        """
        if keyval in (Gdk.KEY_space, Gdk.KEY_KP_Space):
            row = self.selection.get_selected_item()
            if row is None:
                return False
            self._set_checked(row.index, row.index not in self.checked)
            return True
        if keyval == Gdk.KEY_Tab:
            # grab_focus, not set_focus: a Gtk.Entry reports focusable=False (its inner GtkText
            # owns the focus) and set_focus refuses such a widget outright
            self.name_entries.get("Name", self.search).grab_focus()
            return True
        if keyval == Gdk.KEY_ISO_Left_Tab:
            self.search.grab_focus()
            return True
        return False

    def _check_setup(self, _f, item):
        check = Gtk.CheckButton()
        check.set_halign(Gtk.Align.CENTER)
        check.set_focusable(False)          # the list is one Tab stop; Space ticks the cursor row
        a11y(check, "select this player for a bulk edit")
        click = Gtk.GestureClick()
        click.connect("released", self._row_clicked, item)
        check.add_controller(click)
        # connected once per cell: connecting in bind() adds a handler per re-bind, so one
        # toggle would run the body as many times as the row has been shown
        check.connect("toggled", self._on_check, item)
        item.set_child(check)

    def _check_bind(self, _f, item):
        check = item.get_child()
        row = item.get_item()
        self.binding = True
        check.set_active(row is not None and row.index in self.checked)
        self.binding = False

    def _on_check(self, check, item):
        if self.binding:
            return                                  # set_active() above, not a click
        row = item.get_item()
        if row is not None:
            self._set_checked(row.index, check.get_active())

    def _set_checked(self, index, on):
        """Tick one table row into the bulk selection — ticks are the user's alone."""
        if on:
            self.checked.add(index)
        else:
            self.checked.discard(index)
        if 0 <= index < len(self.rows):
            # a cell paints on a re-bind only, so splice this row and keep its edited flag
            pos = self.selection.get_selected()        # the cursor must survive the splice
            self._refresh_cells([index], edited=self.rows[index].edited)
            if pos is not None:
                self.selection.set_selected(pos)
        self._update_bulk()

    def selected_indexes(self):
        """Table indexes of every ticked row, in table order — the bulk pick, nothing else."""
        return sorted(self.checked)

    def _compare(self, a, b, attr):
        av, bv = getattr(a, attr), getattr(b, attr)
        return (av > bv) - (av < bv)

    def _refresh_cells(self, indexes, edited=True):
        """Re-bind the edited rows: a GtkColumnView binds an item once, so writing a property
        repaints nothing. Swapping the row object in the store is what refreshes the cell."""
        seen, batch = set(), []
        for i in sorted(indexes):
            if i in seen:
                continue
            seen.add(i)
            old = self.rows[i]
            t = self.table
            batch.append((i, Row(index=i, pid=old.pid, name=t.name(i), club=old.club,
                                 pos=self.position_name(t.get(i, "Position")),
                                 edited=edited)))
        for i, row in batch:
            self.rows[i] = row
            self.store.splice(i, 1, [row])      # replace in place: the view re-binds that cell
        self._update_count()

    # ---- data
    def load_rows(self):
        t = self.table
        for i in range(len(t)):
            pid = t.pid(i)
            rows = self.club_rows(pid)
            club = self.clubs.get(self.assign.team(rows[0]), "") if rows else ""
            self.rows.append(Row(index=i, pid=pid, name=t.name(i), club=club,
                                 pos=self.position_name(t.get(i, "Position")),
                                 ))

    def position_name(self, value):
        """The release's name for a stored position id; a stray value shows as itself."""
        names = self.mod.POSITION_NAMES
        return names[value] if 0 <= value < len(names) else str(value)

    def club_rows(self, pid):
        """The player's club assignment rows: national-team rows sit below `NATIONAL_MAX`.

        The country row often comes first, and "first row wins" then shows a nation where the
        club belongs (D. RANDOLPH read as IRELAND instead of TEESSIDE) — the same skew the
        PES 2021 editor fixes by skipping team ids under 100.
        """
        return [r for r in self.assign_by_pid.get(pid, ())
                if self.assign.team(r) > pesdb.NATIONAL_MAX]

    def populate(self):
        """Fill the list model — run after load_rows(), so the store matches the table.

        One `splice` for all rows: 26k `append()` calls were 5.3s of a 6.5s import, and the view
        only binds what is on screen anyway.
        """
        self.store.splice(0, self.store.get_n_items(), self.rows)
        self.column_view.set_model(self.selection)   # a rebuilt view starts with no model at all
        # a header click drives the view's own sorter; the sort model is what has to hold it
        self.sorted.set_sorter(self.column_view.get_sorter())
        self._update_count()
        self._load_first()

    def _matches(self, item, _data):
        q = self.search.get_text().strip().lower() if hasattr(self, "search") else ""
        if not q:
            return True
        if q.isdigit():
            return str(item.pid).startswith(q)
        return q in item.name.lower() or q in item.club.lower()

    def _open_first_match(self):
        """Enter in the search: drop the cursor on the first match, so the form shows it."""
        if not self.filtered.get_n_items():
            self._status("nothing matches that search")
            return
        row = self.filtered.get_item(0)
        self.selection.set_selected(0)                # the cursor follows, the ticks do not
        self.column_view.scroll_to(0, None, Gtk.ListScrollFlags.NONE, None)
        self._load(row.index)
        self._status(f"{self.filtered.get_n_items():,} match(es) · {self.table.name(row.index)} "
                     f"(id {self.table.pid(row.index)}) in the form")

    def _refilter(self):
        self.filter.changed(Gtk.FilterChange.DIFFERENT)
        self._update_count()
        self._load_first()

    def _update_count(self):
        shown = self.filtered.get_n_items()
        total = self.store.get_n_items()
        query = self.search.get_text().strip()
        if not shown and total and query:
            text = f'no player matches "{query}" — clear the search to see all {total:,}'
        else:
            text = f"{shown:,} of {total:,} players — type to narrow"
        self.count.set_text(text)
        self.count.set_tooltip_text(text)

    def _load_first(self):
        """Load row 0 into the form: the detail pane must not sit empty.

        It is *not* selected — the form follows the cursor, the bulk pick follows the ticks,
        and a ticked row must mean the user ticked it.
        """
        if self.index is None and self.sorted.get_n_items():
            row = self.sorted.get_item(0)
            if row is not None:
                self._load(row.index)
        self._update_bulk()
        return False

    def _on_select(self, sel, _pos, _n):
        """The current row drives the form: arrows and plain clicks move it.

        `_pos` is *not* the row that was clicked — GTK reports the previous position — so the
        row comes from the model instead. Ticks never touch this selection.
        """
        row = sel.get_selected_item()
        if row is not None and row.index != self.index:
            self._load(row.index)
        self._update_bulk()

    def _row_clicked(self, _gesture, _n_press, _x, _y, item):
        """Show the row that was clicked, whatever the selection was doing."""
        self.show_row(item.get_position())

    def show_row(self, pos):
        """Show the player at view position `pos` (a click, or a check being ticked)."""
        row = self.sorted.get_item(pos) if pos != Gtk.INVALID_LIST_POSITION else None
        if row is not None and row.index != self.index:
            self._load(row.index)
