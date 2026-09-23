"""Course export-folder naming/lookup, shared by `export_course.py` and
`run_log.py` — split out into its own module (rather than living in
`export_course.py`, where it originated) purely to avoid an import cycle:
`export_course.py` imports from `submissions.py` and `rubrics.py` at module
level, and both of those need these helpers (for log-folder resolution),
so this module has no dependency on either and sits underneath all three.
"""
import glob
import os

# Derived from this file's own location, not the current working directory —
# so the default --out always lands in this project's real exports/ folder,
# even when run from inside a course's own exports subfolder (where a bare
# relative "exports" would instead create a stray nested exports/exports/...
# right there). An explicit --out is unaffected by this and still resolves
# relative to wherever you actually are, same as --file always has.
_DEFAULT_OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "exports")


class UnencryptedOutputError(RuntimeError):
    """The export output location isn't safely inside the encrypted folder."""


def _crypto_root():
    # Override with CANVAS_TOOLS_CRYPTO_ROOT if pCloud is mounted elsewhere.
    return os.path.realpath(os.environ.get("CANVAS_TOOLS_CRYPTO_ROOT") or os.path.expanduser("~/pCloudDrive/Crypto Folder"))


def _on_fuse(path):
    """True if `path` sits on a FUSE mount (pCloud's drive is one) — so a plain
    local directory left behind at the same path while pCloud isn't running
    doesn't pass for the encrypted folder. Linux-only; where /proc/mounts
    can't be read, returns True (skips this check) rather than blocking."""
    try:
        with open("/proc/mounts") as f:
            mounts = [line.split()[1:3] for line in f]
    except OSError:
        return True
    best_len, best_type = -1, ""
    for mountpoint, fstype in mounts:
        mountpoint = mountpoint.replace("\\040", " ")
        if (path == mountpoint or path.startswith(mountpoint.rstrip("/") + "/")) and len(mountpoint) > best_len:
            best_len, best_type = len(mountpoint), fstype
    return best_type.startswith("fuse")


def require_encrypted_out(out_parent, allow_unencrypted=False):
    """Refuse to write student data anywhere but the encrypted pCloud folder.

    `exports/` is normally a symlink into the Crypto Folder, but a fresh
    clone, or a deleted/replaced link, leaves nothing there — and the
    exporters' `os.makedirs(..., exist_ok=True)` would then quietly create a
    real, unencrypted local `exports/`. Raises `UnencryptedOutputError` if
    `out_parent` doesn't resolve inside the Crypto Folder, or if the Crypto
    Folder isn't mounted/unlocked right now."""
    if allow_unencrypted:
        return
    root = _crypto_root()
    real = os.path.realpath(out_parent)
    if real != root and not real.startswith(root + os.sep):
        raise UnencryptedOutputError(
            f"refusing to write student data to {real}: it isn't inside the encrypted folder ({root}). "
            "Re-create the exports symlink, or pass --allow-unencrypted to write here anyway."
        )
    try:
        os.listdir(root)
    except PermissionError:
        raise UnencryptedOutputError(f"the encrypted folder ({root}) is locked — unlock it in pCloud and retry.") from None
    except OSError:
        raise UnencryptedOutputError(f"the encrypted folder ({root}) isn't available — is pCloud running and mounted?") from None
    if not _on_fuse(root):
        raise UnencryptedOutputError(
            f"{root} is on a local filesystem, not the pCloud mount — pCloud isn't running, or this is a stray local copy."
        )


def _course_folder_name(course_id, course_code):
    # Course codes look like "26/FA CIS-617-OL01" — "/" isn't valid in a
    # directory name, and leaving it as a bare id ("course_10001") is
    # unreadable months later when you don't remember which id was which
    # section. "/" -> "-", " " -> "_" gives "course_10001_26-FA_XXX-100-OL01".
    safe_code = (course_code or "").replace("/", "-").replace(" ", "_")
    return f"course_{course_id}_{safe_code}" if safe_code else f"course_{course_id}"


def find_course_export_dir(course_id, out_parent=_DEFAULT_OUT):
    """Locate a course's export directory by id alone (no course_code, so no
    extra API call), by globbing for `_course_folder_name`'s pattern. Returns
    the path, or None if this course has never been exported locally."""
    matches = glob.glob(os.path.join(out_parent, f"course_{course_id}_*"))
    return matches[0] if matches else None
