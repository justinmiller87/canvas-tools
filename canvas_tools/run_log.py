"""Per-course, per-command run logs — an optional, always-verbose record of
what an apply/export command actually did, independent of whether the
console itself was run with `--verbose`. Enabled via `logging` in local
settings (see `canvas_tools/settings.py`, `canvas_tools/setup.py`); off by
default, so nothing here does anything unless a user opts in.

Layout: `<course folder>/logs/{apply,apply_dry_run,export}.log` — one file
per (action, dry-run) combination, covering every resource for that course
(not one file per resource — a single chronological record of "everything
done to this course" is usually more useful than five separate ones).
`export` has no dry-run counterpart: export commands only ever read from
Canvas, there's nothing to preview, so no `export_dry_run.log` is ever
created. Each run appends a timestamped header rather than overwriting, so
the file builds into a running history across every command run over time.
"""
import os
from datetime import datetime

from canvas_tools.course_paths import _course_folder_name, find_course_export_dir, _DEFAULT_OUT
from canvas_tools.settings import load_settings


class RunLog:
    """One open log file for one command run. `write(msg)` always appends
    a line — independent of console `--verbose`, that's the whole point of
    this over just `print()`. Pass the instance itself as `Progress(...,
    log=...)` to also get one line per item logged unconditionally."""

    def __init__(self, path, description):
        self._f = open(path, "a")
        self.path = path
        self._f.write(f"=== {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} — {description} ===\n")
        self._f.flush()

    def write(self, msg):
        self._f.write(msg.rstrip("\n") + "\n")
        self._f.flush()

    def close(self):
        self._f.write("\n")
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _NullLog:
    """Stand-in for `RunLog` when logging is off, so every call site can
    unconditionally use whatever `open_run_log` returns without an `if
    logging_enabled` check of its own."""

    path = None

    def write(self, msg):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def vprint(msg, verbose, log=None):
    """Print `msg` to the console only if `verbose`, but always write it
    to `log` (a `RunLog`/`_NullLog`, or None) when given — the "regardless
    of the user's verbose choice, logs are always verbose" rule, in one
    place, for the handful of detail lines that live outside a `Progress`
    loop (e.g. a single upload confirmation, not a per-item step)."""
    if verbose:
        print(msg)
    if log is not None:
        log.write(msg)


def _resolve_log_dir(c, course_id, out_parent):
    out_parent = out_parent or _DEFAULT_OUT
    existing = find_course_export_dir(course_id, out_parent)
    if existing:
        course_dir = existing
    else:
        course_code = c.get(f"courses/{course_id}").get("course_code")
        course_dir = os.path.join(out_parent, _course_folder_name(course_id, course_code))
    log_dir = os.path.join(course_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    return log_dir


def open_run_log(c, course_id, resource, action, dry_run=False, out_parent=None, settings=None):
    """Returns a `RunLog` if the user has logging enabled (see
    `canvas_tools.setup`), else a `_NullLog` that quietly does nothing —
    every call site can use the result unconditionally rather than
    checking the setting itself.

    `action` is `"apply"` or `"export"`; `dry_run` is ignored when
    `action == "export"` (no such thing as an export dry-run — see module
    docstring). `resource` (e.g. `"assignments"`, `"submissions"`) is
    folded into the log's header line, not its filename — see the module
    docstring for why files aren't split per-resource. Looks up the
    course's existing export folder first (no extra API call) and only
    falls back to fetching `course_code` from Canvas to create one if this
    course has never been exported locally."""
    settings = settings or load_settings()
    if not settings.get("logging"):
        return _NullLog()
    log_dir = _resolve_log_dir(c, course_id, out_parent or settings.get("out_dir"))
    if action == "apply":
        filename = "apply_dry_run.log" if dry_run else "apply.log"
    else:
        filename = "export.log"
    path = os.path.join(log_dir, filename)
    suffix = " (dry-run)" if action == "apply" and dry_run else ""
    return RunLog(path, f"{resource} {action}{suffix} — course {course_id}")
