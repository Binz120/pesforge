# PESForge

Edits players in **SP Football Life 2027** (PES 2021 engine). Everything is done on files — the
game never needs to be running, and nothing here touches the game process.

The player's face comes from pesmaster (`/pes-2021/graphics/players/player_<id>.png`,
128×128, ~17 KB). It is fetched the first time you select a player and cached under
`~/.local/share/pesforge/portraits/`, so nothing is bundled and offline runs simply show
"no photo" for players you have not opened yet.

The window opens on **workspace 3** on Hyprland without pulling the view across
(`PESFORGE_PARK=<n>` overrides the workspace, an empty value keeps it on the current one).
Everything is on one page and sized to fit a 1500×950 window: the list, the player form and the
skills/club column. The form grids pick their own column counts from the pane widths they are
given, so a wider window shows more per row instead of scrolling; a window squeezed smaller than
the design gets a scrollbar back rather than a clipped field.

```bash
pesforge                  # GUI  (desktop entry: "PESForge")
pesforge --find messi     # headless, no window at all
```

## Requirements and installing on any distro (running from source)

Linux with Python 3.8+ and **tkinter**. Tk is the only GUI dependency; the app has no pip
packages and stores nothing outside XDG directories.

```bash
sudo apt install python3-tk          # Debian, Ubuntu, Mint, Pop!_OS
sudo dnf install python3-tkinter     # Fedora
sudo pacman -S tk                    # Arch, Manjaro, EndeavourOS
sudo zypper install python3-tk       # openSUSE
```

```bash
git clone <this repo> && cd pesforge
./install.sh                         # ~/.local/bin/pesforge + a menu entry, no root
pesforge --check                # what it found: python, Tk, display, game folder
./install.sh --uninstall             # removes both; config and backups stay
```

The launcher works from any directory and picks the first interpreter with tkinter, preferring
the distro's `/usr/bin/python3` (that build carries the system fontconfig). Override with
`SPFL_PYTHON=/path/to/python3` or point at a checkout elsewhere with `SPFL_EDITOR_DIR=…`.

Any desktop works: Tk draws through X11, which Wayland sessions provide via XWayland (GNOME, KDE,
XFCE, Cinnamon, sway, Hyprland, niri all fine). On a bare Wayland compositor **without**
XWayland, run it under `xwayland-run` or use the headless CLI.

The game folder is found on its own: on first run (or whenever the configured folder no longer
holds a `download/data_s*.cpk`) `~/Games`, the Steam/Lutris libraries, `/run/media` and `/media`
are searched a few levels deep, the newest `data_s*.cpk` wins (any data pack name), and the path is written into
`~/.config/pesforge/config.json` so CLI and GUI agree. If nothing is found the first run asks you to pick the game folder and remembers it. `SPFL_DIR` still overrides everything.

Edits are patched **into the game's `download/data_s*.cpk` itself** (the table's own bytes, same length;
or appended with its TOC entry repointed when the recompressed table needs a few extra bytes).
Changes load the next time the game starts. The CPK is copied to
`~/.local/share/pesforge/backups/` before every write, and **Vanilla** restores the pristine
snapshot taken on first run.

**An in-game edit save shadows the database.** PES writes `EDIT00000000` under
`.../Documents/SPGAME/Football Life/2027/save/` the first time you save anything in Edit mode; on
boot the game layers that file over `data_s2627.cpk`, so squads/players/names stored there win over
anything patched here. Move it aside (`mv EDIT00000000 EDIT00000000.off`) to make database edits
visible again, and the editor's Club section warns you while such a file is present.

A Sider live-CPK override does *not* work for pesdb tables on this install: the file is served
(`livecpk file found` in `sider.log`) but the game then lists every player as
`--- / Unregistered`, even for a byte-identical copy of the shipped file.

## GUI

Search by name, shirt name or ID. A query that matches club names lists **clubs** instead
(team ID, name, squad size) — click one, or press Enter for the first, to get its squad. Club search is accent-blind
(`munchen` finds Bayern München) and takes nicknames — `man u`, `utd`, `spurs`, `barca`, `juve`,
`psg`, `bvb`, `toon`, `wolves`, `barca`, `atleti`, `naples` — with a partial alias working too
(`man` lists both Manchester clubs). Click a
column header to sort by ID, name, club, position, OVR or age (click again to reverse). → edit →
**Save to game**. The app re-reads what it wrote and reports the
record count in the status bar. **Vanilla** deletes the override. Earlier versions land in
`~/.local/share/pesforge/backups/`.

The **Club** box is a type-ahead search, not a 745-item dropdown: type `arse` and the list narrows
to Arsenal FC *and* Olympique Marseille, `united` finds all 15 Uniteds, an exact name like
`Arsenal FC` pins one squad. Typed text filters the dropdown as you go and the table after a
short pause; Enter applies immediately. It reads the `PlayerAssignment.bin` override if one
exists, so transfers show up here too.

Filter the list by **Club** and **Country** (the player's nationality, named from `data/countries.json`,
214 countries keyed by the record's Nationality field).

Editable per player: 25 abilities (40–99) with value bars, age/height/weight/nationality/form/weak
foot usage & accuracy/injury resistance/stronger foot, registered position, playing style,
13 position ratings, 48 skills, full + shirt name, nine celebration/motion slots, the
**career** block (youth club, owner club, contract until, loan until, market value, national
caps, reputation, playing attitude, legend / hand / Ballon d'Or flags), and the
**Club** record. Club names come from `Team.bin` + `PlayerAssignment.bin`.

Form, the weak-foot sliders, injury resistance, reputation and playing attitude all read
1-based, exactly as the game shows them (they are stored one lower).

Menu entries **Coaches…** and **Appearance of the selected player…** open two more editors:

- **Coaches** — all 980 `Coach.bin` records, searchable by name or club: name, Japanese name,
  nationality and adaptability (40–99), with the club each one is linked to.
- **Appearance** — the selected player's `PlayerAppearance.bin` record, grouped by head/eyes/
  eyebrows/nose/mouth/facial hair/hair/physique/kit, each field clamped to its own range.

### Club

Five things in the Club section:

- **Transfer** — writes the player's first *club* `PlayerAssignment.bin` row (national rows
  ≤ 99 are never touched). A player with no club row gets one appended rather than an error.
- **Coach** — the club's coach link (`Team.bin` +0), set from the 980-coach list.
- **Stadium** — the club's home stadium (`Team.bin` +60), picked from the 37 stadiums in
  `Stadium.bin`, names from the table itself.
- **Squad no.** — the number the player wears in this club (`PlayerAssignment.bin` +12,
  stored one below the number the game shows).
- **Club name** — edits the English (70 bytes) and short (10) name slots of the
  club in `Team.bin`; the club filter and the list update immediately.

Every section saves into the same CPK write: only tables you actually changed are re-packed, each one
re-read and its row count compared before the status line reports `ok`.

### OVR

The list and the player header show a rating computed from a per-position model
(`pesdb.OVR_MODEL`, `pesdb.overall`): weighted abilities for the running position, rounded
half-to-even. Messi reads 85. It is a model of its own, not the game's
own hidden overall — PES 2021 stores no rating in the record.

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
this database, so a player's country row is left untouched; `player_teams` ignores those rows
for the club label. Rows referencing a team id missing from `Team.bin` export with a blank club
and are counted in the export summary rather than dropped.

## Settings

The **Menu** button in the header (or `Ctrl+,`) opens:

| Setting | Meaning |
|---|---|
| Game folder | Folder holding `download/data_s2627.cpk`; checked before it is accepted |
| Backup folder | Where previous `Player.bin` versions go; blank = `~/.local/share/pesforge/backups` |
| Keep backups | Newest N are kept, older ones pruned on save; `0` keeps everything |
| Ask before writing the game file | Confirmation dialog before an override is written |

Saved to `~/.config/pesforge/config.json` and applied immediately (the table is re-read).
The CLI reads the same file for the game folder and backup cap; `SPFL_DIR`, `SPFL_STATE` and
`SPFL_CONFIG` still override it.

Menu also has: open the backups folder, reload the database, restore vanilla, export and import a
roster CSV, bulk edit and compare the selected players, the coach and appearance editors, and about.

## Ability and skill tables (eFootball ↔ PES 2021)

`data/pesdb_maps.json` carries the per-version ability, skill and playing-style lists;
`pesdb_maps.py` loads them: `abilities(version)`, `skills(version)`, `PLAYING_STYLES`,
`convert_stats(values, src, dst)` and `skill_id(label)`. The PES 2021 list is the game's own
25-stat order and matches `pesdb.ATTRS` label for label — a second opinion on our bit table.

Conversion is by each stat's `PropertyName`, with one rename (`SetPieceTaking` ↔ `PlaceKicking`
in both directions). Three stats exist in only one game — eFootball's *GK Parrying* and
*Defensive Engagement*, PES 2021's *GK Clearing* — and are returned in `unmapped` rather than
invented. Skill ids resolve by normalised name (case/punctuation aside), so `Chip shot control`
finds id 12; the 12 FL27 skills the list lacks return `None`.

## Files

| file | purpose |
|---|---|
| `cpk.py` | CRI CPK reader (header + TOC `@UTF` tables) |
| `pesdb.py` | WESYS pack/unpack, CPK table extraction, 312-byte bit-field record codec, team maps |
| `editor.py` | Tkinter UI (and entry point) |
| `cli.py` | headless find / show / set / export / import / squad / transfer / coaches / stadium / shirt / copy |
| `settings.py` | config file: game folder, backup folder, backup cap, save confirmation |
| `pesdb_maps.py` | ability/skill/style tables + eFootball↔PES2021 conversion |
| `data/pesdb_maps.json` | the tables above (generated) |
| `data/countries.json` | nationality names keyed by the record's field |
| `assets/pesforge.png` | window and menu icon |
| `bin/pesforge` | launcher: picks a python with tkinter, sets session vars |
| `install.sh` | user-level install: launcher + menu entry, no root |

The pristine table extracted from the CPK is cached at
`~/.local/share/pesforge/Player.vanilla.bin`; delete it to re-extract.

## Format notes (PES 2021 / FL)

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
- Sider logs `read_file:: livecpk file found: …\pesdb\Player.bin` when the override is in use
  (`debug = 1` in `sider.ini` is needed for file-level lines).
