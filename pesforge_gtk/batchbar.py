"""The batch tray: one operation across every ticked player, staged in memory.

The tray is a sibling of the panes and hidden until something is ticked; it edits the
table's own rows (through the editing mixin's plumbing), clamps to the field's range and
leaves the commit to Save.
"""
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango  # noqa: E402

from .reference import bulk_fields, field_range  # noqa: E402
from .widgets import a11y, spin_steps  # noqa: E402




class BatchBarMixin:
    def _batch_bar(self):
        """The mockup's tray: one operation across every ticked player, staged in memory."""
        self.batch = Gtk.Box(spacing=8)
        self.batch.add_css_class("batch")
        self.batch.set_margin_start(14)
        self.batch.set_margin_end(14)
        self.batch.set_margin_top(2)
        self.batch_label = Gtk.Label(label="", xalign=0)
        self.batch_label.add_css_class("hint")
        self.batch_label.set_ellipsize(Pango.EllipsizeMode.END)  # never widens the tray
        self.batch.append(self.batch_label)
        self.bulk_field = Gtk.DropDown.new_from_strings(bulk_fields(self.mod))
        self.bulk_field.set_tooltip_text("field to change")
        a11y(self.bulk_field, "field to change across the ticked players")
        self.bulk_field.set_size_request(186, -1)
        self.bulk_field.connect("notify::selected", self._update_bulk_range)
        self.batch.append(self.bulk_field)
        self.bulk_op = Gtk.DropDown.new_from_strings([label for label, _ in self.BULK_OPS])
        a11y(self.bulk_op, "operation to apply to the field")
        self.bulk_op.set_selected(1)
        self.batch.append(self.bulk_op)
        self.bulk_amount = Gtk.SpinButton.new_with_range(-99, 99, 1)
        spin_steps(self.bulk_amount)
        a11y(self.bulk_amount, "amount for the batch operation")
        self.bulk_amount.set_value(1)
        self.batch.append(self.bulk_amount)
        apply_btn = Gtk.Button(label="Apply to selected")
        apply_btn.add_css_class("accent")
        apply_btn.connect("clicked", lambda _b: self._bulk_apply())
        self.batch.append(apply_btn)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        self.batch.append(spacer)
        clear = Gtk.Button(label="Clear selection")
        clear.connect("clicked", lambda _b: self.selection.unselect_all())
        self.batch.append(clear)
        self.batch.set_visible(False)
        self._bulk_widgets = (self.bulk_field, self.bulk_op, self.bulk_amount, apply_btn)
        return self.batch

    def _range(self, label):
        """(low, high) for a field: the form's own range where one is declared, else the bits."""
        lo, hi = field_range(self.mod, label)
        if label in self.BIO_RANGES:
            lo, hi = max(lo, self.BIO_RANGES[label][0]), min(hi, self.BIO_RANGES[label][1])
        return lo, hi

    def _update_bulk_range(self, *_a):
        """The amount follows the field: "Set to" reaches the field's whole range.

        The spin was ±99, so "Set to Height 200" (or Nationality 511) was unreachable.
        """
        if not hasattr(self, "bulk_amount"):
            return
        label = bulk_fields(self.mod)[self.bulk_field.get_selected()]
        lo, hi = self._range(label)
        self.bulk_amount.set_range(min(lo, -99), max(hi, 99))

    def _update_bulk(self):
        self._update_badge()
        n = len(self.selected_indexes())
        self.batch.set_visible(bool(n))
        if n:
            self.batch_label.set_text(f"{n} selected · {len(self.dirty)} pending edit(s)")

    def _bulk_apply(self):
        """Set, add or subtract one field across every ticked player, clamped to its range."""
        indexes = self.selected_indexes()
        if not indexes:
            return
        label = bulk_fields(self.mod)[self.bulk_field.get_selected()]
        op = self.BULK_OPS[self.bulk_op.get_selected()][1]
        amount = int(self.bulk_amount.get_value())
        lo, hi = self._range(label)
        changes = []
        for i in indexes:
            old = self.table.get(i, label)
            new = amount if op == "set" else (old + amount if op == "add" else old - amount)
            new = max(lo, min(hi, new))
            if new == old:
                continue                        # clamped back to what it already was
            changes.append((i, label, old, new))
            self.table.set(i, label, new)
            self._mark_dirty(i, label, refresh=False)
        self._refresh_cells(indexes)
        self._push_step(changes)
        if self.index in indexes:
            self._load(self.index)              # the form shows the current player: reload it
        done = f"set {label} to {max(lo, min(hi, amount))}" if op == "set" \
            else f"{op} {amount:+d} {label}"
        self._status(f"{done} on {len(indexes)} player(s) · "
                     f"{len(self.dirty)} pending edit(s) · Save writes it")
        self._update_bulk()
