"""PESForge GTK4 — the GTK4 build of the editor, as a package.

The entry point stays `editor_gtk.py` at the repo root (a launcher and the zipapp both run
that file); it imports this package and re-exports the names the tests use. The split:

    importing.py   identify a pick — CPKs, folders, the release behind them
    styling.py     the stylesheet (style.css) from the theme tokens, and light/dark
    widgets.py     small widget helpers + the Cairo ability bar
    reference.py   ability grouping, the OVR model, bulk field lists and ranges
    listpane.py    the player list: columns, ticks, cells, width policy
    formpane.py    the editor panes: player card, ability grid, BIO, chips, batch tray
    editing.py     form <-> record: load, dirty marking, undo/redo, field handlers
    saving.py      the write flow: backups, read-back, revert, close guard, alerts
    hub.py         the import dialog
    window.py      the window shell: header, footer, pages, sizing, import/unload
    selftest.py    the self-check modes — imported only when PESFORGE_GTK_SELFTEST is set
    app.py         main()/check(): argument and environment handling, Gtk.Application
"""
