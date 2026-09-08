"""Local, per-machine defaults for canvas_tools commands — export format,
output directory, a default `--match` term filter, and default
`--new-only`/`--verbose` behavior. Read by `export_course.py` and `cli.py`
as fallbacks for their equivalent flags when the flag isn't given on the
command line; an explicit flag always wins over whatever's stored here.

Lives in `.canvas_tools_settings.json` at the project root, gitignored —
this is a personal workflow preference (which format you like, your usual
term filter), not something to share across machines or collaborators, so
it's kept out of version control the same way `.env`/`.env.local` are.

Created/edited via `python3 -m canvas_tools.setup` (shortcut: `setup`).
"""
import json
import os

_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".canvas_tools_settings.json")

# `out_dir: None` means "use export_course._DEFAULT_OUT" (this project's own
# exports/ folder) rather than baking that path in here too.
DEFAULTS = {
    "formats": ["yaml", "json"],
    "out_dir": None,
    "match": None,
    "new_only": False,
    "verbose": False,
    "logging": False,
}


def settings_path():
    return _SETTINGS_PATH


def has_settings():
    return os.path.isfile(_SETTINGS_PATH)


def load_settings():
    """Always returns every key in `DEFAULTS`, filling in from the stored
    file where present — a settings file written by an older version of
    `setup` (missing a key this version added) doesn't need migrating."""
    merged = dict(DEFAULTS)
    if has_settings():
        with open(_SETTINGS_PATH) as f:
            merged.update(json.load(f))
    return merged


def save_settings(settings):
    with open(_SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
