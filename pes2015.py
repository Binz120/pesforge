"""PES 2015 database layout (Player.bin), recovered from the game's own tables.

Source: an installed PES 2015 (Data/dt10.cpk, updated by download/dt80_*.cpk). The CPK
tables are CRI's encrypted @UTF variant and are read by cpk.Utf (see the decrypt_utf
stream); this module only describes the 180-byte player record inside them.

How the map was found: the game's CPKs were decrypted, Player.bin was located in dt10 /
dt80 (10,091 and 11,707 rows of 180 bytes), then each candidate 6-bit field was compared
with the PES 2015 stats of known players (pesdb.net) and cross-checked against the same
players' ids in a PES 2021 database. Goalkeeping and Saving match exactly; the outfield
values sit a few points under pesdb.net for the same player, which is the difference
between the shipped data pack and the final online ratings — the fields are right.

PES 2015 has nineteen abilities (no Tight Possession, Physical Contact, Aggression, or
the five separate goalkeeping stats that PES 2020/21 carries).

    record  180 bytes, little-endian bit fields counted from bit 0
    id      u32 at +0
    names   +0x28 Japanese | +0x56 shirt | +0x84 full, 46-byte UTF-8 slots

The nineteen abilities sit on a 6-bit grid in four blocks; every field is six bits and
the value is the raw bits plus 40 (so the database stores 0..59 for 40..99).
"""
REC = 180
ID_OFF = 0
NAME_OFFS = {"japanese": 0x28, "shirt": 0x56, "full": 0x84}
NAME_LEN = 46

# primary position: 4 bits, 0 GK, 1 CB, 2 LB, 3 RB, 4 DMF, 5 CMF, 6 LMF, 7 RMF, 8 AMF,
# 9 LWF, 10 RWF, 11 SS, 12 CF. Checked against C. Ronaldo (9 LWF), L. Messi (11 SS),
# M. Neuer (0 GK), S. Ramos (1 CB) and G. Buffon (0 GK).
PRIMARY_POSITION = 0xCF
POSITION_NAMES = ("GK", "CB", "LB", "RB", "DMF", "CMF", "LMF", "RMF",
                  "AMF", "LWF", "RWF", "SS", "CF")

# label -> bit offset (width 6, value = raw + 40)
ABILITIES = {
    "Place Kicking": 0x47,
    "Low Pass": 0x4D,
    "Defensive Prowess": 0x53,
    "Ball Control": 0x59,
    "Header": 0x60,
    "Jump": 0x66,
    "Speed": 0x6C,
    "Ball Winning": 0x72,
    "Saving": 0x78,
    "Goalkeeping": 0x80,
    "Swerve": 0x86,
    "Stamina": 0x8C,
    "Explosive Power": 0x92,
    "Dribbling": 0x98,
    "Kicking Power": 0xA0,
    "Attacking Prowess": 0xA6,
    "Body Balance": 0xAC,
    "Finishing": 0xB2,
    "Lofted Pass": 0xB8,
}
ABILITY_ADD = 40
ABILITY_WIDTH = 6
