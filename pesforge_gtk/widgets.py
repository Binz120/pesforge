"""Small widget helpers and the Cairo ability bar.

Every value control in the editor goes through these: the label cap that keeps a grid's
columns straight, the accessible-name helper, the slimmed spinbutton, the eliding dropdown,
and the bar that shows an ability's level.
"""
import gi

from gi.repository import Gdk, Gtk, Pango

from .styling import TOK


def a11y(widget, label):
    """Give a control the name a screen reader should announce.

    A placeholder, an icon or a bare value box says nothing on its own; the label is what a
    keyboard or braille user hears.
    """
    widget.update_property([Gtk.AccessibleProperty.LABEL], [label])


def dim_label(text, width, chars):
    """A fixed-width right-aligned field label.

    `max_width_chars` matters as much as the size request: a Label's *natural* width is its whole
    text, so without the cap a long label ("Defensive Awareness") pushes its value box right and
    every row's box lands somewhere else — the column looks ragged even though it is a grid.
    """
    lab = Gtk.Label(label=text, xalign=1)
    lab.add_css_class("dim")
    lab.set_size_request(width, -1)
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


class AbilityBar(Gtk.DrawingArea):
    """The ability bar: a track and a fill, Cairo-drawn, repainted when the value changes."""

    def __init__(self, width=104):
        super().__init__()
        self.value = 40
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
        frac = max(0.0, min(1.0, (self.value - 40) / 59.0))
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
