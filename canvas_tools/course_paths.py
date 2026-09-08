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
