"""Form <-> record: loading a player into the panels, pending edits, undo/redo.

Every write to the table goes through here. A handler captures the old value, returns early
when nothing changed, records the field's pristine value once (`originals`, from the step
itself, never from the table after the write), pushes one undo step per user action and
marks the row dirty. A field that lands back on its pristine value stops counting as an
edit, so a value retyped through undo asks nothing on close.
"""
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

import pesdb  # noqa: E402

from .reference import (  # noqa: E402
    ABILITY_GROUPS, COUNTRIES, STYLE_NAMES_FALLBACK, overall_rating, tier_of,
)
from .widgets import set_widget_value, widget_value  # noqa: E402




class EditingMixin:
    # ---- form <-> record
    def _load(self, index):
        if index is None:
            return
        self.index = index
        t = self.table
        self.loading = True
        self.hero.set_text(t.name(index))
        self.meta.set_text(self._meta_text(index))
        self._paint_ovr(index)
        self._paint_profile(index)
        for key in ("Height", "Weight", "Age"):
            self.pills[key].set_text(f"{key} {t.get(index, key)}")
        self.pills["Foot"].set_text("Foot " + ("Left" if t.get(index, "Foot") else "Right"))
        for key, getter in (("Name", t.name), ("Shirt Name", t.shirt), ("Japanese Name", t.japanese)):
            entry = self.name_entries.get(key)     # a release without that slot has no row
            if entry is not None:
                entry.set_text(getter(index))
        for label, (spin, bar) in self.abilities.items():
            value = t.get(index, label)
            spin.set_value(value)
            if bar is not None:               # the narrow layout drops the bars
                bar.set_value(value)
        self.position.set_selected(t.get(index, "Position"))
        for key, spin in self.bio.items():
            set_widget_value(spin, t.get(index, key))
        style = t.get(index, "Playing Style")
        if hasattr(self.style, "set_selected"):          # a named list, wherever it came from
            names = getattr(self.mod, "STYLE_NAMES", None) or STYLE_NAMES_FALLBACK
            style = min(style, len(names) - 1)
            self.style.set_selected(style)
            self.style.set_tooltip_text(names[style] if style < len(names) else "")
        else:                                            # 2015 numbers them
            self.style.set_value(style)
        self.foot.set_selected(t.get(index, "Foot"))
        for label, chip in self.chips.items():
            chip.set_active(bool(t.get(index, label)))
            self._chip_text(chip, bool(t.get(index, label)))
        for label, (spin, _bar) in self.abilities.items():
            self._decorate(spin, label, rating=True)
        for key, widget in self.bio.items():
            self._decorate(widget, key)
        for display, entry in self.name_entries.items():
            self._decorate(entry, "name:" + self.name_slots.get(display, display))
        self.loading = False
        self._status(f"{len(self.rows):,} players · showing {t.name(index)} "
                     f"(id {t.pid(index)})"
                     + (f" · {len(self.dirty)} edited" if self.dirty else ""))

    # ---- commit
    def _edited_values(self):
        """pid -> {field: value}, the form's pending edits keyed by what the game matches on."""
        edits = {}
        for i, key in sorted(self.dirty):
            pid = self.table.pid(i)
            value = (self.table.name(i) if key == "name:full" else
                     self.table.shirt(i) if key == "name:shirt" else
                     self.table.japanese(i) if key == "name:japanese" else
                     self.table.get(i, key))
            edits.setdefault(pid, {})[key] = value
        return edits

    @staticmethod
    def _reads_back(table, pid, fields):
        """True when a patched table really carries the edited values for that player."""
        for i in table.find(str(pid)):
            if all((table.name(i) if k == "name:full" else
                    table.shirt(i) if k == "name:shirt" else
                    table.japanese(i) if k == "name:japanese" else
                    table.get(i, k)) == v for k, v in fields.items()):
                return True
        return False

    def _push_step(self, changes):
        """One user action as one undo step — a bulk edit reverses in one go.

        This is where a field's pristine value is remembered, once, from the step itself: it is
        what the "was …" tooltip shows and what lets an edit that lands back on it stop being an
        edit. Reading the value after the write would remember the new one.
        """
        if not changes:
            return
        for index, key, old, _new in changes:
            self.originals.setdefault((index, key), old)
        self.undo_stack.append(changes)
        del self.undo_stack[:-100]
        self.redo_stack.clear()

    def _value(self, index, key):
        """The stored value of an edit key: a field label, or `name:slot`."""
        if key == "name:full":
            return self.table.name(index)
        if key == "name:shirt":
            return self.table.shirt(index)
        if key == "name:japanese":
            return self.table.japanese(index)
        return self.table.get(index, key)

    def _set_value(self, index, key, value):
        if key.startswith("name:"):
            self.table.set_name(index, **{key.split(":", 1)[1]: value})
        else:
            self.table.set(index, key, value)

    def _undo(self, *_a):
        """Ctrl+Z: reverse the last edit step."""
        if not self.undo_stack:
            self._status("nothing to undo")
            return True
        self._apply_step(self.undo_stack.pop(), back=True)
        return True

    def _redo(self, *_a):
        """Ctrl+Y / Ctrl+Shift+Z: reapply the step Ctrl+Z took back."""
        if not self.redo_stack:
            self._status("nothing to redo")
            return True
        self._apply_step(self.redo_stack.pop(), back=False)
        return True

    def _apply_step(self, step, back):
        """Move every change in a step to its other side and repaint the rows it touched."""
        if back:
            self.redo_stack.append(step)
        else:
            self.undo_stack.append(step)
        for index, key, old, new in step:
            self._set_value(index, key, old if back else new)
            self._mark_dirty(index, key, refresh=False)
        self._refresh_cells(sorted({index for index, _k, _o, _n in step}))
        if self.index is not None and any(index == self.index for index, _k, _o, _n in step):
            self._load(self.index)
        self._status(f"{'undone' if back else 'redone'} · {len(self.dirty)} pending edit(s) "
                     f"· Save writes it")

    def _mark_dirty(self, index, label, refresh=True):
        """Record a pending edit and repaint its row: a mutated Row paints nothing by itself.

        A field that comes back to the value the game holds (an undo, a retyped number) stops
        counting as an edit.
        """
        original = self.originals.get((index, label))
        if original is not None and original == self._value(index, label):
            self.dirty.discard((index, label))
        else:
            self.dirty.add((index, label))
        self._ovr_cache.pop(index, None)          # the rating is built from these values
        if index == self.index:
            self._paint_ovr(index)
            self._paint_profile(index)
        if refresh:
            self._refresh_cells([index])          # the STATE cell only repaints on a re-bind
        else:
            self.rows[index].edited = True        # the bulk path re-splices them in one go
        self._status(f"{len(self.rows):,} players · {len(self.dirty)} pending edit(s) "
                     f"· Save writes it")
        if hasattr(self, "batch"):
            self._update_bulk()

    def _decorate(self, widget, key, rating=False):
        """A field's state: rating tint, the modified marker, the original in the tooltip."""
        base = getattr(widget, "_base_tooltip", None) or widget.get_tooltip_text() or key
        widget._base_tooltip = base
        if rating:
            for n in range(4):
                widget.remove_css_class(f"tier{n}")
            widget.add_css_class(f"tier{tier_of(widget_value(widget))}")
        original = self.originals.get((self.index, key))
        value = self._value(self.index, key) if self.index is not None else None
        if original is not None and original != value:
            widget.add_css_class("edited")
            widget.set_tooltip_text(f"{base} · was {original}")
        else:
            widget.remove_css_class("edited")
            widget.set_tooltip_text(base)

    def _on_ability_changed(self, spin, label):
        if self.loading or self.index is None:
            return
        value = int(spin.get_value())
        old = self.table.get(self.index, label)
        if old == value:
            return
        self.table.set(self.index, label, value)
        self._push_step([(self.index, label, old, value)])
        self._decorate(spin, label, rating=True)
        bar = self.abilities[label][1]
        if bar is not None:
            bar.set_value(value)
        self._mark_dirty(self.index, label)

    def _paint_ovr(self, index):
        rating = self.ovr(index)
        self.pills["OVR"].set_text(f"OVR {rating}")
        for n in range(4):
            self.pills["OVR"].remove_css_class(f"tier{n}")
        self.pills["OVR"].add_css_class(f"tier{tier_of(rating)}")

    def _paint_profile(self, index):
        """One bar per ability group: the mean of the abilities this release actually has."""
        if not getattr(self, "profile", None):
            return
        t = self.table
        for name, labels in ABILITY_GROUPS:
            have = [t.get(index, lab) for lab in labels if lab in self.mod.FIELD_BY_LABEL]
            value = round(sum(have) / len(have)) if have else 0
            bar, text = self.profile[name]
            bar.set_value(value)
            text.set_text(str(value) if value else "—")

    def ovr(self, index):
        """The player's reference rating, cached per record (see `overall_rating`)."""
        cache = getattr(self, "_ovr_cache", None)
        if cache is None:
            cache = self._ovr_cache = {}
        if index not in cache:
            cache[index] = overall_rating(self.table, index, self.mod)
        return cache[index]

    def _meta_text(self, index):
        """The line under the name: id, club, country, how many packs were searched."""
        pid = self.table.pid(index)
        rows = self.club_rows(pid)
        club = self.clubs.get(self.assign.team(rows[0]), "free agent") if rows else "free agent"
        number = self.assign.number(rows[0]) if rows else 0
        return (f"id {pid} · {club} (#{number}) · "
                f"{COUNTRIES.get(self.table.get(index, 'Nationality'), '')} · "
                f"{len(self.packs)} packs searched")

    def _on_bio_changed(self, spin, key):
        if self.loading or self.index is None:
            return
        value = widget_value(spin)
        old = self.table.get(self.index, key)
        if old == value:
            return
        self.table.set(self.index, key, value)
        self._push_step([(self.index, key, old, value)])
        self._decorate(spin, key)
        if key in ("Height", "Weight", "Age"):
            self.pills[key].set_text(f"{key} {int(spin.get_value())}")
        if key == "Nationality":               # the country name lives in the meta line
            self.meta.set_text(self._meta_text(self.index))
        self._mark_dirty(self.index, key)

    def _on_position_changed(self, dd, _p):
        if self.loading or self.index is None:
            return
        old = self.table.get(self.index, "Position")
        if old == dd.get_selected():
            return
        self.table.set(self.index, "Position", dd.get_selected())
        self._push_step([(self.index, "Position", old, dd.get_selected())])
        row = self.rows[self.index]
        row.pos = self.mod.POSITION_NAMES[dd.get_selected()]
        self._mark_dirty(self.index, "Position")

    def _on_style_changed(self, dd, _p):
        if self.loading or self.index is None:
            return
        old = self.table.get(self.index, "Playing Style")
        if old == dd.get_selected():
            return
        self.table.set(self.index, "Playing Style", dd.get_selected())
        self._push_step([(self.index, "Playing Style", old, dd.get_selected())])
        self._mark_dirty(self.index, "Playing Style")

    def _on_foot_changed(self, dd, _p):
        if self.loading or self.index is None:
            return
        old = self.table.get(self.index, "Foot")
        if old == dd.get_selected():
            return
        self.table.set(self.index, "Foot", dd.get_selected())
        self._push_step([(self.index, "Foot", old, dd.get_selected())])
        self.pills["Foot"].set_text("Foot " + ("Left" if dd.get_selected() else "Right"))
        self._mark_dirty(self.index, "Foot")

    def _on_skill_toggled(self, chip, label):
        if self.loading or self.index is None:
            return
        value = 1 if chip.get_active() else 0
        old = self.table.get(self.index, label)
        if old == value:
            return
        self.table.set(self.index, label, value)
        self._push_step([(self.index, label, old, value)])
        self._chip_text(chip, bool(value))
        self._mark_dirty(self.index, label)

    def _name_limit(self):
        """The game's name slot in UTF-8 bytes — what it will actually keep."""
        return int(getattr(self.mod, "NAME_SLOT",
                           getattr(self.mod, "NAME_LEN",
                                   getattr(pesdb, "NAME_SLOT", 61)))) - 1

    def _on_name_changed(self, entry, slot):
        if self.loading or self.index is None:
            return
        text = entry.get_text()
        limit = self._name_limit()
        if len(text.encode("utf-8")) > limit:
            # the game stores a fixed slot and cuts the rest: say so instead of losing it quietly
            entry.add_css_class("invalid")
            entry.set_tooltip_text(f"too long — the game stores {limit} bytes and cuts the rest")
        else:
            entry.remove_css_class("invalid")
            entry.set_tooltip_text(f"up to {limit} bytes")
        old = self._value(self.index, "name:" + slot)
        if old == text:
            return
        self.table.set_name(self.index, **{slot: text})
        self._push_step([(self.index, "name:" + slot, old, text)])
        self._decorate(entry, "name:" + slot)
        row = self.rows[self.index]
        row.name = self.table.name(self.index)
        self._mark_dirty(self.index, f"name:{slot}")
