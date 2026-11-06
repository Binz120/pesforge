"""Layout policy: the panes' arrangement, decided from the live allocation.

GTK4 has no size signal, so the decision is taken in `do_size_allocate`. One structure —
the list beside a stacked form+BIO, or the list as a strip above the editor — is only
*turned* here; which way is decided from the editor's own measured minimum and whether the
form pane is actually scrolling this frame, so the two states cannot flip back and forth
and the split layout cannot become a one-way door.

The `do_size_allocate` vfunc itself stays on the window class (window.py): PyGObject only
installs a `do_*` override that is defined on the class, not one inherited from a mixin —
a mixin copy is simply never called, and the layout silently freezes at whatever GTK
allocated first.
"""
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .listpane import LIST_MAX, LIST_MIN_H  # noqa: E402




class LayoutMixin:
    def _rebuild_for_mode(self):
        """Rebuild the grids for the other layout — once, when the window's size is settled.

        A rebuild is a fresh page: the store, the filter and the form all have to be refilled,
        or the list comes back empty.
        """
        was = self.index
        self._build_panes()
        if getattr(self, "rows", None):
            self.populate()
            self._refilter()
            if was is not None:
                self._load(was)
        return False

    def _size_panes(self, width, height=0):
        """Point the page: the list beside the editor, or as a strip above it.

        The editor's own content decides, not a width I guessed: the form overflows (and GTK
        slices its columns) whenever the space left beside the list is smaller than what the
        fields need, and that need changes with how the labels happen to wrap. So: side by side
        while it fits, strip-on-top the moment it does not, with a 40px band so the two states
        cannot flip back and forth.
        """
        if width <= 1 or not hasattr(self, "_editor"):
            return False
        total = width - 16                          # the two 8px margins
        list_w = max(260, min(LIST_MAX, int(total * 0.32)))
        # The six columns are 452px together and a ColumnView squeezes instead of scrolling: at a
        # 320px pane ID and NAME collapsed to nothing (the "blank columns" the user reported)
        # because the policy below was data nobody read. Hide what does not fit, and never hand the
        # pane less width than the columns that are left.
        pane_w = total if (width < 910 or 0 < height < 950) else list_w
        cols_w = 36 + 158 + 56                      # check, NAME, STATE: shown at any width
        for col, need in self._col_min:
            col.set_visible(pane_w >= need)
            if pane_w >= need:
                cols_w += int(col.get_fixed_width())
        list_w = max(list_w, cols_w)
        space = total - list_w - 8                  # what the editor would get beside the list
        try:
            editor_min = self._editor.measure(Gtk.Orientation.HORIZONTAL, -1)[1]
        except Exception:                           # not measurable before the first frame
            editor_min = 586
        was = getattr(self, "_paned", None) or ("h", 0)
        if was[0] == "h":
            # what the editor needs when it *has* the side-by-side width — the only honest number
            # to judge the way back with. Measured while stacked it is inflated (the form is as
            # wide as the window there, so its labels stop wrapping), and the pane then never
            # returns: the split layout would be a one-way door.
            self._editor_min_h = max(getattr(self, "_editor_min_h", 0), editor_min)
        else:
            editor_min = getattr(self, "_editor_min_h", editor_min)
        stacked = (width < 910 or 0 < height < 950
                   or space < editor_min + 16
                   or (was[0] == "h" and self._form_overflows()))
        want = ("v", -1) if stacked else ("h", list_w)
        if was != want:
            self._paned = want
            self._panes.set_orientation(
                Gtk.Orientation.VERTICAL if stacked else Gtk.Orientation.HORIZONTAL)
            self._editor.set_vexpand(True)
            if stacked:
                self._list_pane.set_size_request(-1, LIST_MIN_H)
            else:
                self._list_pane.set_size_request(list_w, -1)
        return False

    def _form_overflows(self):
        """True when the form pane is actually showing a horizontal scrollbar this frame."""
        scroller = self._form_pane.get_first_child()
        if scroller is None:
            return False
        ha = scroller.get_hadjustment()
        return ha.get_upper() > ha.get_page_size() + 0.5
