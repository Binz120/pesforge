# PESForge GTK4

The GTK4 build of PESForge, forked from [pesforge](https://github.com/Binz120/pesforge). The data
layer, the file format work, the CLIs and the test suite are shared with the original; the desktop
window is rebuilt on GTK4 and libadwaita instead of Tk.

**Status: the main screen is real, the write path is not.** The player list is the table the game
itself reads, search and column sorting are live, every field in the form comes from the selected
record, and edits land in its in memory copy. Save, undo/redo, the photo fetch and the PES 2021 and
PES 2015 tabs exist only in the Tk build, which this fork keeps as `editor.py`.

## Run

    ./bin/pesforge-gtk4          # or: python3 editor_gtk.py
    ./bin/pesforge-gtk4 --check  # report the table it would open, without a window

GTK4 and PyGObject ship with every current desktop, so there is nothing to install and no venv.
The window talks Wayland directly, which is why none of the Tk build's XWayland workarounds (cursor
theme, DISPLAY probing, DPI scaling) appear here.

## The PESForge editor

A player editor for **SP Football Life 2027** (PES 2021 engine) on Linux — GUI and headless CLI.
Everything is done on files: the game never needs to be running, and nothing touches the game
process. Edits are patched into the game's own database and load the next time the game starts.

## What you can edit

- **Players** — 25 abilities (40–99) with value bars; age, height, weight, nationality, form,
  weak foot usage & accuracy, injury resistance, stronger foot; registered position and 13
  position ratings; playing style; 48 skills; full + shirt names; nine celebration/motion slots;
  and the career block (youth and owner club picked by club name, contract until, loan until,
  market value, national caps, reputation, playing attitude, legend / hand / Ballon d'Or flags).
- **Clubs** — transfers, squad numbers, the club's coach (from the 980-coach list), home stadium
  (from the 37 in the database) and the club's name slots.
- **Coaches** — all 980 `Coach.bin` records: name, Japanese name, nationality, adaptability,
  and the club each one is linked to.
- **Appearance** — the selected player's 117 appearance fields, grouped by head/eyes/eyebrows/
  nose/mouth/facial hair/hair/physique/kit, each clamped to its own range.

Search by name, shirt name or ID. Club search is accent-blind and takes nicknames (`man u`,
`spurs`, `barca`, …). Filter by club, country or free agents, show only changed records, sort any
column, undo/redo (`Ctrl+Z` / `Ctrl+Y`), bulk-edit a selection, compare two players, and import or
export rosters as CSV.

The app is fully offline — the only network call is the optional player-photo fetch, and it can
be switched off in Settings.

Every write is safe: the game's CPK is copied to a backups folder before each save, and
**Vanilla** restores the pristine snapshot taken on first run.

## Requirements

Linux with Python 3.8+ and **tkinter** (Tk 8.6 or newer). No pip packages, no other dependencies.

```bash
sudo apt install python3-tk          # Debian, Ubuntu, Mint, Pop!_OS
sudo dnf install python3-tkinter     # Fedora
sudo pacman -S tk                    # Arch, Manjaro, EndeavourOS
sudo zypper install python3-tk       # openSUSE
```

Any desktop works: Tk draws through X11, which Wayland sessions provide via XWayland (GNOME, KDE,
XFCE, Cinnamon, sway, Hyprland, niri are all fine). On a bare Wayland compositor **without**
XWayland, run it under `xwayland-run` or use the headless CLI.

## Install

```bash
git clone <this repo> && cd pesforge
./install.sh                         # ~/.local/bin/pesforge + a menu entry, no root
./install.sh --uninstall             # removes both; config and backups stay
```

The launcher works from any directory and picks the first interpreter with tkinter, preferring
the distro's `/usr/bin/python3` (that build carries the system fontconfig). Override with
`SPFL_PYTHON=/path/to/python3`, or point at a checkout elsewhere with `SPFL_EDITOR_DIR=…`.

## Quick start

```bash
pesforge                  # GUI  (menu entry: "PESForge")
pesforge --check          # environment report: python, Tk, display, game folder
pesforge --find messi     # headless, no window at all
```

On first run the game folder is found automatically: `~/Games`, the Steam/Lutris libraries,
`/run/media` and `/media` are searched a few levels deep, and the newest `data_s*.cpk` wins. If
nothing is found, the first run asks you to pick the folder and remembers it. The path is stored
in `~/.config/pesforge/config.json` so the CLI and GUI agree.

## The GUI

The window is sized for a 1460×920 setup: the player list, the player form and the
radar/skills/club column side by side. The header carries the search box — type a name, shirt
name, club or ID and press Enter to open the first match — beside **Save to game**, **Undo**,
**Redo**, **Discard** and the menu. The footer keeps the status line, the pending-change count
(click it to open the change drawer) and the keyboard hints. The shape follows the window width:
three columns from 1360 px, the list beside a single form column between 900 and 1360, and below
that the list sits above the form with a draggable splitter — a tiling window manager can hand the
app any size, so nothing is ever clipped to make a layout fit. The form grids pick their own
column counts from the pane widths they are given.

**Sections fold.** Player, Abilities, Position ratings, Details, Contract · motion · celebration,
Ability profile, Skills, Current club and Club name are collapsible: click a section title. The
career block and the club-name slots start folded because they are touched once a save, not once
a player, and the choice is remembered in the config.

**Every edit is tracked.** Changing a value tints the field it belongs to and adds a row to the
change drawer, which lists what changed, from what to what, with *Revert selected*, *Revert all*
and *Save now*. The section titles carry their own count, so a folded block still says it has
edits. **Undo** / **Redo** walk the history (`Ctrl+Z` / `Ctrl+Y`); **Discard** throws every
unsaved change away and reloads the tables from disk — it is not a one-step undo, and the header
says so.

**Save to game** opens a review sheet first: which tables are written, how many edits and rows
each has, where the backup lands, and the warnings that matter (the game is running, an in-game
`EDIT00000000` save shadows the database, two players in one club share a squad number). Only the
tables you changed are re-packed, each re-read and its row count compared before the status line
reports `ok`. The **Ask before writing** setting can skip the sheet; warnings still show it.

**Abilities** are edited with a number box and a full-width bar: click or drag the bar, or type
the value. A ghost tick on the bar marks the value the player started with, so an edit is visible
before it is saved. Right-click a bar to put that one field back.

**Position and skill chips** work from the keyboard too: `Tab` to a chip, `Space`/`Enter` to
toggle. A position chip cycles none → can play → natural (right-click walks the other way) and
shows the state as a mark, not only as a colour: `CB`, `CB ·`, `CB ✓`. The 48 skills have a
filter box, and the count says how many are chosen.

**The list has two tabs.** A search reports players *and* clubs — typing `arsenal` keeps the
players whose club matches on the Players tab and puts the club itself on the Clubs tab, each
tab showing its count. The list has a slim drag indicator on its right edge; it appears only when
there is something to scroll.

**The club block edits the club, not the player.** Coach, stadium and squad number belong to the
club record (and every player at that club), and the block says so above its fields. Transfers
move the player's first club assignment row; the club column, coach and stadium boxes all take
type-ahead.

The **OVR** in the list and on the player card is computed from a per-position model
(`pesdb.OVR_MODEL`, `pesdb.overall`): weighted abilities for the running position, rounded
half-to-even. It is a model of its own, not the game's hidden overall — PES 2021 stores no rating
in the record. The card colours it by tier: 90+ accent, 80+ green, 70+ plain text.

**Coaches** open in their own window: search by name or club, edit name, Japanese name,
nationality (a country name, picked from the same list as the player form) and adaptability.

**Appearance** opens grouped by head/eyes/eyebrows/nose/mouth/facial hair/hair/physique/kit, with
a filter, a slider that knows the field's own range, and a *Reset field* button. Changed rows are
marked in the list.

**Theme** switches the palette (Amber, Crimson, Ice, Pitch) immediately, from the menu or from
Settings: the colours are re-bound and the widgets rebuilt, keeping the loaded tables, the search
query, the selection and pending edits.

Menu entries: **Settings…** (`Ctrl+,`), **Theme**, open backups folder, **Reload database**,
**Import database…**, **PES 2015 database…**, **Restore vanilla**, export/import roster CSV,
**Bulk edit selected…**, **Compare selected players…**, **Coaches…**, **Appearance of the
selected player…**, **About**, plus Undo/Redo/Discard at the top.

Keyboard: `Ctrl+S` save · `Ctrl+Z` / `Ctrl+Y` undo/redo · `Ctrl+F` focus the search box ·
`Ctrl+,` settings · `Escape` clear the search · `Enter` open the first match / apply a field ·
`Space`/`Enter` on a focused chip.

## Settings

| Setting | Meaning |
|---|---|
| Game folder | Folder holding `download/data_s2627.cpk`; checked before it is accepted |
| Backup folder | Where previous file versions go; blank = `~/.local/share/pesforge/backups` |
| Keep backups | Newest N are kept, older ones pruned on save; `0` keeps everything |
| Ask before writing the game file | Show the review sheet before a save is written |
| Load player photos from pesmaster (online) | Fetch each player's face the first time it is shown; off = fully offline, and the card drops the empty photo box |
| Theme | Amber, Crimson, Ice or Pitch; applied immediately (also in the menu) |

Saved to `~/.config/pesforge/config.json` and applied immediately. The window size and the folded
sections are remembered there too. The CLI reads the same file for the game folder and backup cap.
`SPFL_DIR`, `SPFL_STATE` and `SPFL_CONFIG` override the config file for both GUI and CLI.

## Settings

| Setting | Meaning |
|---|---|
| Game folder | Folder holding `download/data_s2627.cpk`; checked before it is accepted |
| Backup folder | Where previous file versions go; blank = `~/.local/share/pesforge/backups` |
| Keep backups | Newest N are kept, older ones pruned on save; `0` keeps everything |
| Ask before writing the game file | Confirmation dialog before a save is written |
| Load player photos from pesmaster (online) | Fetch each player's face the first time it is shown; off = fully offline |

Saved to `~/.config/pesforge/config.json` and applied immediately. The CLI reads the same file
for the game folder and backup cap. `SPFL_DIR`, `SPFL_STATE` and `SPFL_CONFIG` override the
config file for both GUI and CLI.

## Headless

```
pesforge --find "messi"                        # list matches
pesforge --show 7511                           # every field of one player
pesforge --fields                              # field names + ranges
pesforge --set "Lionel Messi" Speed=99 Age=30  # dry run, prints before/after
pesforge --set 7511 Speed=99 --save            # write the override
pesforge --export roster.csv                   # 26,395 rows, all fields
pesforge --import edits.csv --save             # bulk edit, id column required
pesforge --squad "Arsenal"                     # browse a club
pesforge --export-assignments ass.csv          # id, name, shirt, team_id, club, league_id
pesforge --transfer "David Raya" --to "Chelsea FC" --save
pesforge --shirt "Lionel Messi" 10 --save      # squad number in his club
pesforge --coaches "arteta"                    # search Coach.bin
pesforge --club-coach "Arsenal FC" "Hansi Flick" --save
pesforge --stadium "Arsenal FC" 15 --save      # stadium id from --stadiums
pesforge --stadiums                            # the 37 known stadiums
pesforge --clean-tables blank/                 # Player/Team/PlayerAssignment/Coach, 0 rows
pesforge --import new.csv --append --save      # grow the table with ids it does not have
pesforge --copy "Lionel Messi"                 # one TSV line in --fields order
```

`--set` takes any number of `field=value` pairs; without `--save` it changes nothing on disk.
Every command starts from the **current override** if one exists, so successive runs accumulate
instead of overwriting each other.

CSV import: `id` plus any column from `--fields`; empty cells are left alone. `name` and `shirt`
edit the name slots. Unknown columns are rejected. Unknown ids are skipped with a `skip id N`
line unless `--append` is given, which creates a blank record first.

`--transfer` edits the player's first **club** assignment row. National teams own rows 1–99 in
this database, so a player's country row is left untouched. Rows referencing a team id missing
from `Team.bin` export with a blank club and are counted in the export summary rather than
dropped.

## PES 2015 (dt10.cpk + dt80 data packs)

Retail PES 2015 is a different vintage — 180-byte player records, three name slots, nineteen
abilities, a CRI-encrypted TOC — so it gets its own reader/writer. Edit it two ways:

- **In the app**: Menu → **PES 2015 database…** opens a small editor window — search the
  12k+ players, set the 19 abilities, the bio block (height, weight, age, foot, weak foot,
  form, injury resistance, playing style, nationality), the primary position, the three names
  and the skill / COM-style flags (tick the ones a player has), then **Save to game**. The
  list also shows each player's club. An edit lands in every copy of the player (base
  `Player.bin` and each numbered update slot, in `dt10.cpk` and in the mounted data pack),
  region backups land in `<state>/backups/pes2015`, and the save note lists exactly which
  tables were written. The window finds the install itself and asks for the folder once if it
  cannot. Close PES 2015 while a save writes.
- **Headless**:

```
pesforge2015 --tables                        # what an edit would reach, with row counts
pesforge2015 --find "messi"                  # id, name, position, a few stats
pesforge2015 --show 7511                     # abilities, bio, flags and the three names
pesforge2015 --set 7511 Speed=90 Position=SS            # dry run, prints before/after
pesforge2015 --set 7511 Speed=90 Position=SS --save     # write every table holding him
pesforge2015 --set 7511 Height=180 "Skill: Scissors Feint"=1    # bio + flags via --set too
pesforge2015 --teams "barcelona"             # clubs / national teams: id, kind, name, short
pesforge2015 --set-team 108 Name="FC Barcelona" Short="BARCELONA" --save
pesforge2015 --coaches "mazzarri"            # coach list; Name= / Japanese= to rename
pesforge2015 --squads "L. Messi"             # his club and country rows, with squad numbers
pesforge2015 --set-squad "L. Messi" Team=108 Number=10 --save   # or Club=113 to move him
pesforge2015 --export players.csv            # all tables, with cpk/table columns
pesforge2015 --fields                        # field names + ranges
pesforge2015 --backups                       # what can be undone
pesforge2015 --restore last                  # put the previous bytes back
```

The install folder is found automatically (Wine prefixes, `wineprefixes/*`, Steam);
`PES2015_DIR` overrides it, or pass `--game DIR`. Writes need the game closed.

**What an edit reaches.** The tables live in `Data/dt10.cpk` (base) and in the data pack
`download/DpFileList.bin` names — `dt80_100E.cpk` on this install. Each holds `Player.bin` plus
numbered update slots (`Player1..6.bin`) and the same numbered slots for `Team.bin`,
`Coach.bin` and `PlayerAssignment.bin` (the squads). A `--set`, `--set-team`, `--set-coach`,
`--set-squad` or **Save to game** (window) lands in *every* copy across those tables — dt10 and
the pack disagree on hundreds of squads, so the edit shows whichever file the game reads on
boot.

**Saves are reversible.** Before touching a CPK, the bytes about to change — the TOC, the
header table and the payload region up to whatever the write can reach — are saved under
`<state>/backups/pes2015/`; `--restore` writes them back and truncates the file to its recorded
size, so a save undoes byte for byte.
The game's own compressor beats standard zlib by a few percent, so a re-packed table usually
outgrows its slot and is appended at the end of the CPK with its TOC entry repointed (the file
grows by about one table per save: ~0.5 MB on dt10, ~0.6 MB on the data pack); an edit that
fits is patched in place and the file size does not move.

Format notes (the details live in `pes2015.py`): 180-byte records, id u32 at +0, names at
+0x28/+0x56/+0x84 (46-byte UTF-8 slots), bio fields as bit-packed values (nationality 8 bits
at 32, height at 50 + 132, weight at 57 + 30, age at 192 + 15, playing style 198, form 211,
weak foot 233/241, injury 237, strong foot 279), primary position 4 bits at 0xcf, 19 abilities
as 6-bit fields on a 6-bit grid (value = raw + 40), and one bit per skill / COM style in the
block at 264..291 (+ Early Cross at 95) — the whole map matches ~200 pesdb.net cards
bit-for-bit. `Team.bin` rows are 700 bytes (roster id u16@8 — the id the squads carry — names
at 24/48/234/304/654), `Coach.bin` 108 bytes (id u32@0, names at 16/62), and
`PlayerAssignment.bin` is 4-byte lead + 16-byte rows ([player][team][number + flags]
[counter]) + 12-byte trailer; team ids below 100 are national teams. In the CPK a file's
payload starts at `FileOffset + 0x800`, is `prefix + "WESYS" + u32 compressed + u32
uncompressed + zlib`, and is `FileSize` long; the TOC sits at `0x10 + header.TocOffset`, and
both the TOC and header tables are XOR-obfuscated (the writer re-applies the stream).

## Good to know

**An in-game edit save shadows the database.** PES writes `EDIT00000000` under
`.../Documents/SPGAME/Football Life/2027/save/` the first time you save anything in Edit mode; on
boot the game layers that file over `data_s2627.cpk`, so squads/players/names stored there win
over anything patched here. Move it aside (`mv EDIT00000000 EDIT00000000.off`) to make database
edits visible again — the editor's Club section warns you while such a file is present.

**Writes go into the CPK itself.** PESForge does not use a Sider live-CPK override for the pesdb
tables — an override for these tables ends up listing every player as `--- / Unregistered`, even
for a byte-identical copy of the shipped file — so edits are patched into `download/data_s*.cpk`
directly (the table's own bytes, same length; or appended with its TOC entry repointed when the
recompressed table needs a few extra bytes).

**Any CPK can be the database.** The Club section's Database dropdown lists every `data_s*.cpk`
found on disk; **Import database…** (the dropdown's last entry, or the menu) opens a file picker
for any `.cpk` — a database kept outside a game folder, or a second `data_s*.cpk` in the same
`download/`. The picked file is validated (Player, Team and PlayerAssignment must parse as PES
2021 tables; a PES 2015 CPK is refused) and becomes the file the loads and saves go to, remembered
as `database` in the config — the CLI and `--check` follow it too. Picking an install's own
`data_s*.cpk` hands control back to that folder, and the **Unmount** button beside the dropdown
drops an imported database outright, so the install's own CPK is used again.

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

## Files

| file | purpose |
|---|---|
| `cpk.py` | CRI CPK reader/writer (header + TOC `@UTF` tables, plaintext or XOR-obfuscated) |
| `pesdb.py` | WESYS pack/unpack, CPK table extraction, 312-byte bit-field record codec, team maps |
| `editor.py` | Tkinter UI (and entry point) |
| `cli.py` | headless find / show / set / export / import / squad / transfer / coaches / stadium / shirt / copy |
| `pes2015.py` | PES 2015 tables: 180-byte records, WESYS payloads, CPK patching, backups |
| `cli2015.py` | headless PES 2015 find / show / set / export / tables / backups / restore |
| `settings.py` | config file: game folder, backups, save confirmation, photos, theme, window size |
| `theme.py` | the four palettes and the derived colour tokens (contrast-checked) |
| `pesdb_maps.py` | ability/skill/style tables + eFootball↔PES2021 conversion |
| `data/pesdb_maps.json` | the tables above (generated) |
| `data/countries.json` | nationality names keyed by the record's field |
| `assets/pesforge.png` | window and menu icon |
| `bin/pesforge` | launcher: picks a python with tkinter, sets session vars |
| `bin/pesforge2015` | launcher for the PES 2015 CLI (plain Python, no display) |
| `install.sh` | user-level install: launcher + menu entry, no root |

The pristine table extracted from the CPK is cached at
`~/.local/share/pesforge/Player.vanilla.bin`; delete it to re-extract.

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
