#!/usr/bin/env python3
"""Reverse-export a live Canvas course into the assignments.yaml / modules.yaml
schema used by `canvas assignments apply` / `canvas modules apply`, so a real
built course can be inspected (or reused as a template) locally.
"""
import argparse
import glob
import json
import locale
import os
import re
from datetime import datetime, timedelta

import yaml

from canvas_tools.client import CanvasClient, CanvasError
from canvas_tools.html_clean import clean_html
from canvas_tools.progress import Progress
from canvas_tools.rubrics import export_rubrics_csv

# Derived from this file's own location, not the current working directory —
# so the default --out always lands in this project's real exports/ folder,
# even when run from inside a course's own exports subfolder (where a bare
# relative "exports" would instead create a stray nested exports/exports/...
# right there). An explicit --out is unaffected by this and still resolves
# relative to wherever you actually are, same as --file always has.
_DEFAULT_OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "exports")

ASSIGNMENT_FIELDS = [
    "name",
    "description",
    "points_possible",
    "due_at",
    "unlock_at",
    "lock_at",
    "submission_types",
    "published",
    "allowed_extensions",
    "allowed_attempts",
    "omit_from_final_grade",
    "use_rubric_for_grading",
    "grade_group_students_individually",
    "peer_reviews",
    "automatic_peer_reviews",
    "anonymous_peer_reviews",
    "intra_group_peer_reviews",
    "peer_review_count",
    "peer_reviews_assign_at",
]


class LiteralStr(str):
    pass


def _literal_str_representer(dumper, data):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


yaml.add_representer(LiteralStr, _literal_str_representer)


def dump_data(data, path, header_comment=None):
    """Write `data` to `path` as YAML (default) or JSON, based on the `.json`
    extension on `path`. Every `apply` command already reads either format
    for free — YAML is a syntactic superset of JSON, so `yaml.safe_load()`
    parses a plain JSON file correctly with no code changes needed (verified
    directly, not assumed) — this is only needed for the write side.
    `header_comment` (the "# Exported from course X..." banner some files
    get) is YAML-only, since JSON has no comment syntax; it's silently
    omitted for JSON output rather than corrupting the file."""
    if path.lower().endswith(".json"):
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
    else:
        with open(path, "w") as f:
            if header_comment:
                f.write(header_comment)
            yaml.dump(data, f, sort_keys=False, allow_unicode=True, width=100)


def _local_date_order():
    """'DM' if the current user's locale conventionally writes a numeric date
    day-before-month (e.g. 31/12/2026), 'MD' otherwise — also the fallback
    when the locale can't be read (nl_langinfo isn't available on every
    platform, and a locale may not be fully configured in the shell this
    runs from)."""
    try:
        locale.setlocale(locale.LC_TIME, "")
        fmt = locale.nl_langinfo(locale.D_FMT)
    except (AttributeError, locale.Error, ValueError):
        return "MD"
    d_pos, m_pos = fmt.find("%d"), fmt.find("%m")
    if d_pos == -1 or m_pos == -1:
        return "MD"
    return "DM" if d_pos < m_pos else "MD"


def _archive_timestamp():
    pattern = "%Y-%d-%m_%H-%M-%S" if _local_date_order() == "DM" else "%Y-%m-%d_%H-%M-%S"
    return datetime.now().strftime(pattern)


def _archive_existing(path):
    """Move an existing file into the course's single `archive/` folder,
    renamed with a local timestamp, so `write_with_confirmation`'s "archive"
    choice never just silently clobbers the old copy. Every archived file
    for a course lands in one shared `archive/` next to that course's own
    resource files — including rubric CSVs, which live one level down in
    `rubrics/`, so their archive folder is hoisted up to the course level
    too rather than creating a second, separate `rubrics/archive/`."""
    directory = os.path.dirname(path) or "."
    if os.path.basename(directory) == "rubrics":
        directory = os.path.dirname(directory) or "."
    archive_dir = os.path.join(directory, "archive")
    os.makedirs(archive_dir, exist_ok=True)
    base, ext = os.path.splitext(os.path.basename(path))
    archived_path = os.path.join(archive_dir, f"{base}_{_archive_timestamp()}{ext}")
    os.rename(path, archived_path)
    return archived_path


def _prompt_overwrite(path):
    reply = input(f"{path!r} already exists. [Y]es/[N]o/[A]rchive: ").strip().lower()
    while reply not in ("y", "yes", "n", "no", "a", "archive"):
        reply = input("Please answer Y, N, or A: ").strip().lower()
    return reply


class OverwritePolicy:
    """Remembers a single Y/N/A overwrite decision across every file written
    in one run, once the user opts into that after the first prompt — so a
    full course export re-run doesn't have to answer the same "already
    exists" question for every one of a dozen-plus files. Pass the same
    instance to every write call across a run; leave it as None (the
    default everywhere) for a one-off write, like a single `assignments
    export`, where "apply to everything else" wouldn't mean anything."""

    def __init__(self):
        self.remembered = None

    def decide(self, path):
        if self.remembered is not None:
            return self.remembered
        decision = _prompt_overwrite(path)
        apply_all = input("Apply that choice to every other file in this run too? [y/N]: ").strip().lower()
        if apply_all in ("y", "yes"):
            self.remembered = decision
        return decision


def _resolve_write_decision(path, policy=None):
    """Shared no-blind-overwrite gate for a single file: prompts (and
    archives, if asked) when `path` already exists, before the caller does
    the actual write. Returns 'write' or 'skip'."""
    if not os.path.exists(path):
        return "write"
    reply = policy.decide(path) if policy is not None else _prompt_overwrite(path)
    if reply in ("n", "no"):
        print(f"skipped {path!r}")
        return "skip"
    if reply in ("a", "archive"):
        archived = _archive_existing(path)
        print(f"archived old copy -> {archived!r}")
    return "write"


def write_with_confirmation(data, path, header_comment=None, policy=None):
    """`dump_data`, but never a blind overwrite: if `path` already exists,
    ask whether to overwrite it in place, skip it entirely, or archive the
    old copy first (see `_archive_existing`). Returns False if the file was
    left untouched (the user chose to skip it), True otherwise. Pass an
    `OverwritePolicy` shared across a whole run to avoid re-prompting for
    every file once the user opts into "apply to everything else"."""
    if _resolve_write_decision(path, policy) == "skip":
        return False
    dump_data(data, path, header_comment=header_comment)
    return True


def write_text_with_confirmation(path, text, newline=None, policy=None):
    """Same no-blind-overwrite protection as `write_with_confirmation`, for
    plain-text output (e.g. rubric CSVs) that doesn't go through
    `dump_data`. Returns False if the file was left untouched, True
    otherwise."""
    if _resolve_write_decision(path, policy) == "skip":
        return False
    with open(path, "w", newline=newline) as f:
        f.write(text)
    return True


_ARCHIVE_FILENAME_RE = re.compile(r"^(?P<base>.+)_(?P<ts>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})(?P<ext>\.[^.]*)?$")


def _archive_timestamp_patterns():
    """Try the current locale's date order first (most likely correct, since
    cleanup normally runs on the same machine that did the archiving), then
    the other order as a fallback for a file archived under a different
    locale — an out-of-range month/day in the wrong order fails to parse
    cleanly, so trying both is safe rather than ambiguous."""
    primary = "%Y-%d-%m_%H-%M-%S" if _local_date_order() == "DM" else "%Y-%m-%d_%H-%M-%S"
    other = "%Y-%m-%d_%H-%M-%S" if primary.startswith("%Y-%d") else "%Y-%d-%m_%H-%M-%S"
    return [primary, other]


def find_archive_dirs(root):
    """Every directory literally named `archive` found anywhere under
    `root` (there can be several — one per resource directory, plus one
    under each course's own `rubrics/` subfolder)."""
    found = []
    for dirpath, dirnames, _filenames in os.walk(root):
        if os.path.basename(dirpath) == "archive":
            found.append(dirpath)
            dirnames[:] = []  # don't descend into an archive dir looking for nested ones
    return sorted(found)


def list_archived_files(archive_dirs):
    """Every file across the given archive directories, with its archived-at
    timestamp (parsed from the filename this tool itself writes) and the
    "identity" it's a copy of — (archive_dir, base name, extension) — used
    to group multiple archived copies of the same original file together.
    Falls back to the file's mtime, and its bare filename as the identity,
    for anything in there that doesn't match this tool's own naming
    pattern (e.g. a file a person dropped in there by hand)."""
    entries = []
    for archive_dir in archive_dirs:
        for filename in sorted(os.listdir(archive_dir)):
            path = os.path.join(archive_dir, filename)
            if not os.path.isfile(path):
                continue
            m = _ARCHIVE_FILENAME_RE.match(filename)
            when = None
            identity = (archive_dir, filename, "")
            if m:
                identity = (archive_dir, m.group("base"), m.group("ext") or "")
                for pattern in _archive_timestamp_patterns():
                    try:
                        when = datetime.strptime(m.group("ts"), pattern)
                        break
                    except ValueError:
                        continue
            if when is None:
                when = datetime.fromtimestamp(os.path.getmtime(path))
            entries.append({"path": path, "when": when, "identity": identity})
    return entries


def select_for_cleanup(entries, mode, cutoff=None):
    """Which of `entries` (from `list_archived_files`) `mode` selects for
    deletion:
    - "all": every entry.
    - "keep_recent": every entry except the newest `when` per identity group
      (so exactly one archived copy of each original file survives).
    - "older_than": every entry with `when` before `cutoff`.
    """
    if mode == "all":
        return list(entries)
    if mode == "older_than":
        return [e for e in entries if e["when"] < cutoff]
    if mode == "keep_recent":
        newest_by_identity = {}
        for e in entries:
            cur = newest_by_identity.get(e["identity"])
            if cur is None or e["when"] > cur["when"]:
                newest_by_identity[e["identity"]] = e
        keep_paths = {e["path"] for e in newest_by_identity.values()}
        return [e for e in entries if e["path"] not in keep_paths]
    raise ValueError(f"unknown cleanup mode: {mode!r}")


TIME_WINDOWS = {
    "1": ("last day", timedelta(days=1)),
    "2": ("last week", timedelta(weeks=1)),
    "3": ("last month", timedelta(days=30)),
    "4": ("last year", timedelta(days=365)),
}


def export_assignment_groups(c, course_id):
    groups = c.get(f"courses/{course_id}/assignment_groups", params={"per_page": 100})
    groups.sort(key=lambda g: (g.get("position") or 0))
    out = []
    for g in groups:
        item = {"name": g["name"]}
        if g.get("position") is not None:
            item["position"] = g["position"]
        if g.get("group_weight"):
            item["group_weight"] = g["group_weight"]
        out.append(item)
    return {"assignment_groups": out}


def export_assignments(c, course_id):
    assignments = c.get(
        f"courses/{course_id}/assignments", params={"per_page": 100, "include[]": "checkpoints"}
    )
    assignments.sort(key=lambda a: (a.get("position") or 0))

    discussions_by_id = {d["id"]: d for d in c.get(f"courses/{course_id}/discussion_topics", params={"per_page": 100})}
    group_names_by_id = {g["id"]: g["name"] for g in c.get(f"courses/{course_id}/assignment_groups", params={"per_page": 100})}
    category_names_by_id = {g["id"]: g["name"] for g in c.get(f"courses/{course_id}/group_categories", params={"per_page": 100})}

    out = []
    for a in assignments:
        item = {}
        for field in ASSIGNMENT_FIELDS:
            val = a.get(field)
            if val is None:
                continue
            if field == "description" and val:
                val = LiteralStr(clean_html(val))
            item[field] = val

        if a.get("assignment_group_id") in group_names_by_id:
            item["assignment_group"] = group_names_by_id[a["assignment_group_id"]]
        if a.get("group_category_id") in category_names_by_id:
            item["group_category"] = category_names_by_id[a["group_category_id"]]
        if (a.get("rubric_settings") or {}).get("title"):
            item["rubric"] = a["rubric_settings"]["title"]

        # Checkpointed discussions (two required submissions, e.g. reply_to_topic +
        # reply_to_entry) have a null due_at on the parent; the real dates/points live
        # on sub-assignments (checkpoints), updated via `canvas assignments apply`
        # (Canvas's REST API can't touch these at all — it goes through GraphQL).
        if a.get("has_sub_assignments") and a.get("checkpoints"):
            topic = discussions_by_id.get(a.get("discussion_topic", {}).get("id"), {})
            replies_required = topic.get("reply_to_entry_required_count") or 1
            item["checkpoints"] = []
            for cp in a["checkpoints"]:
                entry = {
                    "tag": cp.get("tag"),
                    "points_possible": cp.get("points_possible"),
                    "due_at": cp.get("due_at"),
                }
                if cp.get("tag") == "reply_to_entry":
                    entry["replies_required"] = replies_required
                item["checkpoints"].append(entry)

        out.append(item)
    return {"assignments": out}


def export_pages(c, course_id, verbose=False):
    pages = c.get(f"courses/{course_id}/pages", params={"per_page": 100})
    out = []
    with Progress(len(pages), "pages", verbose=verbose) as progress:
        for p in pages:
            full = c.get(f"courses/{course_id}/pages/{p['url']}")
            item = {"title": full["title"]}
            if full.get("body"):
                item["body"] = LiteralStr(clean_html(full["body"]))
            if full.get("front_page"):
                item["front_page"] = True
            item["published"] = full.get("published", True)
            if full.get("editing_roles") and full["editing_roles"] != "teachers":
                item["editing_roles"] = full["editing_roles"]
            out.append(item)
            progress.step(full["title"])
            if verbose:
                print(f"  fetched: {full['title']}")
    return {"pages": out}


def export_announcements(c, course_id):
    anns = c.get(
        f"courses/{course_id}/discussion_topics", params={"only_announcements": True, "per_page": 100}
    )
    anns.sort(key=lambda a: a.get("posted_at") or a.get("created_at") or "")
    out = []
    for a in anns:
        item = {"title": a["title"]}
        if a.get("message"):
            item["message"] = LiteralStr(clean_html(a["message"]))
        item["published"] = a.get("published", True)
        if a.get("delayed_post_at"):
            item["delayed_post_at"] = a["delayed_post_at"]
        if a.get("lock_at"):
            item["lock_at"] = a["lock_at"]
        out.append(item)
    return {"announcements": out}


def export_modules(c, course_id):
    modules = c.get(f"courses/{course_id}/modules", params={"include[]": "items", "per_page": 100})
    modules.sort(key=lambda m: (m.get("position") or 0))

    pages_by_url = {p["url"]: p for p in c.get(f"courses/{course_id}/pages", params={"per_page": 100})}
    names_by_id = {m["id"]: m["name"] for m in modules}

    out = []
    for m in modules:
        mod_entry = {"name": m["name"], "published": m.get("published", True)}
        if m.get("unlock_at"):
            mod_entry["unlock_at"] = m["unlock_at"]
        if m.get("require_sequential_progress"):
            mod_entry["require_sequential_progress"] = True
        if m.get("prerequisite_module_ids"):
            mod_entry["prerequisites"] = [names_by_id[pid] for pid in m["prerequisite_module_ids"] if pid in names_by_id]

        # `include[]=items` on the modules LIST endpoint silently omits items
        # for modules above some internal item-count threshold — confirmed
        # live against a 223-item module, which came back with
        # `items_count: 223` but no `items` key at all, not even a partial
        # list. Trusting that silently-empty list here would be actively
        # dangerous: `modules apply` deletes any item not listed under its
        # module, so an under-counted export could wipe out real content the
        # next time it's applied. Always cross-check against items_count and
        # re-fetch via the module's own items endpoint whenever they
        # disagree, instead of trusting the list include blindly.
        raw_items = m.get("items", [])
        if m.get("items_count") is not None and len(raw_items) != m["items_count"]:
            raw_items = c.get(f"courses/{course_id}/modules/{m['id']}/items", params={"per_page": 100})

        mod_entry["items"] = []
        for it in raw_items:
            entry = {"type": it["type"], "title": it["title"]}
            if it.get("indent"):
                entry["indent"] = it["indent"]

            if it["type"] == "ExternalUrl":
                entry["url"] = it.get("external_url")
            elif it["type"] == "ExternalTool":
                entry["url"] = it.get("external_url")
                if it.get("new_tab") is False:
                    entry["new_tab"] = False
            elif it["type"] == "Page":
                page = pages_by_url.get(it.get("page_url"))
                entry["page_url"] = page["url"] if page else it.get("page_url")
            elif it["type"] == "File":
                # Use the real id directly rather than relying on title-matching at
                # apply-time — a module item's title is just a display label and can
                # differ from the file's actual display_name (confirmed live: an item
                # titled "Errata" pointed at a file actually named
                # "1337405876_605653.docx" — title-matching would never have found it).
                entry["content_id"] = it.get("content_id")
            elif it["type"] == "SubHeader":
                pass
            # Assignment / Quiz / Discussion: matched by title at apply-time, no extra fields needed

            if not it.get("published", True):
                entry["published"] = False

            mod_entry["items"].append(entry)
        out.append(mod_entry)
    return {"modules": out}


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


def _ext(fmt):
    return "json" if fmt == "json" else "yaml"


def export_one_course(c, course_id, out_parent, verbose=False, formats=("yaml",), policy=None):
    if policy is None:
        policy = OverwritePolicy()
    course_code = c.get(f"courses/{course_id}").get("course_code")
    out = os.path.join(out_parent, _course_folder_name(course_id, course_code))
    os.makedirs(out, exist_ok=True)
    print(f"exporting course {course_id} to {out}/")

    rubric_files = export_rubrics_csv(c, course_id, verbose=verbose)
    rubrics_dir = os.path.join(out, "rubrics")
    os.makedirs(rubrics_dir, exist_ok=True)
    written = 0
    for filename, csv_text in rubric_files:
        if write_text_with_confirmation(os.path.join(rubrics_dir, filename), csv_text, newline="", policy=policy):
            written += 1
    print(f"wrote {written} rubrics -> {rubrics_dir}/")

    assignment_groups = export_assignment_groups(c, course_id)
    for fmt in formats:
        path = os.path.join(out, f"assignment_groups.{_ext(fmt)}")
        if write_with_confirmation(
            assignment_groups,
            path,
            header_comment=f"# Exported from course {course_id} — schema matches `canvas assignment_groups apply`\n",
            policy=policy,
        ):
            print(f"wrote {len(assignment_groups['assignment_groups'])} assignment groups -> {path}")

    assignments = export_assignments(c, course_id)
    for fmt in formats:
        path = os.path.join(out, f"assignments.{_ext(fmt)}")
        if write_with_confirmation(
            assignments,
            path,
            header_comment=f"# Exported from course {course_id} — schema matches `canvas assignments apply`\n",
            policy=policy,
        ):
            print(f"wrote {len(assignments['assignments'])} assignments -> {path}")

    pages = export_pages(c, course_id, verbose=verbose)
    for fmt in formats:
        path = os.path.join(out, f"pages.{_ext(fmt)}")
        if write_with_confirmation(
            pages, path, header_comment=f"# Exported from course {course_id} — schema matches `canvas pages apply`\n", policy=policy
        ):
            print(f"wrote {len(pages['pages'])} pages -> {path}")

    announcements = export_announcements(c, course_id)
    for fmt in formats:
        path = os.path.join(out, f"announcements.{_ext(fmt)}")
        if write_with_confirmation(
            announcements,
            path,
            header_comment=f"# Exported from course {course_id} — schema matches `canvas announcements apply`\n",
            policy=policy,
        ):
            print(f"wrote {len(announcements['announcements'])} announcements -> {path}")

    modules = export_modules(c, course_id)
    total_items = sum(len(m["items"]) for m in modules["modules"])
    for fmt in formats:
        path = os.path.join(out, f"modules.{_ext(fmt)}")
        if write_with_confirmation(
            modules,
            path,
            header_comment=(
                f"# Exported from course {course_id} — schema matches `canvas modules apply`\n"
                "# `modules apply` treats this file as the exact, complete set of modules and\n"
                "# items — a module or item missing from this file gets DELETED from the course,\n"
                "# not just left alone. Always --dry-run before applying an edited copy of this file.\n"
            ),
            policy=policy,
        ):
            print(f"wrote {len(modules['modules'])} modules / {total_items} items -> {path}")


def main(argv=None):
    p = argparse.ArgumentParser(description="Export one or more Canvas courses to assignments.yaml + modules.yaml")
    course_group = p.add_mutually_exclusive_group(required=True)
    course_group.add_argument("--course", nargs="+", help="One or more Canvas course IDs to export")
    course_group.add_argument(
        "--all", action="store_true", help="Export every course you teach (an initial full sync)"
    )
    p.add_argument(
        "--match",
        help="Only with --all: case-insensitive substring to match against each course's code or "
        "name (e.g. '26/FA' to export just one term — not limited to that format, matches "
        "whatever text your school's course codes/names actually contain)",
    )
    p.add_argument(
        "--out",
        default=_DEFAULT_OUT,
        help="Parent directory (default: this project's own exports/ folder, regardless of your "
        "current directory) — each course's own subfolder, named course_<id>_<course code>, is "
        "created underneath it. An explicit --out is resolved relative to wherever you actually "
        "are, same as --file.",
    )
    p.add_argument("--verbose", action="store_true", help="Print each item as it's fetched instead of a progress bar")
    p.add_argument(
        "--format",
        choices=["yaml", "json"],
        default=None,
        help="Output format for the written files. Default: write both yaml and json for every "
        "resource. Give this to write only one. JSON files work with every `apply` command too, "
        "just without comments and without multi-line-friendly HTML.",
    )
    args = p.parse_args(argv)

    if args.match and not args.all:
        p.error("--match only applies with --all")

    c = CanvasClient()

    if args.all:
        courses = c.get("courses", params={"per_page": 100, "enrollment_type": "teacher", "state[]": "available"})
        if args.match:
            needle = args.match.lower()
            matched = [co for co in courses if needle in (co.get("course_code") or "").lower() or needle in (co.get("name") or "").lower()]
            print(f"matched {len(matched)}/{len(courses)} courses against {args.match!r}")
            courses = matched
        course_ids = [str(co["id"]) for co in courses]
        if not course_ids:
            print("No courses found.")
            return
    else:
        course_ids = args.course

    formats = [args.format] if args.format else ["yaml", "json"]

    # One policy shared across every course in this run — an "apply to
    # everything else" answer to the first overwrite prompt then covers the
    # rest of a multi-course --all/--course A B C run too, not just the
    # rest of one course's own files.
    policy = OverwritePolicy()

    failed = []
    for i, course_id in enumerate(course_ids):
        if len(course_ids) > 1:
            print(f"\n=== [{i + 1}/{len(course_ids)}] course {course_id} ===")
        try:
            export_one_course(c, course_id, args.out, verbose=args.verbose, formats=formats, policy=policy)
        except CanvasError as e:
            print(f"course {course_id}: ERROR — {e}")
            failed.append(course_id)

    if len(course_ids) > 1:
        ok = len(course_ids) - len(failed)
        print(f"\ndone: {ok}/{len(course_ids)} courses exported")
        if failed:
            print(f"failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
