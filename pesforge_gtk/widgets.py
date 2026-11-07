"""Small widget helpers and the Cairo ability bar.

Every value control in the editor goes through these: the label cap that keeps a grid's
columns straight, the accessible-name helper, the slimmed spinbutton, the eliding dropdown,
and the bar that shows an ability's level.
"""
import math

import gi

from gi.repository import Gdk, Gtk, Pango

from .styling import TOK


def a11y(widget, label):
    """Give a control the name a screen reader should announce.

    A placeholder, an icon or a bare value box says nothing on its own; the label is what a
    keyboard or braille user hears. The name is also recorded on the widget: GTK's property is
    write-only from Python, and `PESFORGE_GTK_SELFTEST=a11y` walks the tree to prove nothing
    interactive was left unnamed.
    """
    widget._a11y_name = label
    widget.update_property([Gtk.AccessibleProperty.LABEL], [label])


def text_scale_factor():
    """The desktop's text-scaling factor, as GTK is applying it (1.0 at 96 dpi).

    The stylesheet's sizes are in em, so the *text* follows this on its own; pixel widths
    (column widths, label widths, the list's pane thresholds) are fixed numbers and have to be
    multiplied by it, or a larger text scale clips them.
    """
    settings = Gtk.Settings.get_default()
    dpi = settings.get_property("gtk-xft-dpi") if settings is not None else 0
    return max(1.0, dpi / (96 * 1024)) if dpi and dpi > 0 else 1.0


def scaled(px):
    """A design pixel width, at the text scale the desktop is running at."""
    return int(round(px * text_scale_factor()))


LABEL_STEPS = (1.0, 0.9, 0.8, 0.75)   # font steps a field label may be drawn at


def fit_field_labels(texts, chars, budget, parent=None, pad=0, gap=6, box=72, cols=3, spacing=2):
    """`(width, css class)` for a grid's label column: the widest label, at the largest font
    step that still lets the whole grid fit `budget` design pixels.

    Two rules this exists to keep, both learned by measuring: a label's `set_size_request` is a
    minimum, so a term wider than it takes its own natural width and pushes its value box out of
    line (the classic vocabularies' "Short Pass Accuracy" needs ~150px against the 110 the grid
    was paying, which is what put boxes 40px apart in one column); and every label in a grid must
    share one width, or the boxes align per row instead of per column. The font step is the only
    lever that keeps the whole term — an elided game term reads as a missing field.
    """
    factor = text_scale_factor()
    chosen = None
    for step in LABEL_STEPS:
        widest = 0
        for text in texts:
            probe = dim_label(text, 0, chars, scale=step)
            # Measured *in the tree* when the caller has a widget to lend: an unattached label
            # resolves the default theme font, not the app's own `.dim` step, and comes back ~35%
            # wider — which is how a "measured" width over-sized the column and dropped the font a
            # step it did not need.
            if parent is not None:
                parent.append(probe)
            try:
                widest = max(widest, probe.measure(Gtk.Orientation.HORIZONTAL, -1)[1])
            finally:
                if parent is not None:
                    parent.remove(probe)
        design = int(math.ceil(widest / factor)) + pad
        chosen = (design, step)
        if cols * (design + gap + box) + spacing * (cols - 1) <= budget:
            return chosen
    return chosen                              # the smallest step, even if the budget is tight


def dim_label(text, width, chars, scale=None):
    """A fixed-width right-aligned field label.

    `max_width_chars` matters as much as the size request: a Label's *natural* width is its whole
    text, so without the cap a long label ("Defensive Awareness") pushes its value box right and
    every row's box lands somewhere else — the column looks ragged even though it is a grid.
    The width is a *design* width and scales with the desktop's text size, or the label would be
    clipped once the text outgrows it.
    """
    lab = Gtk.Label(label=text, xalign=1)
    lab.add_css_class("dim")
    if scale and scale != 1.0:
        # a Pango scale, not a CSS class: it is applied by the label itself, so the width this
        # function is handed was measured with the same step (a CSS rule is not visible to an
        # unattached probe label, which is how a "measured" width once came out wrong)
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_scale_new(scale))
        lab.set_attributes(attrs)
    lab.set_size_request(scaled(width), -1)
    lab.set_max_width_chars(chars)
    lab.set_ellipsize(Pango.EllipsizeMode.END)
    lab.set_tooltip_text(text)
    return lab


def slim_spin(spin):
    """Hide a spinbutton's steppers (they cost ~60px of width each, and typing/scrolling works).

    The compact layouts have three fields per row and no room for the buttons; the batch tray
    keeps its own steppers, and the wide layout keeps every one of them.
    """
    child = spin.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Button):
            child.set_visible(False)
        child = child.get_next_sibling()


def ellipsizing_dropdown(items, chars=12):
    """A DropDown whose shown value elides.

    Its natural width is otherwise the longest item in the list ("Extra Frontman"), which makes
    that cell wider than every value box beside it and the whole column ragged.
    """
    dd = Gtk.DropDown.new_from_strings(items)
    factory = Gtk.SignalListItemFactory()

    def setup(_f, item):
        lab = Gtk.Label(xalign=0)
        lab.set_ellipsize(Pango.EllipsizeMode.END)
        lab.set_max_width_chars(chars)
        item.set_child(lab)

    def bind(_f, item):
        entry = item.get_item()
        text = entry.get_string() if entry is not None else ""
        lab = item.get_child()
        lab.set_text(text)
        lab.set_tooltip_text(text)     # the elided value stays readable on hover

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    dd.set_factory(factory)
    return dd


def select_all_on_focus(spin):
    """Focusing a value selects it, so typing replaces it instead of appending to it."""
    focus = Gtk.EventControllerFocus()
    focus.connect("enter", lambda _c, s=spin: s.select_region(0, -1))
    spin.add_controller(focus)


def step_widget(spin, delta):
    """Move a spinbutton's value by `delta`, clamped to its own range (the game's limits)."""
    lo, hi = spin.get_range()
    spin.set_value(max(lo, min(hi, spin.get_value() + delta)))


def spin_steps(spin, big=5):
    """Wheel and arrows step by one; hold Shift for five.

    Both controllers run in the capture phase: the spinbutton's inner GtkText would otherwise eat
    Up/Down as caret movement and the key would never reach this handler.
    """
    keys = Gtk.EventControllerKey()

    def on_key(_c, keyval, _code, state):
        up = keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up)
        down = keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down)
        if not (up or down):
            return False
        step = big if state & Gdk.ModifierType.SHIFT_MASK else 1
        step_widget(spin, step if up else -step)
        return True

    keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    keys.connect("key-pressed", on_key)
    spin.add_controller(keys)
    scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
    scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

    def on_scroll(controller, _dx, dy):
        state = controller.get_current_event_state()
        step = big if state & Gdk.ModifierType.SHIFT_MASK else 1
        step_widget(spin, -dy * step)
        return True

    scroll.connect("scroll", on_scroll)
    spin.add_controller(scroll)


def widget_value(widget):
    """A field's value whether it is a spinbutton or a dropdown."""
    if isinstance(widget, Gtk.DropDown):
        return int(widget.get_selected())
    return int(widget.get_value())


def set_widget_value(widget, value):
    value = int(value)
    if isinstance(widget, Gtk.DropDown):
        model = widget.get_model()
        widget.set_selected(max(0, min(value, model.get_n_items() - 1)))
    else:
        widget.set_value(value)


def tree(root):
    """Every widget under `root`, depth first (no window-manager or GTK-internal traversal)."""
    out, stack = [], [root]
    while stack:
        w = stack.pop()
        out.append(w)
        child = w.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return out


class AbilityBar(Gtk.DrawingArea):
    """The ability bar: a track and a fill, Cairo-drawn, repainted when the value changes."""

    def __init__(self, width=104, floor=40, top=99):
        super().__init__()
        self.floor = floor            # the release's own 0: 40 on the +40 maps, 0 on PES 5's
        self.top = top
        self.value = floor
        self.set_content_width(width)
        self.set_content_height(7)
        self.set_valign(Gtk.Align.CENTER)
        self.set_draw_func(self._draw)

    def set_value(self, value):
        self.value = value
        self.queue_draw()

    @staticmethod
    def _round_rect(cr, x, y, w, h, r):
        r = min(r, h / 2, max(w, 0.1) / 2)
        cr.new_path()
        cr.arc(x + w - r, y + r, r, -1.5708, 0)
        cr.arc(x + w - r, y + h - r, r, 0, 1.5708)
        cr.arc(x + r, y + h - r, r, 1.5708, 3.14159)
        cr.arc(x + r, y + r, r, 3.14159, 4.71239)
        cr.close_path()

    def _draw(self, _area, cr, width, height):
        frac = max(0.0, min(1.0, (self.value - self.floor) / float(self.top - self.floor)))
        track = Gdk.RGBA()
        Gdk.RGBA.parse(track, TOK["bar"])
        cr.set_source_rgba(track.red, track.green, track.blue, track.alpha)
        self._round_rect(cr, 0, 0, width, height, height / 2)
        cr.fill()
        if frac <= 0:
            return
        spec = TOK["accent"] if self.value >= 80 else (
            TOK["accent_lo"] if self.value >= 65 else TOK["bar_low"])
        paint = Gdk.RGBA()
        Gdk.RGBA.parse(paint, spec)
        cr.set_source_rgba(paint.red, paint.green, paint.blue, paint.alpha)
        self._round_rect(cr, 0, 0, max(width * frac, height), height, height / 2)
        cr.fill()
