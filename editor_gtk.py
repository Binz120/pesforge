"""PESForge — the entry point. The application lives in the `pesforge_gtk` package.

Run:  ./bin/pesforge-gtk4              (or: python3 editor_gtk.py; GTK4 via gi, no other dependency)

The window starts empty: nothing is read until a database CPK is imported, and the import
dialog shows what a pick holds before the window changes. This file stays the entry the
launcher, the zipapp and the self-checks drive — it puts the package on the path, re-exports
the names that were once defined here (the tests and callers import them from this module),
and runs the app. What lives where is documented in `pesforge_gtk/__init__.py` and
`docs/ARCHITECTURE.md`.
"""
import os
import sys

# The one place a missing dependency is explained: a zipapp run on a machine without GTK4
# otherwise dies with a bare ImportError, which says nothing about what to install.
MISSING = """pesforge-gtk4 needs GTK 4 with its Python bindings, and could not start:

    {error}

Install them, then run this again:

    Debian/Ubuntu   sudo apt install python3-gi gir1.2-gtk-4.0
    Fedora          sudo dnf install python3-gobject gtk4
    Arch            sudo pacman -S python-gobject gtk4
"""

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
except (ImportError, ValueError) as exc:                      # no gi, or GTK 4 too old/absent
    raise SystemExit(MISSING.format(error=exc))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pesforge_gtk.app import check, main  # noqa: E402,F401
from pesforge_gtk.batchbar import BatchBarMixin  # noqa: E402,F401
from pesforge_gtk.editing import EditingMixin  # noqa: E402,F401
from pesforge_gtk.formpane import FormPaneMixin  # noqa: E402,F401
from pesforge_gtk.hub import ImportHub  # noqa: E402,F401
from pesforge_gtk.importing import (  # noqa: E402,F401
    EDITABLE, GAME, NOT_READ, PROBE_LIMIT, VINTAGE_NAMES, VINTAGES,
    cpk_files, db_arg, default_database, describe_database, describe_pick, find_database,
    game_dir, import_start_folder, install_of, pack_order, probe_candidates, probe_database,
    rank_cpk, vintage_database,
)
from pesforge_gtk.layout import LayoutMixin  # noqa: E402,F401
from pesforge_gtk.listpane import (  # noqa: E402,F401
    LIST_MAX, LIST_MIN_H, ListPaneMixin, Row,
)
from pesforge_gtk.reference import (  # noqa: E402,F401
    ABILITY_GROUPS, COUNTRIES, OVR_ALIASES, STYLE_NAMES_FALLBACK,
    ability_group, ability_order, bulk_fields, country_names, field_range,
    grouped_ability_order, overall_rating, tier_of,
)
from pesforge_gtk.saving import SavingMixin  # noqa: E402,F401
from pesforge_gtk.styling import (  # noqa: E402,F401
    TOK, css, install_css, system_theme, tint,
)
from pesforge_gtk.widgets import (  # noqa: E402,F401
    AbilityBar, a11y, dim_label, ellipsizing_dropdown, select_all_on_focus,
    set_widget_value, slim_spin, spin_steps, step_widget, tree, widget_value,
)
from pesforge_gtk.window import PesForgeGtk  # noqa: E402,F401


if __name__ == "__main__":
    raise SystemExit(main())
