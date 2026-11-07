"""The editor panes: the player card, the ability grid, BIO, skills and styles.

Everything here derives from the loaded release's map module (`self.mod`): ability order,
bio rows and their ranges, positions, playing styles and the skill chips — one panel,
every release.
"""
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango  # noqa: E402

from .reference import (  # noqa: E402
    ABILITY_GROUPS, ability_group, country_name, field_range, grouped_ability_order,
    style_names,
)
from .widgets import (  # noqa: E402
    AbilityBar, a11y, dim_label, ellipsizing_dropdown, fit_field_labels, select_all_on_focus,
    slim_spin,
    spin_steps, set_widget_value,
)




class FormPaneMixin:
    def _form(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("pane")
        box.set_hexpand(True)
        box.set_margin_top(0)
        # Rows are as tall as a spinbutton whatever the window does, so a narrow window gets more
        # columns instead of more rows and drops the value bars: that is what makes the whole
        # panel fit the window the user actually has. `self.narrow` is set in _build_panes.
        narrow = getattr(self, "narrow", False)
        scroller = Gtk.ScrolledWindow()      # narrow windows both panes share a column and scroll
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_width(False)
        scroller.set_min_content_width(0)    # the pane's width is _size_panes' call, not its rows'
        scroller.set_vexpand(True)
        box.append(scroller)
        # spacing 3 / margins 4-6: with the theme's 34px spinbuttons the
        # section stack is the tallest thing in the editor, and these few pixels are what make
        # the panes fit 941x1030 without a scrollbar (measured, see docs/testing.md)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        for m, side in ((8, "start"), (8, "end"), (4, "top"), (6, "bottom")):
            inner.set_margin_start(m) if side == "start" else (
                inner.set_margin_end(m) if side == "end" else
                inner.set_margin_top(m) if side == "top" else inner.set_margin_bottom(m))
        scroller.set_child(inner)

        inner.append(self._section("PLAYER", ""))
        self.hero = Gtk.Label(label="no player", xalign=0)
        self.hero.add_css_class("hero")
        # ellipsized like the meta line: an unconstrained label reports its whole text as its
        # minimum width, and a long player name would then be what decides whether the editor
        # fits beside the list (the layout measures the panes' content)
        self.hero.set_ellipsize(Pango.EllipsizeMode.END)
        inner.append(self.hero)
        self.meta = Gtk.Label(label="", xalign=0)
        self.meta.add_css_class("meta")
        self.meta.set_ellipsize(Pango.EllipsizeMode.END)
        inner.append(self.meta)

        pills = Gtk.Box(spacing=6)
        self.pills = {}
        for key in ("OVR", "Height", "Weight", "Age", "Foot"):
            p = Gtk.Label(label=f"{key} —")
            p.add_css_class("pill")
            pills.append(p)
            self.pills[key] = p
        inner.append(pills)

        self.name_entries = {}
        self.name_slots = {}
        slots = getattr(self.mod, "NAME_SLOTS",
                       (("Name", "full"), ("Shirt Name", "shirt"), ("Japanese Name", "japanese")))
        nrow = Gtk.Grid(column_spacing=10, row_spacing=0) if narrow else None
        name_label_w, name_label_step = fit_field_labels(
            [k for k, _off in slots], 11 if narrow else 15, budget=560, parent=inner, pad=0,
            gap=10, box=160, cols=2 if narrow else 1)   # one width, or the entries drift
        #   and the labels' own naturals must fit it, or a long slot name shifts its entry
        for i, (key, off) in enumerate(slots):
            lab = dim_label(key, name_label_w, 11 if narrow else 15, scale=name_label_step)
            e = Gtk.Entry()
            e.set_hexpand(True)
            e.set_tooltip_text(f"up to {self._name_limit()} bytes")
            a11y(e, f"{key}, up to {self._name_limit()} bytes")
            e.connect("changed", self._on_name_changed, off)
            self.name_slots[key] = off
            self.name_entries[key] = e
            if nrow is not None:              # short window: pair the name fields up
                nrow.attach(lab, (i % 2) * 2, i // 2, 1, 1)
                nrow.attach(e, (i % 2) * 2 + 1, i // 2, 1, 1)
            else:
                row = Gtk.Box(spacing=6)
                row.append(lab)
                row.append(e)
                inner.append(row)
        if nrow is not None:
            inner.append(nrow)

        inner.append(self._section("ABILITIES",
                                   " · ".join(name for name, _labels in ABILITY_GROUPS)))
        self.abilities = {}
        cols = 3 if narrow else 2
        # label 110 + spacing 4 in the grid: 16px less than the old 114/6, which is the
        # difference between the form pane fitting beside the list at a 917px window (the
        # size a tiled window gets on this desktop) and being 13px short of it
        grid = Gtk.Grid(column_spacing=2, row_spacing=0)
        order = grouped_ability_order(self.mod)
        # 575 = the 591px form pane at 917x1030 minus its 16px of margins: the narrowest a
        # split pane gets, so the grid is sized for the layout's own promise
        ability_label_w, ability_label_step = fit_field_labels(
            [*order, "Position"], 20, budget=575, parent=inner, pad=1, cols=cols)
        self.ability_label_step = ability_label_step     # the align check reports it
        for n, label in enumerate(order):
            cell = Gtk.Box(spacing=6)
            cell.append(dim_label(label, ability_label_w, 20, scale=ability_label_step))
            # the range is the release's own: 40..99 from the six-bit +40 maps, 0..99 on the
            # classic map (its stats are stored as the number the game shows)
            lo, hi = field_range(self.mod, label)
            spin = Gtk.SpinButton.new_with_range(lo, hi, 1)
            spin.set_width_chars(4)          # without this the entry reserves ~112px
            spin.set_size_request(72, 28)    # one box size for every value control in the grid
            spin.set_tooltip_text(f"{label} · {ability_group(label)} · {int(lo)} to {int(hi)}")
            a11y(spin, f"{label}, {ability_group(label)}, {int(lo)} to {int(hi)}")
            select_all_on_focus(spin)
            spin_steps(spin)
            if narrow:
                slim_spin(spin)
            spin.connect("value-changed", self._on_ability_changed, label)
            cell.append(spin)
            if not narrow:                   # the wide layout has room for the value bars
                bar = AbilityBar(42, lo)     # its 0 sits on the release's own floor
                cell.append(bar)
                self.abilities[label] = (spin, bar)
            else:
                self.abilities[label] = (spin, None)
            grid.attach(cell, n % cols, n // cols, 1, 1)
        pos_cell = Gtk.Box(spacing=6)
        pos_cell.append(dim_label("Position", ability_label_w, 20, scale=ability_label_step))
        self.position = ellipsizing_dropdown(self.mod.POSITION_NAMES, 5)   # "DMF" fits, its popup keeps the long form
        a11y(self.position, "registered position")
        self.position.set_size_request(72, 28)                      # same box as the value fields
        self.position.set_hexpand(False)                            # or it outgrows its column
        self.position.connect("notify::selected", self._on_position_changed)
        pos_cell.append(self.position)
        grid.attach(pos_cell, len(order) % cols, len(order) // cols, 1, 1)
        band = Gtk.Box(spacing=12)
        band.append(grid)
        if getattr(self, "roomy", False):
            band.append(self._profile())
        inner.append(band)
        return box

    def _profile(self):
        """The abilities at a glance: one bar per group, drawn right of the grid.

        The grid already holds every number; this shows the shape of them — four rows, each the
        mean of its group. Reuses AbilityBar, so the colours match the values elsewhere.
        """
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        box.set_valign(Gtk.Align.START)
        head = Gtk.Label(label="PROFILE", xalign=0)
        head.add_css_class("dim")
        box.append(head)
        self.profile = {}
        for name, _labels in ABILITY_GROUPS:
            row = Gtk.Box(spacing=6)
            tag = Gtk.Label(label=name[:3].upper(), xalign=0)
            tag.add_css_class("dim")
            tag.set_size_request(32, -1)
            bar = AbilityBar(64)
            bar.set_hexpand(True)
            bar.set_tooltip_text(f"{name} average")
            a11y(bar, f"{name} average")
            value = Gtk.Label(label="—", xalign=1)
            value.add_css_class("dim")
            value.set_size_request(20, -1)
            row.append(tag)
            row.append(bar)
            row.append(value)
            box.append(row)
            self.profile[name] = (bar, value)
        return box

    def _bio(self):
        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        pane.add_css_class("pane")
        pane.set_vexpand(True)
        box = Gtk.ScrolledWindow()          # the safety net, like the app's own scroll panes
        box.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        box.set_propagate_natural_width(False)
        box.set_min_content_width(0)        # the pane's width is _size_panes' call, not its rows'
        box.set_vexpand(True)
        pane.append(box)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        inner.set_margin_top(4)
        inner.set_margin_bottom(6)
        inner.set_margin_start(8)
        inner.set_margin_end(8)
        box.set_child(inner)
        inner.append(self._section("BIO", ""))

        self.bio = {}
        roomy = self.roomy                        # three narrow columns, or two roomy ones
        pairs = []

        # PES 2015 stores the same style ids as 2019 (0 None, 17 Defensive Goalkeeper) but its
        # map carries no names, so it borrows the shared list; a map with STYLE_NAMES = None
        # (PES 5) numbers the style field, and gets the spinbutton below.
        names = style_names(self.mod)
        if names:
            self.style = ellipsizing_dropdown(names)       # roomy: no truncation, the box is the longest style
            a11y(self.style, "playing style")
            self.style.connect("notify::selected", self._on_style_changed)
        else:                                 # a release without names: its id in a spinbox
            lo, hi = field_range(self.mod, "Playing Style")
            self.style = Gtk.SpinButton.new_with_range(lo, hi, 1)
            a11y(self.style, "playing style")
            self.style.connect("value-changed", self._on_bio_changed, "Playing Style")
        self.foot = ellipsizing_dropdown(("Right", "Left"), 6)
        a11y(self.foot, "preferred foot")
        self.foot.connect("notify::selected", self._on_foot_changed)

        # Every field goes into ONE grid: the style and foot dropdowns used to sit on their own
        # line below it, so their labels started at a different x than the rows above and the
        # boxes looked like they were floating. The grid sizes each value column to its widest
        # member, so the boxes line up and a country name is never cut short.
        # A release's own bio rows and ranges win: PES 5's height stops at 211 cm and its
        # weak-foot fields run 1..8 where the CPK releases' run 1..4; the window's lists are
        # the fallback every other vintage uses.
        bio_labels = getattr(self.mod, "BIO_LABELS", None) or self.BIO_LABELS
        bio_ranges = getattr(self.mod, "BIO_RANGES", None) or self.BIO_RANGES
        fields = [(label, bio_ranges[label]) for label in bio_labels
                  if label in self.mod.FIELD_BY_LABEL and label in bio_ranges]
        # style and foot follow the last row's dropdown, as in the game's own panel
        fields += [("Playing Style", None), ("Foot", None)]
        for label, span in fields:
            key = label
            if label == "Playing Style":
                widget = self.style
            elif label == "Foot":
                widget = self.foot
            elif key == "Nationality":
                # a code, not a number: offer the country itself. The list is the whole code
                # range, so the selected index *is* the stored value — nothing to drift.
                span_n = len(self.mod.NATION_NAMES) if hasattr(self.mod, "NATION_NAMES") else 512
                widget = ellipsizing_dropdown([country_name(self.mod, c) for c in range(span_n)],
                                              12)                        # 7 cut "Portugal" in half
                if hasattr(widget, "set_enable_search"):
                    widget.set_enable_search(True)
            elif key in getattr(self.mod, "CHOICE_FIELDS", {}):
                # a stored code whose meaning is a word (PES 5's injury tolerance is C/B/A)
                widget = ellipsizing_dropdown(self.mod.CHOICE_FIELDS[key], 5)
            else:
                lo, hi = span
                widget = Gtk.SpinButton.new_with_range(lo, hi, 1)
                widget.set_width_chars(4)
                if not roomy:
                    slim_spin(widget)
            if isinstance(widget, Gtk.DropDown):
                # notify::selected hands over the pspec too; the handler wants (widget, key)
                widget.connect("notify::selected",
                               lambda dd, _p, k=key: self._on_bio_changed(dd, k))
            else:
                widget.connect("value-changed", self._on_bio_changed, key)
            if span is not None:
                widget.set_tooltip_text(f"{label} · {span[0]} to {span[1]}")
                a11y(widget, f"{label}, {span[0]} to {span[1]}")
            if isinstance(widget, Gtk.SpinButton):
                select_all_on_focus(widget)
                spin_steps(widget)
            self.bio[key] = widget
            pairs.append((label, widget))

        grid = Gtk.Grid(column_spacing=6, row_spacing=1)
        ncols = 2 if roomy else 3
        for i, (text, widget) in enumerate(pairs):
            lab = dim_label(text, 128 if roomy else 114, 20)
            widget.set_hexpand(False)
            widget.set_halign(Gtk.Align.FILL)   # one width per column: no box floats
            if widget.get_tooltip_text() is None:
                widget.set_tooltip_text(text)
            grid.attach(lab, (i % ncols) * 2, i // ncols, 1, 1)
            grid.attach(widget, (i % ncols) * 2 + 1, i // ncols, 1, 1)
        inner.append(grid)

        self.chip_filter = Gtk.SearchEntry()
        self.chip_filter.add_css_class("chipfilter")
        self.chip_filter.set_placeholder_text("filter skills and styles…")
        a11y(self.chip_filter, "filter the skill and playing-style chips by name")
        self.chip_filter.set_tooltip_text("show only the chips whose name matches")
        self.chip_filter.connect("search-changed", lambda _e: self._filter_chips())
        inner.append(self._section("SKILLS & COM STYLES", self.chip_filter))
        # A grid, not a FlowBox: the flow reports six widest-in-a-row as its minimum width, which
        # no amount of font shrinking fixed, and a grid's columns are the pane's to divide.
        # Seven columns in the compact shape: 46 chips land in 7 rows instead of 8, which is the
        # difference between the bio pane fitting its page at 941x1030 and scrolling (measured).
        cols = 7 if getattr(self, "narrow", False) else 4
        grid2 = Gtk.Grid(column_spacing=2, row_spacing=1)
        self.chips = {}
        # the modules key their skill/style flags as "Skill: X" / "Style: X"; the chip shows
        # the bare name, the table lookup uses the label
        for n, label in enumerate([f"Style: {x}" for x in self.mod.COM_STYLES]
                                  + [f"Skill: {x}" for x in self.mod.SKILLS]):
            chip = Gtk.ToggleButton(label=label.split(": ", 1)[1])
            chip.add_css_class("chip")
            chip.set_hexpand(True)
            text = chip.get_child()
            if text is not None:              # long names elide instead of widening the column
                text.set_ellipsize(Pango.EllipsizeMode.END)
            chip.set_tooltip_text(label.split(": ", 1)[1])
            a11y(chip, f"toggle {label.split(': ', 1)[1]}")
            self._chip_text(chip, False, chip.get_tooltip_text())
            chip.connect("toggled", self._on_skill_toggled, label)
            grid2.attach(chip, n % cols, n // cols, 1, 1)
            self.chips[label] = chip
        inner.append(grid2)
        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        inner.append(spacer)
        return pane

    def _section(self, text, hint):
        """A section band: its title, and a hint that may be a string or a widget."""
        box = Gtk.Box(spacing=8)
        box.add_css_class("section-row")
        lab = Gtk.Label(label=f"▼ {text}", xalign=0)
        lab.add_css_class("section")
        box.append(lab)
        if isinstance(hint, Gtk.Widget):
            hint.set_hexpand(True)
            hint.set_halign(Gtk.Align.END)
            box.append(hint)
        elif hint:
            h = Gtk.Label(label=hint, xalign=1)
            h.add_css_class("hint")
            h.set_hexpand(True)
            # the ability-group hint is a long string; unconstrained it is the widest thing in
            # the editor and would decide the layout on its own, so it elides with a tooltip
            h.set_ellipsize(Pango.EllipsizeMode.END)
            h.set_tooltip_text(hint)
            box.append(h)
        return box

    def _chip_text(self, chip, active, name=None):
        """A chosen chip carries a tick; colour alone must not be the only difference."""
        text = chip.get_child()
        if text is None:
            return
        if name is None:
            name = getattr(chip, "_chip_name", None) or chip.get_tooltip_text() or ""
        chip._chip_name = name
        text.set_text(f"✓ {name}" if active else name)

    def _filter_chips(self):
        """Narrow the chip grid to the names that match the filter box."""
        query = self.chip_filter.get_text().strip().lower()
        for recorded, chip in self.chips.items():
            chip.set_visible(not query or query in recorded.lower())
