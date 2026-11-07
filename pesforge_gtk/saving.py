"""The write flow: backups, the patch, the read-back check, revert and the close guard.

Save writes every pending edit into each copy of the player table the install carries: the
loaded file first, then its siblings. Every copy is read and the edits applied in memory
before anything is written; each write is preceded by a whole-file backup and followed by a
re-read of the table from disk (what was saved is not what was written). A failed save is
reported in the status line and a dialog, and the pending edits stay.
"""
import os
import subprocess
import time

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

import settings  # noqa: E402

from .importing import VINTAGES  # noqa: E402




class SavingMixin:
    def _alert(self, message, detail=""):
        """A modal the user cannot miss: a traceback in a signal handler is not an error report."""
        dialog = Gtk.AlertDialog()
        dialog.set_modal(True)
        dialog.set_message(message)
        if detail:
            dialog.set_detail(detail[:400])
        dialog.set_buttons(["OK"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)

        def _done(d, result):
            try:
                d.choose_finish(result)
            except GLib.Error:                      # dismissed
                pass
        dialog.choose(self, None, _done)

    def _on_close_request(self, *_a, ask=True):
        """Hold the window open while edits are pending: memory is their only copy."""
        if not self.dirty:
            return False
        if ask:
            dialog = Gtk.AlertDialog()
            dialog.set_modal(True)
            dialog.set_message(f"Discard {len(self.dirty)} unsaved edit(s)?")
            dialog.set_detail("They are not written to the database until you press "
                              "Save to game.")
            dialog.set_buttons(["Keep editing", "Discard"])
            dialog.set_cancel_button(0)
            dialog.set_default_button(0)

            def _done(d, result):
                try:
                    if d.choose_finish(result) == 1:
                        self.destroy()
                except GLib.Error:
                    pass
            dialog.choose(self, None, _done)
        return True

    def _save(self):
        """Write every pending edit into each copy of the table, backing each region up first."""
        if not self.dirty:
            self._status("nothing to save · edit a field first")
            return
        if not self.game:
            self._status(f"no install to write to (set {VINTAGES[self.vintage]['env']})")
            return
        edits = self._edited_values()
        backup = settings.backups_dir()
        written, failed = [], []
        others = self.mod.database_cpks(self.game) if self.game else []
        # Read every copy and build what would be written first: a copy that cannot be read
        # fails before anything has been written.
        jobs = []
        for path in [self.source] + [p for p in others if p != self.source]:
            name = os.path.basename(path)
            try:
                db = self.mod.Cpk(path)
                table = db.read_table("Player")
            except KeyError:
                continue                      # a pack without a player table
            except (OSError, ValueError) as exc:
                failed.append(f"{name}: {type(exc).__name__}: {exc}")
                continue
            hit = 0
            for pid, fields in edits.items():
                for i in table.find(str(pid)):
                    hit += 1
                    for key, value in fields.items():
                        if key.startswith("name:"):
                            table.set_name(i, **{key.split(":", 1)[1]: value})
                        else:
                            table.set(i, key, value)
            if hit:
                jobs.append((path, db, table, hit))
        # Write. A failure is reported in the status line and a dialog — a traceback in a signal
        # handler is a console nobody watches.
        for path, db, table, hit in jobs:
            name = os.path.basename(path)
            if not os.access(path, os.W_OK):  # no backup is worth spending on a doomed write
                failed.append(f"{name}: not writable")
                continue
            try:
                db.patch("Player", table.to_payload(), backup_dir=backup)
                back = self.mod.Cpk(path).read_table("Player")    # saved is not written
            except (OSError, KeyError, ValueError) as exc:
                failed.append(f"{name}: {type(exc).__name__}: {exc}")
                continue
            wrong = [pid for pid, fields in edits.items() if not self._reads_back(back, pid, fields)]
            if wrong:
                failed.append(f"{name}: {len(wrong)} player(s) did not read back")
            else:
                written.append(f"{name} ({hit})")
        if not jobs:
            if failed:
                self._status("could not write: " + "; ".join(failed))
                self._alert("Could not write the database", "; ".join(failed))
            else:
                self._status("no database copy carries these players — nothing written")
            return
        exes = VINTAGES[self.vintage]["exe"]
        exes = exes if isinstance(exes, tuple) else (exes,)
        running = any(subprocess.run(["pgrep", "-f", exe],
                                     capture_output=True).returncode == 0 for exe in exes)
        head = "game is running, it reloads the table when you restart it · " if running else ""
        if failed:
            kind = "partial save: " if written else "could not write: "
            self._status(f"{head}{kind}" + "; ".join(failed))
            self._alert("Save did not finish", f"{head}{kind}" + "; ".join(failed))
            return
        saved = sorted({i for i, _label in self.dirty})
        report = self._change_report(edits, written)          # before the edits are forgotten
        self.dirty.clear()
        self.originals.clear()                     # the game holds these now: no "was …" left
        self._refresh_cells(saved, edited=False)   # the STATE cell only repaints on a re-bind
        if self.index is not None:
            self._load(self.index)                 # and the form's fields drop their markers
        pruned = settings.prune_backups(backup, settings.load()["max_backups"])
        self._status(f"{head}saved {len(edits)} player(s) into " + ", ".join(written)
                     + f" · backups in {backup}" + (f" ({pruned} old pruned)" if pruned else "")
                     + (f" · changes in {os.path.basename(report)}" if report else "")
                     + (" · EDIT00000000 is present and overrides this file in game"
                        if getattr(self.mod, "EDIT_SAVE", True) and settings.edit_save_files()
                        else ""))
        self._update_bulk()

    def _change_report(self, edits, written):
        """A human-readable record of what this save changed, beside the backup it made.

        The backup holds the bytes; this holds the intent — one line per player and field with
        the value that was there and the one written — so a save can be checked, or undone by
        hand, without the app. Returns the file's path, or "" when it could not be written
        (which is reported but does not fail the save: the data is already on disk).
        """
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(settings.backups_dir(), f"changes-{stamp}.txt")
        lines = [f"# PESForge save, {time.strftime('%Y-%m-%d %H:%M:%S')}",
                 f"# release: {VINTAGES[self.vintage]['label']}",
                 f"# written: {', '.join(written)}",
                 f"# players: {len(edits)}"]
        for i, key in sorted(self.dirty):
            was = self.originals.get((i, key), "?")
            now = edits.get(self.table.pid(i), {}).get(key, "?")
            field = "name (" + key.split(":", 1)[1] + ")" if key.startswith("name:") else key
            lines.append(f"pid {self.table.pid(i)}\t{field}\t{was} -> {now}")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError as exc:
            self._status(f"saved, but the change report could not be written: {exc}")
            return ""
        return path

    def _reload(self):
        """Drop the pending edits and re-read the database from disk."""
        try:
            self.table = self.mod.Cpk(self.source).read_table("Player")
        except (KeyError, OSError, ValueError) as exc:
            self._status(f"reload failed: {exc}")
            return
        self.dirty.clear()
        self.originals.clear()
        self.checked.clear()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.originals.clear()
        self.index = None
        self.rows = []
        self.store.remove_all()
        self.load_rows()
        self.populate()
        self._status("pending edits dropped, re-read from disk")
