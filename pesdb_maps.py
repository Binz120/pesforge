"""PES / eFootball ability, skill and playing-style tables.

`data/pesdb_maps.json` holds the lists; they are the authoritative in-game order for each game
version, and our own bit offsets are a separate thing that stays in `pesdb.py`.

Conversion between eFootball and PES 2021 is done on each stat's `PropertyName`, with the few
renames both games made spelled out in `ALIASES`. Stats that exist in only one game are
reported as unmapped — never invented.
"""
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(_HERE, "data", "pesdb_maps.json")


def read_data(name):
    """Bytes of a file the app ships in `data/`.

    Works from the source tree and from inside a single-file zipapp (`.pyz`): in the archive
    `__file__` is a path no `open()` can reach, so the archive itself is read instead.
    """
    if ".pyz" not in _HERE:                       # a real source tree
        with open(os.path.join(_HERE, "data", name), "rb") as f:
            return f.read()
    import zipfile
    with zipfile.ZipFile(_HERE[: _HERE.index(".pyz") + 4]) as z:
        return z.read("data/" + name)


_RAW = json.loads(read_data("pesdb_maps.json"))

ABILITIES = {k: [a["name"] for a in v] for k, v in _RAW["abilities"].items()}
SKILLS = {k: [(s["id"], s["name"]) for s in v] for k, v in _RAW["skills"].items()}
PLAYING_STYLES = list(_RAW["playing_styles"])

# Same stat, different name between the two games (PropertyName -> ours)
ALIASES = {
    "SetPieceTaking": "PlaceKicking",     # eFootball's set-piece stat is PES's Place Kicking
}
_ALIASES_BOTH = dict(ALIASES)
_ALIASES_BOTH.update({dst: src for src, dst in ALIASES.items()})


def abilities(version="PES2021"):
    return ABILITIES.get(version + "Abilities", [])


def skills(version="PES2021"):
    return SKILLS.get(version + "Skills", [])


def _props(version):
    rows = _RAW["abilities"].get(version + "Abilities", [])
    return {a["prop"]: a["name"] for a in rows}


def convert_stats(values, src="EFootball", dst="PES2021"):
    """Map {stat name or PropertyName: value} between the two games.

    Returns {"stats": {dst name: value}, "unmapped": [src names]}. Three stats exist in one game
    only — eFootball's Defensive Engagement and GK Parrying, PES 2021's GK Clearing — and they
    are reported as unmapped rather than guessed at.
    """
    src_props = _props(src)
    dst_props = _props(dst)
    # lookups accept either the display name or the PropertyName
    src_by_name = {a["name"].lower(): a["prop"]
                   for a in _RAW["abilities"].get(src + "Abilities", [])}
    out, unmapped = {}, []
    for key, value in values.items():
        prop = key if key in src_props else src_by_name.get(str(key).lower(), key)
        target = _ALIASES_BOTH.get(prop, prop)
        if target in dst_props:
            out[target] = value
        else:
            unmapped.append(src_props.get(prop, key))
    return {"stats": out, "unmapped": unmapped}


def skill_id(label):
    """In-game skill id for one of our labels, or None when the list has no match."""
    want = _norm(label)
    for skill_id_, name in skills():
        if _norm(name) == want:
            return skill_id_
    return None


def _norm(text):
    return "".join(ch for ch in text.lower() if ch.isalnum())
