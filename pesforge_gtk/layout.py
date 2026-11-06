"""Layout policy: the panes' arrangement, decided from the live allocation.

GTK4 has no size signal, so the decision is taken in `do_size_allocate` (window.py) and the
policy lives here. One structure — the list beside a stacked form+BIO, or the list as a strip
above the editor — is only *turned*; which way is decided from measurements, never from a
guessed pixel threshold:

* the list's width comes from its own column policy (which columns the width on offer can pay
  for), and the pane is never handed less width than the columns it is showing;
* the editor's minimum width comes from `Gtk.Widget.measure()` on its own content, so a larger
  text scale or longer wording moves the boundary by itself;
* the vertical signal is the panes' own scroll adjustments — the same predicate GTK uses to
  decide to draw a scrollbar. The panes sit in scrollers whose minimum is 0 by construction, so
  "is it scrolling" is the only honest height measurement available; the height at which the
  split layout was last seen costing that scroll is remembered and used as an explicit,
  symmetric band (`tight below H`), so a short window keeps the strip layout and a tall one
  returns to the split without the two states chattering.

The other remembered figure is `_editor_min_w`: the largest editor minimum width seen *while
side by side*. Measured while stacked it is inflated (the form then has the whole window, so
its labels stop wrapping), so judging the way back with it is what stops the split layout
becoming a one-way door — and both crossings use the same figure.

The `do_size_allocate` vfunc itself stays on the window class (window.py): PyGObject only
installs a `do_*` override that is defined on the class, not one inherited from a mixin —
a mixin copy is simply never called, and the layout silently freezes at whatever GTK
allocated first.
"""
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .listpane import LIST_MAX, LIST_MIN_H  # noqa: E402
from .widgets import scaled  # noqa: E402

LIST_WANTED_MIN = 260        # a list narrower than this shows a name and little else
GAP = 8                      # the box spacing between the list and the editor
SHARE = 0.32                 # the list's share of the width while side by side
SETTLE_FRAMES = 3            # allocations a split-layout overflow must hold before it counts


class LayoutMixin:
    def _apply_column_policy(self, pane_w):
        """Show only the columns this pane width can pay for; returns their total width.

        A GtkColumnView squeezes its columns when the pane is narrower than their sum, which
        reads as blank columns; hiding the ones that do not fit is the honest alternative, and
        the pane is then never given less width than the ones left.
        """
        cols_w = scaled(36 + 158 + 56)         # check, NAME, STATE: shown at any width
        for col, need in self._col_min:
            shown = pane_w >= need
            col.set_visible(shown)
            if shown:
                cols_w += int(col.get_fixed_width())
        return cols_w

    def _editor_overflows(self):
        """True when an editor pane is scrolling right now (its page is exceeded)."""
        for pane in (self._form_pane, self._bio_pane):
            scroller = pane.get_first_child()
            if scroller is None:
                continue
            ha, va = scroller.get_hadjustment(), scroller.get_vadjustment()
            if (ha.get_upper() > ha.get_page_size() + 0.5
                    or va.get_upper() > va.get_page_size() + 0.5):
                return True
        return False

    def _editor_min_width(self):
        """The editor's real horizontal minimum, measured where the content lives.

        `measure()` answers with a (minimum, natural) pair, and the *minimum* is the number that
        matters: below it the content is clipped or scrolled, above it the boxes give way. The
        panes sit in scrollers with `min_content_width(0)` — that override is what lets a pane
        give way — so measuring the pane reports the override, not the content. The content box
        is two levels down: pane → scroller → viewport → box.
        """
        best = 0
        for pane in (self._form_pane, self._bio_pane):
            node = pane.get_first_child()                  # the scroller
            for _ in range(2):                             # its viewport, then the content
                if node is None:
                    break
                node = node.get_child() or node
            if node is not None:
                best = max(best, node.measure(Gtk.Orientation.HORIZONTAL, -1)[0])
        return best

    def _size_panes(self, width, height=0):
        """Point the page: the list beside the editor, or as a strip above it."""
        if width <= 1 or not hasattr(self, "_editor"):
            return False
        total = width - 16                     # the two 8px margins
        share = max(scaled(LIST_WANTED_MIN), min(scaled(LIST_MAX), int(total * SHARE)))
        wanted = max(share, self._apply_column_policy(share))
        editor_min = self._editor_min_width()
        was = getattr(self, "_paned", None) or ("h", 0)
        # what the editor needs when it *has* the split width — the only honest figure to judge
        # the way back with (see the module docstring); the same number both ways
        self._editor_min_w = max(getattr(self, "_editor_min_w", 0), editor_min)
        fits = total - wanted - GAP >= self._editor_min_w
        panes_h = self._panes.get_height() or height
        tight_below = getattr(self, "_tight_below", 0)
        if was[0] == "h" and fits and self._editor_overflows():
            # The split layout fits horizontally yet is costing a scrollbar at this height.
            # A frame or two at startup can still be settling (a pane allocated before its
            # content is), so the band only moves after the overflow has held for a few
            # allocations — a transient frame must not pin the strip layout for the session.
            self._overflow_frames = getattr(self, "_overflow_frames", 0) + 1
            if self._overflow_frames >= SETTLE_FRAMES:
                tight_below = self._tight_below = max(tight_below, panes_h)
        else:
            self._overflow_frames = 0
        stacked = not fits or (tight_below and panes_h <= tight_below)
        want = ("v", -1) if stacked else ("h", wanted)
        if was != want:
            self._paned = want
            self._panes.set_orientation(
                Gtk.Orientation.VERTICAL if stacked else Gtk.Orientation.HORIZONTAL)
            if stacked:
                # the editor gets the whole width, so every column is affordable again
                self._apply_column_policy(total)
                self._list_pane.set_size_request(-1, scaled(LIST_MIN_H))
            else:
                self._list_pane.set_size_request(wanted, -1)
        return False
