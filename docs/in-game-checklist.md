# In-game checklist

The hardening pass could not boot the game, so nothing below has been observed in it. This is
what to open and what to compare, after a save, to close that gap.

## Before the save

1. Start the app: `./bin/pesforge-gtk4`, Import, pick the install's database CPK (or the game
   folder). The header should say which release it found and the list should show the roster
   (SP Football Life / PES 2021: 26,395; PES 2019: 16,762; PES 2015: 10,091).
2. Write down two or three players you will edit, and one field each — an ability, a BIO field
   (height or age are easy to see) and a shirt name.
3. Check the game is **not** running (`pgrep -f pes2027.exe` / the release's exe). The status
   line warns when it is; a running game reloads the table only when restarted.

## The save

4. Press **Save to game**. The status line should end with `saved N player(s) into …`,
   a `backups in …` path, and a `changes in changes-<stamp>.txt` name.
5. Open the change report next to the backups (`~/.local/share/pesforge/backups/`): one line
   per player and field, `pid <id>  <field>  <old> -> <new>`. This is the list to check in
   game — it is also what to read if the game shows something unexpected.

## In the game

6. Boot the game and open the player's own screen (Edit mode → Players, or the squad list).
   Compare, in this order — the later items depend on the earlier ones being right:
   * the roster loads at all, and the count is unchanged (no truncated table);
   * the player is on their team (the assignment rows are what the app writes around);
   * the ability numbers are the ones the change report says;
   * the BIO fields (height/age/foot/nationality) match;
   * the shirt name shows in a match or the squad list.
7. Check a player you did **not** edit: their abilities and team must be untouched. This is
   the evidence that only the intended records moved.
8. Exit and re-enter the game once more: the values must survive a reload (they are read from
   the CPK, not from a session cache).

## If the game shows the old values (or nothing changed)

* **The edit file wins.** PES keeps a `EDIT00000000` save that overrides the CPK's tables.
  If one exists, the app's status line says so; delete or back it up in the game's
  `save/` folder (or in `Documents/KONAMI/...`) and restart the game.
* **Wrong copy written.** SP Football Life reads `download/data_s2627.cpk`; an install can
  carry several copies of the table. The change report names every CPK the save touched.
* **Backup count.** `max_backups` in the config prunes old backups; the newest one is always
  the state before the last save.

## Reverting

```sh
# the app's own backups: newest first, one folder per vintage
ls -t ~/.local/share/pesforge/backups/pes2019 | head
# PES 2015/2019 keep region backups (a .bin plus a .json naming the CPK and ranges)
python3 -c "import pes2019, settings, os; \
  d = os.path.join(settings.backups_dir(), 'pes2019'); \
  print(pes2019.restore_backup(os.path.join(d, sorted(os.listdir(d))[-2][:-5])))"
```

PES 2021 / SP Football Life keep whole-file backups (`data_s2627.<stamp>.cpk`): copy the one
you want back over `download/data_s2627.cpk`. In the app, **Revert** only drops pending edits —
it does not touch the file.

## What this pass verified without the game

* the write flowed through the real code path on **copies**, and the copy's bytes were checked
  against the original: an unedited save is byte-identical, a single-field edit changes only
  that record's byte range, a three-player batch changes only those three records
  (tests/run_all.py, `test_container_*`, `test_gtk4_bulk_matrix_stages_and_commits`);
* the read-back check reads the table back off disk after every save;
* the game's own files were hashed before and after the whole pass — unchanged
  (REPORT.md lists both hash sets).
