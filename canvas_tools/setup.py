#!/usr/bin/env python3
"""Interactive wizard for local, per-machine canvas_tools defaults — export
format, output directory, a default `--match` term filter, and default
`--new-only`/`--verbose` behavior. Run any time to view/change your
settings. See `canvas_tools/settings.py` for what's stored, where, and how
each command falls back to it; the README's "Local settings" section
explains each one from a user's perspective.
"""
import glob
import os

from canvas_tools.export_course import _DEFAULT_OUT, _ext, _archive_existing, FORMAT_SWITCHABLE_RESOURCES
from canvas_tools.settings import DEFAULTS, has_settings, load_settings, save_settings, settings_path


def _ask(prompt, default=""):
    suffix = f" [{default}]" if default else ""
    reply = input(f"{prompt}{suffix}: ").strip()
    return reply or default


def _ask_yes_no(prompt, default):
    label = "Y/n" if default else "y/N"
    reply = input(f"{prompt} [{label}]: ").strip().lower()
    if not reply:
        return default
    return reply in ("y", "yes")


def _ask_format(current_formats):
    current_label = "both" if len(current_formats) == 2 else current_formats[0]
    while True:
        reply = input(f"Export format for course exports — [Y]aml / [J]son / [B]oth [{current_label}]: ").strip().lower()
        if not reply:
            reply = current_label[0]
        if reply in ("y", "yaml"):
            return ["yaml"]
        if reply in ("j", "json"):
            return ["json"]
        if reply in ("b", "both"):
            return ["yaml", "json"]
        print("Please answer Y, J, or B.")


def _find_stale_files(out_dir, dropped_formats):
    """Every `FORMAT_SWITCHABLE_RESOURCES` file, in one of `dropped_formats`,
    still sitting in an existing course export under `out_dir` — the set a
    format-preference change just made redundant."""
    stale = []
    for course_dir in sorted(glob.glob(os.path.join(out_dir, "course_*"))):
        for basename in FORMAT_SWITCHABLE_RESOURCES:
            for fmt in dropped_formats:
                path = os.path.join(course_dir, f"{basename}.{_ext(fmt)}")
                if os.path.isfile(path):
                    stale.append(path)
    return stale


def _cleanup_stale_formats(out_dir, dropped_formats):
    """Offer to archive or delete files left over from a dropped format,
    one prompt per file with an "apply to all" escape hatch — same shape as
    `OverwritePolicy` in export_course.py, just three choices instead of
    two (archive/delete/leave rather than yes/no/archive)."""
    stale = _find_stale_files(out_dir, dropped_formats)
    if not stale:
        return
    print(
        f"\n{len(stale)} file(s) in the format(s) you just dropped ({', '.join(dropped_formats)}) "
        f"still exist under {out_dir}/:"
    )
    for path in stale:
        print(f"  {path}")
    remembered = None
    archived = deleted = left = 0
    for path in stale:
        decision = remembered
        if decision is None:
            reply = input(f"\n{path!r} — [A]rchive / [D]elete / [L]eave: ").strip().lower()
            while reply not in ("a", "archive", "d", "delete", "l", "leave"):
                reply = input("Please answer A, D, or L: ").strip().lower()
            decision = "archive" if reply.startswith("a") else "delete" if reply.startswith("d") else "leave"
            apply_all = input("Apply that choice to every other stale file too? [y/N]: ").strip().lower()
            if apply_all in ("y", "yes"):
                remembered = decision
        if decision == "archive":
            archived_path = _archive_existing(path)
            print(f"  archived -> {archived_path}")
            archived += 1
        elif decision == "delete":
            os.remove(path)
            print(f"  deleted {path}")
            deleted += 1
        else:
            left += 1
    print(f"\ncleanup done: {archived} archived, {deleted} deleted, {left} left in place")


def main():
    print("canvas_tools setup — local, per-machine defaults (not shared to GitHub)\n")
    old = load_settings() if has_settings() else None
    current = old or dict(DEFAULTS)

    formats = _ask_format(current["formats"])

    out_dir = _ask(
        f"Default output directory (blank = this project's own {_DEFAULT_OUT}/)", current["out_dir"] or ""
    ) or None

    match = _ask(
        "Default --match term filter for --all sweeps, e.g. '26/FA' (blank = none)", current["match"] or ""
    ) or None

    new_only = _ask_yes_no(
        "Default submissions pull / course export --submissions to --new-only (incremental)?", current["new_only"]
    )

    verbose = _ask_yes_no("Default to --verbose (per-item log instead of a progress bar)?", current["verbose"])

    logging_enabled = _ask_yes_no(
        "Write always-verbose run logs to each course's logs/ folder (apply.log, apply_dry_run.log, "
        "export.log) on every apply/export command?",
        current["logging"],
    )

    settings = {
        "formats": formats,
        "out_dir": out_dir,
        "match": match,
        "new_only": new_only,
        "verbose": verbose,
        "logging": logging_enabled,
    }
    save_settings(settings)
    print(f"\nsaved -> {settings_path()}")

    if old is not None:
        dropped = sorted(set(old["formats"]) - set(formats))
        if dropped:
            _cleanup_stale_formats(old["out_dir"] or _DEFAULT_OUT, dropped)


if __name__ == "__main__":
    main()
