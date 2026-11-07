"""The reference vocabulary and derived numbers the panels share.

Ability grouping (the game's own Attacking / Defending / Physical / Goalkeeping split), the
OVR model read through whatever labels a vintage's map uses, the country table, and the
field lists and ranges the batch tray and the widgets clamp to.
"""
import json

import pes2019
import pesdb


def country_names():
    """code -> country name, from the table the PES 2021 side already ships."""
    try:
        import pesdb_maps
        raw = json.loads(pesdb_maps.read_data("countries.json"))
        return {int(code): name for code, name in raw.items()}
    except (OSError, ValueError, AttributeError):
        return {}


COUNTRIES = country_names()
STYLE_NAMES_FALLBACK = getattr(pes2019, "STYLE_NAMES", ())   # 2015's ids, 2019's names


def style_names(mod):
    """A release's playing-style names, or None when it numbers the field instead.

    A map without the attribute (PES 2015) gets the shared 2019 list; a map that defines
    STYLE_NAMES = None (PES 5, whose only style field is the numeric dribble style) is
    saying the row is a number, and the panel then offers a spinbutton.
    """
    return getattr(mod, "STYLE_NAMES", STYLE_NAMES_FALLBACK)


def country_name(mod, code):
    """A nationality code's name, read from the release's own list when it ships one.

    PES 5's country list is the classic-era enum (109 entries, England 6), not the vendor's
    country ids — running one through the other silently shows a plausible wrong country,
    so a module with NATION_NAMES answers for itself.
    """
    names = getattr(mod, "NATION_NAMES", None)
    if names:
        return names[code] if 0 <= code < len(names) else f"#{code}"
    return COUNTRIES.get(code, f"#{code}")


def ability_order(mod):
    """The abilities in the order the map lists them (23 for PES 2019, 19 for PES 2015)."""
    return tuple(mod.ABILITIES)


# The game groups its ratings; the grid follows, so related numbers sit together
# (Attacking → Defending → Physical → Goalkeeping) and each field's tooltip names its group.
# Each group lists every vintage's spelling of its ratings; a label a map does not carry is
# simply never matched.
ABILITY_GROUPS = (
    ("Attacking", ("Offensive Awareness", "Attacking Prowess", "Attack", "Ball Control",
                   "Technique", "Dribbling", "Dribble Accuracy", "Dribble Speed",
                   "Tight Possession", "Low Pass", "Short Pass Accuracy", "Short Pass Speed",
                   "Lofted Pass", "Long Pass Accuracy", "Long Pass Speed", "Finishing",
                   "Shot Accuracy", "Shot Technique", "Place Kicking", "Free Kick", "Curl",
                   "Curling", "Swerve", "Heading", "Header")),
    ("Defending", ("Defensive Awareness", "Defensive Prowess", "Defence", "Ball Winning",
                   "Aggression")),
    ("Physical", ("Speed", "Acceleration", "Explosive Power", "Kicking Power", "Shot Power",
                  "Jump", "Physical Contact", "Balance", "Unwavering Balance", "Body Balance",
                  "Stamina", "Response")),
    ("Goalkeeping", ("GK Awareness", "Goalkeeping", "GK Ability", "GK Catching", "GK Catch",
                     "GK Clearing", "Clearing", "GK Reflexes", "Reflexes", "GK Reach",
                     "Coverage", "Saving")),
)


def ability_group(label):
    """The group a rating belongs to; a label a later map adds lands in Other."""
    for group, labels in ABILITY_GROUPS:
        if label in labels:
            return group
    return "Other"


def grouped_ability_order(mod):
    """`ability_order` with related ratings together: the map's own rows, grouped."""
    order = list(ability_order(mod))
    rank = {name: i for i, (name, _labels) in enumerate(ABILITY_GROUPS)}
    rank["Other"] = len(ABILITY_GROUPS)
    return tuple(label for _i, label in sorted(
        enumerate(order), key=lambda pair: (rank[ability_group(pair[1])], pair[0])))


# The reference-rating model (pesdb.OVR_MODEL) is written in PES 2021's ability names. In 2019
# and 2015 some of those abilities are spelled differently, and a few do not exist: those weights
# fold onto the map's own equivalent, or drop and the rest renormalise. 2015 has one keeper pair
# (Saving / Goalkeeping) where 2021 has five separate ones.
OVR_ALIASES = {
    "pes2019": {"Offensive Awareness": "Attacking Prowess",
                "Defensive Awareness": "Defensive Prowess",
                "Acceleration": "Explosive Power",
                "Balance": "Unwavering Balance",
                "GK Awareness": "Goalkeeping", "GK Reflexes": "Reflexes",
                "GK Reach": "Coverage", "GK Catching": "GK Catch", "GK Clearing": "Clearing",
                "Tight Possession": None, "Aggression": None},
    "pes2015": {"Offensive Awareness": "Attacking Prowess",
                "Defensive Awareness": "Defensive Prowess",
                "Acceleration": "Explosive Power",
                "Balance": "Body Balance",
                "GK Awareness": "Goalkeeping", "GK Reflexes": "Saving", "GK Reach": "Saving",
                "GK Catching": "Saving", "GK Clearing": "Saving",
                "Tight Possession": None, "Aggression": None, "Physical Contact": None},
    # The classic era renamed more than it kept; the five 2021 keeper ratings fold onto the
    # one keeper stat PES 5 has, and the two it lacks outright drop their weight.
    "pes5": {"Offensive Awareness": "Attack", "Defensive Awareness": "Defence",
             "Ball Control": "Technique", "Dribbling": "Dribble Accuracy",
             "Low Pass": "Short Pass Accuracy", "Lofted Pass": "Long Pass Accuracy",
             "Finishing": "Shot Accuracy", "Place Kicking": "Free Kick", "Curl": "Curling",
             "Header": "Heading", "GK Awareness": "GK Ability", "GK Catching": "GK Ability",
             "GK Reflexes": "GK Ability", "GK Reach": "GK Ability",
             "GK Clearing": "GK Ability", "Kicking Power": "Shot Power",
             "Physical Contact": "Balance", "Balance": "Balance", "Speed": "Speed",
             "Acceleration": "Acceleration", "Jump": "Jump", "Stamina": "Stamina",
             "Ball Winning": None, "Tight Possession": None},
}


def overall_rating(table, index, mod):
    """The reference rating for one record, read through the map's own ability names.

    Same arithmetic as `pesdb.overall` (weighted average, rounded half-to-even) — the labels are
    the only reason this exists here rather than there.
    """
    aliases = OVR_ALIASES.get(mod.__name__, {})
    fields = getattr(mod, "FIELD_BY_LABEL", {})
    position = table.get(index, "Registered Position" if "Registered Position" in fields
                         else "Position")
    # a release whose position codes are not the model's own maps them first (PES 5: 2 CWP,
    # 3 CBT, 5 DMF ... against 2021's 1 CB, 4 DMF)
    position = getattr(mod, "OVR_POSITION", {}).get(int(position), position)
    weights = pesdb.OVR_MODEL.get(int(position))
    if not weights:                       # a position the model does not cover: the general six
        names = ("Offensive Awareness", "Defensive Awareness", "Finishing", "Speed",
                 "Physical Contact", "Stamina")
        values = [table.get(index, aliases.get(n, n)) for n in names
                  if aliases.get(n, n) in fields]
        return int(round(sum(values) / len(values))) if values else 0
    total = used = 0.0
    for label, weight in weights:
        target = aliases.get(label, label)
        if target == "Header" and target not in fields:
            target = "Heading"            # 2021 calls the same ability Heading
        if target is None or target not in fields:
            continue                      # the map has no such ability: drop the weight
        total += table.get(index, target) * weight
        used += weight
    return int(round(total / used)) if used else 0


def tier_of(value):
    """A rating's band: under 60, 60-74, 75-84, 85+.

    The *tint* carries the band; the number stays on top of it, so nothing is said by colour
    alone.
    """
    value = int(value)
    if value < 60:
        return 0
    if value < 75:
        return 1
    if value < 85:
        return 2
    return 3


def bulk_fields(mod):
    """The tray's fields: every ability, then the bio numbers the release actually carries."""
    bios = ("Age", "Height", "Weight", "Form", "Weak Foot Usage", "Weak Foot Accuracy",
            "Injury Resistance", "Reputation")
    return ability_order(mod) + tuple(n for n in bios if n in mod.FIELD_BY_LABEL)


def field_range(mod, label):
    """(low, high) for a numeric field: abilities stop at 99 even if their bits allow more."""
    _bit, width, add = mod.FIELD_BY_LABEL[label]
    hi = add + (1 << width) - 1
    if label in ability_order(mod):
        hi = min(hi, 99)
    return add, hi
