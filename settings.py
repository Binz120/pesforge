"""User settings shared by the GUI and the CLI.

Stored in ~/.config/pesforge/config.json (override with SPFL_CONFIG). Env vars still win:
SPFL_DIR overrides the game folder, SPFL_STATE the state folder.
"""
import json
import os
import shutil

VERSION = "1.0"
APP_NAME = "PESForge"

CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
DATA_HOME = os.environ.get("XDG_DATA_HOME") or "~/.local/share"

CONFIG_PATH = os.path.expanduser(os.environ.get(
    "SPFL_CONFIG", os.path.join(CONFIG_HOME, "pesforge/config.json")))
OLD_CONFIG_PATH = os.path.expanduser(os.path.join(CONFIG_HOME, "spfl27-editor/config.json"))
DEFAULT_GAME_DIR = ""            # empty = find it (see game_dir/find_databases)
DEFAULT_STATE_DIR = os.path.join(DATA_HOME, "pesforge")
OLD_STATE_DIR = os.path.join(DATA_HOME, "spfl27-editor")


def _adopt_old_home():
    """First run under the new name: bring the spfl27-editor config and state along (once)."""
    if os.environ.get("SPFL_CONFIG") or os.environ.get("SPFL_STATE"):
        return                   # an explicit path wins; there is nothing to adopt
    if not os.path.exists(CONFIG_PATH) and os.path.exists(OLD_CONFIG_PATH):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        shutil.copy2(OLD_CONFIG_PATH, CONFIG_PATH)
    if not os.path.isdir(DEFAULT_STATE_DIR) and os.path.isdir(OLD_STATE_DIR):
        try:
            os.rename(OLD_STATE_DIR, DEFAULT_STATE_DIR)     # portraits, backups, pristine CPK
        except OSError:
            pass


_adopt_old_home()

DEFAULTS = {
    "game_dir": DEFAULT_GAME_DIR,
    "backups_dir": "",          # empty = <state>/backups
    "max_backups": 50,          # 0 keeps everything
    "confirm_save": True,
    "photos": True,             # fetch the player's face from pesmaster on first view
}


# Where a game install usually lives. Each root is walked a few levels down looking for
# download/data_s*.cpk, which is what SP Football Life (and PES 2021 mods) ship.
SEARCH_ROOTS = (
    "~/Games", "~/games",
    "~/.local/share/Steam/steamapps/common", "~/.steam/steam/steamapps/common",
    "~/.local/share/lutris/games", "~/Documents",
    "/run/media", "/media",
)
MAX_DEPTH = 4
SKIP_DIRS = {"node_modules", "shadercache", "compatibilitytools.d", "__pycache__",
             "wineprefixes", "prefix", ".git"}


def find_databases(roots=None):
    """Every data_s*.cpk under the usual install folders, newest first."""
    hits = []
    roots = SEARCH_ROOTS if roots is None else roots
    for root in roots:
        root = os.path.expanduser(root)
        if not os.path.isdir(root):
            continue
        base = root.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root):
            if dirpath.count(os.sep) - base >= MAX_DEPTH:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]
            for name in filenames:
                if name.startswith("data_s") and name.endswith(".cpk"):
                    hits.append(os.path.join(dirpath, name))
    hits.sort(key=lambda p: (os.path.basename(p) != "data_s2627.cpk", -os.path.getmtime(p)
                             if os.path.exists(p) else 0))
    return hits


def edit_save_files():
    """PES saves an in-game edit file that overrides the database on load.

    Any hit means values edited here can be shadowed by what the game saved from Edit mode.
    """
    import glob
    patterns = (
        "~/.local/share/wineprefixes/*/drive_c/users/*/Documents/SPGAME/Football Life/*/save/EDIT*",
        "~/.local/share/wineprefixes/*/drive_c/users/*/Documents/KONAMI/*/save/EDIT*",
        "~/Documents/KONAMI/*/save/EDIT*",
    )
    hits = []
    for pattern in patterns:
        for path in glob.glob(os.path.expanduser(pattern)):
            if os.path.basename(path) == "EDIT00000000" and os.path.isfile(path):
                real = os.path.realpath(path)        # wine user dirs are often symlinks
                if real not in [os.path.realpath(h) for h in hits]:
                    hits.append(path)
    return hits


def database_path(folder):
    """The game's data CPK: `download/data_s2627.cpk`, or whatever data_s*.cpk is shipped.

    PES data packs are named per release, so the file must be discovered rather than assumed.
    """
    dl = os.path.join(os.path.expanduser(folder or ""), "download")
    try:
        names = [n for n in os.listdir(dl) if n.startswith("data_s") and n.endswith(".cpk")]
    except OSError:
        names = []
    names.sort(key=lambda n: (n != "data_s2627.cpk", n))
    return os.path.join(dl, names[0] if names else "data_s2627.cpk")


def has_database(folder):
    """True when the folder holds a data CPK."""
    return bool(folder) and os.path.isfile(database_path(folder))


def load():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}, f, indent=2)
    return CONFIG_PATH


def state_dir():
    return os.path.expanduser(os.environ.get("SPFL_STATE") or DEFAULT_STATE_DIR)


def game_dir(cfg=None):
    """Game folder: SPFL_DIR, else the configured one, else whatever this machine has.

    On a fresh install (or after the game moved) the configured folder holds no database, so
    the usual install roots are searched and the winner is written back to the config.
    """
    env = os.environ.get("SPFL_DIR")
    if env and has_database(env):          # an override that points nowhere is stale
        return env
    cfg = cfg if cfg is not None else load()
    folder = cfg["game_dir"]
    if has_database(folder):
        return folder
    for cpk in find_databases():
        found = os.path.dirname(os.path.dirname(cpk))
        cfg["game_dir"] = found
        try:
            save(cfg)
        except OSError:
            pass
        return found
    return folder


def backups_dir(cfg=None, state=None):
    cfg = cfg or load()
    return os.path.expanduser(cfg["backups_dir"]) if cfg["backups_dir"] \
        else os.path.join(state or state_dir(), "backups")


def prune_backups(directory, keep):
    """Delete all but the newest `keep` files. 0 or less keeps everything."""
    if not keep or keep <= 0 or not os.path.isdir(directory):
        return 0
    files = [os.path.join(directory, f) for f in os.listdir(directory)]
    files = sorted((f for f in files if os.path.isfile(f)),
                   key=os.path.getmtime, reverse=True)
    removed = 0
    for path in files[keep:]:
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed
