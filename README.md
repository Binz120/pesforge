# PESForge GTK4

The GTK4 build of [PESForge](https://github.com/Binz120/pesforge). One repo, two windows onto the
same data layer: the GTK4 + libadwaita window (`editor_gtk.py`, what this fork is about) and the
original Tk window (`editor.py`), which stays here because it still has the features the GTK4 build
does not. The CPK/WESYS readers, the CLIs and the test suite are shared.

GTK4 and PyGObject ship with every current desktop, so the GTK4 build needs nothing installed and
no venv. It draws through Wayland or X11 directly — none of Tk's XWayland workarounds appear here.

## Run

```bash
./bin/pesforge-gtk4              # the GTK4 window
./bin/pesforge-gtk4 --check      # python/GTK versions, both installs it can see, no window
./bin/pesforge-gtk4 --db <cpk|folder>   # open straight into a database (or find it in a folder)
./install.sh                     # ~/.local/bin launchers + menu entries, no root
```

## The GTK4 window

**It starts empty.** Nothing is read until you import a database: an empty page with one
**Import database…** action, and Save/Revert/the search box all insensitive. **Import database…**
opens a hub where you drop a **CPK or a game folder** — or press **Choose file…** / **Choose
folder…** — and the hub reads the candidate's own bytes and prints what it found before the window
changes under you:

```
file: dt10_x64.cpk
detected: PES 2019 — 2,594,400 bytes of player data, 13,805 players
contents: 529 clubs, 188-byte player records
```

**Point it at a folder and it finds the file.** A folder pick is resolved by probing, never by
filename: the folder itself, its `Data/` and its `download/`, and the same pair one level down (so
the install root works too). Picking the PES 2019 install finds the pack the game loads
(`download/dt80_200E_x64.cpk`); picking its `Data/` finds `dt10_x64.cpk`; picking a PES 2015 install
finds `Data/dt10.cpk`; picking SP Football Life's install — or its `Data/`, which holds no database
at all — finds `download/data_s2627.cpk`; picking a PES 5 install — which holds no CPK anywhere —
finds the option file the game itself reads (`~/Documents/P5OSOF/...` for Original Season,
`~/Documents/KONAMI/...` for the plain release). A file pick that carries no player table (`Data/` is 40+
CPKs, and SP Football Life's `Data/dt10_x64.cpk` is an 86 KB stub with no table in it) is answered
with the file that should have been picked, and the hub's Load button before it is live.
Candidates are opened in ranked order and the walk is capped (the 12 likeliest of what it found),
so a folder pick answers in seconds, not a scan.

`Load` is only live for a release this build can edit; a CPK with no readable player table is
refused by name and the hub says which file it should have been. **Unload** drops the loaded
database and returns to the empty window; it refuses while edits are pending, naming the count.

**What you can edit.** One panel serves all four releases this build writes — everything the
screen shows comes from the loaded release's own map module (`pes2021.py` / `pes2019.py` /
`pes2015.py` / `pes5.py`):

| | PES 2021 / SP Football Life | PES 2019 | PES 2015 | PES 5 |
|---|---|---|---|---|
| records | 312 bytes, 26,395 players in `download/data_s2627.cpk` | 188 bytes, 13,805 in `Data/dt10_x64.cpk` | 180 bytes, 10,091 in `Data/dt10.cpk` | 124 bytes, 5,000 + 184 created slots in the `KONAMI-WIN32PES5OPT` option file |
| abilities | 25 | 23 | 19 | 26 |
| playing style | named (dropdown) | named (dropdown) | numeric (0–21) | numeric dribble style (1–4) |
| bio rows | + Reputation | + Reputation | — | condition, consistency, weak foot 1–8, injury C/B/A |
| skills / COM styles | 48 chips | 46 chips | 27 chips | 24 chips (the special abilities) |

- **List** — virtualized (`GtkColumnView` over the whole roster): ID, NAME, CLUB, POS, SPD, BC, FIN, STA,
  STATE. Every column sorts except POS; a tick column selects rows for the bulk edit. Names elide
  when the pane is narrow, with the full value on hover.
- **Search** — type to narrow by player name, club name or leading ID digits; the line under the
  list says how many rows are shown, and an empty result names the query.
- **Player card** — hero name, `id · club (#squad number)`, Height/Weight/Age/Foot chips, and the
  three name slots (name, shirt name, Japanese name).
- **Abilities** — the release's own range (40–99, or 0–99 on the classic map), a spin box (and a
  Cairo bar that colours by tier in the wide layout);
  **Position** is a dropdown over the release's position names.
- **BIO / SKILLS & COM STYLES** — height, weight, age, nationality (code plus the country name),
  form, weak foot usage/accuracy, injury resistance and reputation where the release has it,
  playing style, stronger foot, and the skill chips.
- **Layout** — the whole editor is meant to be on screen at once: player list on the left, the form
  and BIO panes stacked beside it, both scroll-free. Under 1340px the grids go compact (three
  ability/bio columns, spin steppers hidden — type or scroll the value, chips six to a row) so a
  tiled half-screen window still shows every field; at 1340px and wider the panes sit side by side
  with the value bars. `PESFORGE_GTK_SELFTEST=fit` proves it at the current window size, and
  `PESFORGE_GTK_SIZE=917x1030` picks the size for a check run.
- **Theme** — light or dark follows the desktop (GTK's own preference, then the freedesktop
  portal; `PESFORGE_THEME=light|dark` overrides) and the switch is followed live. Which palette
  light and dark use comes from the same `theme` key the Tk build's Settings writes
  (`amber`/`crimson`/`ice`/`pitch`); every combination has to clear 4.5:1 on its own, which
  `PESFORGE_GTK_SELFTEST=theme` reports.
- **Bulk matrix** — tick any number of rows and a tray appears: *set / add / subtract* one field
  across the selection, clamped to the field's own range (abilities stop at 99). Staged in memory
  like any other edit.

**Save to game** (the footer button) writes every pending edit into *each copy* of the Player table
that carries the player — the CPK you loaded plus the install's other database CPKs (for PES 5, the
one option file) — backing up the
regions it will touch (TOC, header table, payload) under the state folder first, then re-reading
each copy to check the edit landed. It reports which files were written, or which one failed to
read back. **Revert** drops the pending edits and re-reads from disk. The status line always names
the database it is reporting on.

**What this build does not do** (the Tk build and the CLIs do): undo/redo, per-field revert, the
pesmaster photo, appearance/coach/stadium editing, roster CSV, and club search as its own tab.
All four vintages are read *and* edited here: PES 2019 (188-byte records), PES 2015 (180),
SP Football Life / PES 2021 (312) and the classic PES 5 option file (124-byte records), each
through its own map module. There is no separate "PES 2015
tab": the release is read off the file you import, so a tab could only disagree with it.

**Self-checks** (`PESFORGE_GTK_SELFTEST=…`, each prints a marker and exits):

| value | what it proves |
|---|---|
| `1` | the real screen: rows in the model, a record in the form, search narrowing and clearing, no inert button, no national team in a CLUB cell |
| `empty` | nothing is read before an import, then the database loads and the panes come up |
| `cycle` | import PES 2019 → unload → SP Football Life → unload → PES 2015 |
| `select` | a click shows the row it hit, not the previous selection |
| `hub` | what a pick holds — a CPK or a folder — and Load only when this build can open it |
| `write` | tick rows, bulk-edit, save into a sandbox copy, read the edits back, restore and compare bytes |
| `write2015` | the same for PES 2015, on a copy, with the game's own file hashed before and after |
| `write5` | the same for the PES 5 option file, on a copy, checksums and the game's own file verified after |
| `write2021` | the same for SP Football Life / PES 2021 (abilities, a skill bit, the shirt name) |
| `fit` | neither editor pane needs a scrollbar at the window's size (`PESFORGE_GTK_SIZE=WxH` picks one) |
| `theme` | the palette in force (light or dark, from the desktop) and its fg/bg contrast |

## The Tk build (`editor.py`)

Still the fuller editor: SP Football Life / PES 2021 records (25 abilities, appearance, career
block), club/coach/stadium editing, undo/redo with a change drawer, player photos, sections that
fold, CSV in and out, and its own PES 2015/PES 2019 tabs driven by the same panel shape. Run it with
`./bin/pesforge` (needs `tkinter`; see the install notes in the launcher) — and with arguments it
runs the headless CLI instead of opening a window (`./bin/pesforge --find messi`). Both windows
share `~/.config/pesforge/config.json` and the backups folder.

## Headless

Both CLIs read the same config file, so the folder they use is the one the app already found.
`pesforge` here is the Tk build's launcher name; the CLIs are just Python.

```bash
python3 cli.py     --find "messi"                     # list matches
python3 cli.py     --show 7511                        # every field of one player
python3 cli.py     --fields                           # field names + ranges
python3 cli.py     --set "Lionel Messi" Speed=99 Age=30   # dry run, prints before/after
python3 cli.py     --set 7511 Speed=99 --save         # write every copy of the table
python3 cli.py     --export roster.csv                # 26,395 rows, all fields
python3 cli.py     --import edits.csv --save          # bulk edit, id column required
python3 cli.py     --squad "Arsenal"                  # browse a club
python3 cli.py     --export-assignments ass.csv       # id, name, shirt, team_id, club, league_id
python3 cli.py     --transfer "David Raya" --to "Chelsea FC" --save
python3 cli.py     --shirt "Lionel Messi" 10 --save    # squad number in his club
python3 cli.py     --coaches "arteta"                 # search Coach.bin
python3 cli.py     --club-coach "Arsenal FC" "Hansi Flick" --save
python3 cli.py     --stadium "Arsenal FC" 15 --save   # stadium id from --stadiums
python3 cli.py     --stadiums                         # the 37 known stadiums
python3 cli.py     --clean-tables blank/              # Player/Team/PlayerAssignment/Coach, 0 rows
python3 cli.py     --import new.csv --append --save   # grow the table with ids it does not have
python3 cli.py     --copy "Lionel Messi"              # one TSV line in --fields order
```

`--set` takes any number of `field=value` pairs; without `--save` it changes nothing on disk.
Every command starts from the **current database**, so successive runs accumulate instead of
overwriting each other.

CSV import: `id` plus any column from `--fields`; empty cells are left alone. `name` and `shirt`
edit the name slots. Unknown columns are rejected. Unknown ids are skipped with a `skip id N` line
unless `--append` is given, which creates a blank record first.

`--transfer` edits the player's first **club** assignment row. National teams own ids 1–99 in this
database, so a player's country row is left untouched. Rows referencing a team id missing from
`Team.bin` export with a blank club and are counted in the export summary rather than dropped.

### PES 2015 (dt10.cpk + dt80 data packs)

Retail PES 2015 is a different vintage — 180-byte player records, three name slots, nineteen
abilities, an encrypted TOC — so it gets its own reader/writer, usable from the GUI panel or
headless:

```bash
python3 cli2015.py --tables                        # what an edit would reach, with row counts
python3 cli2015.py --find "messi"                  # id, name, position, a few stats
python3 cli2015.py --show 7511                     # abilities, bio, flags and the three names
python3 cli2015.py --set 7511 Speed=90 Position=SS            # dry run, prints before/after
python3 cli2015.py --set 7511 Speed=90 Position=SS --save     # write every table holding him
python3 cli2015.py --set 7511 Height=180 "Skill: Scissors Feint"=1    # bio + flags via --set too
python3 cli2015.py --teams "barcelona"             # clubs / national teams: id, kind, name, short
python3 cli2015.py --set-team 108 Name="FC Barcelona" Short="BARCELONA" --save
python3 cli2015.py --coaches "mazzarri"            # coach list; Name= / Japanese= to rename
python3 cli2015.py --squads "L. Messi"             # his club and country rows, with squad numbers
python3 cli2015.py --set-squad "L. Messi" Team=108 Number=10 --save   # or Club=113 to move him
python3 cli2015.py --export players.csv            # all tables, with cpk/table columns
python3 cli2015.py --fields                        # field names + ranges
python3 cli2015.py --backups                       # what can be undone
python3 cli2015.py --restore last                  # put the previous bytes back
```

The install folder is found automatically (Wine prefixes, `wineprefixes/*`, Steam); `PES2015_DIR`
overrides it, or pass `--game DIR`. Writes need the game closed.

**What an edit reaches.** The tables live in `Data/dt10.cpk` (base) and in the data pack
`download/DpFileList.bin` names — `dt80_100E.cpk` on this install. Each holds `Player.bin` plus
numbered update slots (`Player1..6.bin`) and the same numbered slots for `Team.bin`, `Coach.bin`
and `PlayerAssignment.bin` (the squads). A `--set`, `--set-team`, `--set-coach`, `--set-squad` or a
**Save to game** from either window lands in *every* copy across those tables — dt10 and the pack
disagree on hundreds of squads, so the edit shows whichever file the game reads on boot.

**Saves are reversible.** Before touching a CPK, the bytes about to change — the TOC, the header
table and the payload region up to whatever the write can reach — are saved under
`<state>/backups/pes2015/`; `--restore` writes them back and truncates the file to its recorded
size, so a save undoes byte for byte. The game's own compressor beats standard zlib by a few
percent, so a re-packed table usually outgrows its slot and is appended at the end of the CPK with
its TOC entry repointed (the file grows by about one table per save: ~0.5 MB on dt10, ~0.6 MB on the
data pack); an edit that fits is patched in place and the file size does not move.

### PES 5 (the option file)

The classic era has no CPK, no WESYS and no CLI of its own — **the GTK4 window edits it**. The whole
database is one 1,250,304-byte `KONAMI-WIN32PES5OPT`: 5,000 market players plus the 184 created
slots, each a 124-byte record; the 138 clubs; the squads. `pes5.py` reads and writes it with the
PESFan Editor 5.08 crypto — a whole-file XOR, a per-word block cipher over blocks 1..9, and ten
block checksums — verified byte for byte against the shipped files.

Where the file is: PES 5 Original Season redirects into
`~/Documents/P5OSOF/Pro Evolution Soccer 5/save/folder1/`, the plain release uses
`~/Documents/KONAMI/...`; importing an install folder resolves to that file rather than to the
template shipped in `<game>/misc/Option File/`. The game reads it at boot, so close the game before
saving. A save rewrites the whole file (re-encrypted, checksums recomputed) after keeping a copy
under `<state>/backups/pes5/` — one file, one backup, and `pes5.backup_list()` /
`pes5.restore_backup()` put it back byte for byte.

Editable in this build: names (full UTF-16LE; shirt names keep the game's own A–Z, `.`, `_` and
space alphabet), the 26 abilities, age/height/weight/
foot/nationality, condition, consistency, weak foot 1–8, injury C/B/A, the registered position, the
numeric dribble style and the 24 special-ability chips. Clubs and squads are read (the list's CLUB
column, `#number` in the card); formation, kit and squad editing stay out of scope.

## Settings

`~/.config/pesforge/config.json`, shared by both windows and the CLIs; `SPFL_CONFIG`, `SPFL_DIR` and
`SPFL_STATE` override it.

| Setting | Meaning |
|---|---|
| Game folder | Folder holding `download/data_s2627.cpk`; checked before it is accepted |
| Database | An imported CPK that wins over the game folder (the Tk build's Club tab, the CLIs) |
| PES 2015 / PES 2019 folder | The install each vintage panel reads; empty = found automatically |
| PES 5 folder | The PES 5 install, for resolving the option file it plays; empty = found automatically |
| Backup folder | Where previous file versions go; blank = `~/.local/share/pesforge/backups` |
| Keep backups | Newest N are kept, older ones pruned on save; `0` keeps everything |
| Ask before writing the game file | Tk build: show the review sheet before a save is written |
| Load player photos from pesmaster (online) | Tk build: fetch each face once; off = fully offline |
| Theme | Tk build: Amber, Crimson, Ice or Pitch |

The GTK4 build reads the SP Football Life / PES 2019 / PES 2015 folders (`SPFL_DIR` /
`PES2019_DIR` / `PES2015_DIR` override them) and the PES 5 install folder (`PES5_DIR`), plus the
backup folder and cap; it has no settings
dialog yet.

## Files

| file | purpose |
|---|---|
| `cpk.py` | CRI CPK reader/writer (header + TOC `@UTF` tables, plaintext or XOR-obfuscated) |
| `pesdb.py` | WESYS pack/unpack, CPK table extraction, 312-byte PES 2021 bit-field codec, team maps |
| `pes2019.py` | PES 2019: 188-byte records, team/coach/stadium/assignment tables, CPK patching |
| `pes2015.py` | PES 2015: 180-byte records, WESYS payloads, CPK patching, region backups |
| `pes5.py` | PES 5: the classic-era option file — crypto, 124-byte records, clubs, inverted squads, whole-file backups |
| `editor_gtk.py` | the GTK4 window (and its entry point + self-checks) |
| `editor.py` | the Tk window (SP Football Life / PES 2021 and the vintage tabs) |
| `cli.py` | headless find / show / set / export / import / squad / transfer / coaches / stadium / shirt / copy |
| `cli2015.py` | headless PES 2015 find / show / set / export / tables / backups / restore |
| `settings.py` | config file: game folder, backups, save confirmation, photos, theme, window size |
| `theme.py` | the four palettes and the derived colour tokens (contrast-checked) |
| `pesdb_maps.py` | ability/skill/style tables + eFootball↔PES2021 conversion |
| `data/pesdb_maps.json` | the tables above (generated) |
| `data/countries.json` | nationality names keyed by the record's field |
| `assets/pesforge.png` | window and menu icon |
| `bin/pesforge-gtk4` | GTK4 launcher (finds an interpreter with PyGObject) |
| `bin/pesforge` | Tk launcher (finds an interpreter with tkinter) |
| `bin/pesforge2015` | launcher for the PES 2015 CLI (plain Python, no display) |
| `install.sh` | user-level install: launchers + menu entries, no root |
| `tests/run_all.py` | the whole suite: `python3 tests/run_all.py` (needs a display) |

## Tests

```bash
python3 tests/run_all.py         # plain asserts, no pytest; prints N/N passed
```

Covers the CPK/WESYS layer, both record maps, the PES 5 option-file codec (a synthetic fixture
gates CI, with no game data; the shipped and live files are decoded and written when one is
installed), the CLI sandbox round-trips, the Tk window's layout
and form behaviour, and the GTK4 window through its self-checks above. The GTK4 checks run under
`PESFORGE_GTK4_PYTHON` (or the first interpreter that can `import gi`); the Tk checks need
`tkinter`. Any check that wants a window skips rather than fails without a display, and the suite
runs the GUI ones on a throwaway window (`-alpha 0.0`) so it never flashes over your desktop.

## File format notes (PES 2021 / SP Football Life)

- `Player.bin` = 2048-byte block (`PlayerWeekno`) + WESYS block of 312-byte player records.
- Records are little-endian **bit fields** counted from bit 0:
  `value = ((int(rec, "little") >> bit) & mask) + add`. Abilities are value-40 in 6 bits,
  height -100, weight -30, age -15.
- Name slots at `+0x44`, four × 0x3D bytes: full, shirt, screen, printed.
- `Team.bin`: 1532-byte rows, team id at `+0x08`, **coach id u32 at +0**, **stadium id u16 at
  +60**, name at `+0x170`. `PlayerAssignment.bin`: 16-byte rows → `+0x04` player id, `+0x08`
  team id, **+0x0C squad number stored one below the number the game shows**.
- `Coach.bin`: 100-byte rows, id u32 at +0, Japanese name at +8 (45 bytes), name at +54
  (45 bytes), country 9 bits at bit 32, adaptability (40–99) 7 bits at bit 41.
- `Stadium.bin`: 392-byte rows, id u16 at +4, Japanese name at +8, English name at +189.
- `PlayerAppearance.bin`: 60-byte rows, one per `Player.bin` record **in the same order**
  (the player id also sits at +0), 117 bit fields grouped in `pesdb.APPEARANCE_GROUPS`.
- Career fields in the player record, from the PES 2021 schema: youth club u32 at bit 0,
  owner club u32 at bit 32, contract until (YYYYMMDD) 27 bits at 96, loan until 27 bits at 128,
  market value 24 bits at 160, national caps 8 bits at 184, reputation 3 bits at 451,
  playing attitude 2 bits at 382, legend bit 508, hand bit 509, Ballon d'Or bit 498.
- The shipped `data_s2627.cpk` understates `Player.bin` by 3,072 bytes in its TOC, so every
  later file sits 3,072 bytes further in; `pesdb._find_block()` scans for the real `WESYS`
  start instead of trusting the TOC offset.

**PES 2019** is a different vintage again: 188-byte records (23 abilities, per-position
playability, 48 skill bits, numeric nationality), `Team.bin` rows of 700 bytes with the roster id
the squads carry at +8, `Coach.bin` 108 bytes, `PlayerAssignment.bin` 16 bytes. The offsets are in
`pes2019.py`, verified against the installed copy. **PES 2015** is described above.

**PES 5** predates all of it: there is no container — the database *is* the option file, one
1.25 MB file of ten blocks behind 12-byte headers (`[u32 size][u32 checksum][u32 0xD00]`) under a
whole-file XOR and a per-word block cipher. Players are 124-byte records — 5,000 at
`36,872 + i*124` and 184 created slots at `14,044 + k*124` (the game calls those 32768+k) — whose
stats are u16 words at `rec[47+off]`, read as `(word >> shift) & mask`, i.e. plain bit fields over
the record; the full name is UTF-16LE (15 characters) at +0, the shirt name 16 single-byte bytes at
+32, the call name u16 at +48. Clubs are 138 × 140 bytes at 803,608, and squads live in block 5
(32 slots per club: u16 player ids at `667,458 + c*64`, number bytes stored one below the shown
number at `659,414 + c*32`, 255 = none). `pes5.py` carries the map and the crypto, byte-verified
against the shipped files; its `check` is the round trip — load, re-encode, compare.

## Good to know

**An in-game edit save shadows the database.** PES writes `EDIT00000000` under
`.../Documents/SPGAME/Football Life/2027/save/` the first time you save anything in Edit mode; on
boot the game layers that file over `data_s2627.cpk`, so squads/players/names stored there win over
anything patched here. Move it aside (`mv EDIT00000000 EDIT00000000.off`) to make database edits
visible again.

**Writes go into the CPK itself.** PESForge does not use a Sider live-CPK override for the pesdb
tables — an override for these tables ends up listing every player as `--- / Unregistered`, even
for a byte-identical copy of the shipped file — so edits are patched into the database CPK directly
(the table's own bytes, same length; or appended with its TOC entry repointed when the recompressed
table needs a few extra bytes). An edit that fits never changes the file size.

**A CPK outside a game folder.** `cli.py` and the Tk build's Database row take any `.cpk` (an
import is stored as `database` in the config and wins over the game folder). The GTK4 build imports
whatever file you drop in the hub and remembers the folder for the next chooser.

## Ability and skill tables (eFootball ↔ PES 2021)

`data/pesdb_maps.json` carries the per-version ability, skill and playing-style lists;
`pesdb_maps.py` loads them: `abilities(version)`, `skills(version)`, `PLAYING_STYLES`,
`convert_stats(values, src, dst)` and `skill_id(label)`. The PES 2021 list is the game's own
25-stat order and matches `pesdb.ATTRS` label for label — a second opinion on the bit table.

Conversion is by each stat's `PropertyName`, with one rename (`SetPieceTaking` ↔ `PlaceKicking`
in both directions). Three stats exist in only one game — eFootball's *GK Parrying* and
*Defensive Engagement*, PES 2021's *GK Clearing* — and are returned in `unmapped` rather than
invented. Skill ids resolve by normalised name (case/punctuation aside), so `Chip shot control`
finds id 12; the 12 FL27 skills the list lacks return `None`.
