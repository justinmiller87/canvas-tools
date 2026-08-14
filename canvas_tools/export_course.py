#!/usr/bin/env python3
"""Reverse-export a live Canvas course into the assignments.yaml / modules.yaml
schema used by `canvas assignments apply` / `canvas modules apply`, so a real
built course can be inspected (or reused as a template) locally.
"""
import argparse
import os

import yaml

from canvas_tools.client import CanvasClient
from canvas_tools.html_clean import clean_html
from canvas_tools.progress import Progress
from canvas_tools.rubrics import export_rubrics_csv

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
        mod_entry["items"] = []
        for it in m.get("items", []):
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


def main():
    p = argparse.ArgumentParser(description="Export a Canvas course to assignments.yaml + modules.yaml")
    p.add_argument("--course", required=True, help="Canvas course ID to export")
    p.add_argument(
        "--out",
        default="exports",
        help="Parent directory (default: exports) — the course's own subfolder, named "
        "course_<id>_<course code>, is created underneath it",
    )
    p.add_argument("--verbose", action="store_true", help="Print each item as it's fetched instead of a progress bar")
    args = p.parse_args()

    c = CanvasClient()
    course_code = c.get(f"courses/{args.course}").get("course_code")
    args.out = os.path.join(args.out, _course_folder_name(args.course, course_code))
    os.makedirs(args.out, exist_ok=True)
    print(f"exporting to {args.out}/")

    rubric_files = export_rubrics_csv(c, args.course, verbose=args.verbose)
    rubrics_dir = os.path.join(args.out, "rubrics")
    os.makedirs(rubrics_dir, exist_ok=True)
    for filename, csv_text in rubric_files:
        with open(os.path.join(rubrics_dir, filename), "w", newline="") as f:
            f.write(csv_text)
    print(f"wrote {len(rubric_files)} rubrics -> {rubrics_dir}/")

    assignments = export_assignments(c, args.course)
    with open(os.path.join(args.out, "assignments.yaml"), "w") as f:
        f.write(f"# Exported from course {args.course} — schema matches `canvas assignments apply`\n")
        yaml.dump(assignments, f, sort_keys=False, allow_unicode=True, width=100)
    print(f"wrote {len(assignments['assignments'])} assignments -> {args.out}/assignments.yaml")

    pages = export_pages(c, args.course, verbose=args.verbose)
    with open(os.path.join(args.out, "pages.yaml"), "w") as f:
        f.write(f"# Exported from course {args.course} — schema matches `canvas pages apply`\n")
        yaml.dump(pages, f, sort_keys=False, allow_unicode=True, width=100)
    print(f"wrote {len(pages['pages'])} pages -> {args.out}/pages.yaml")

    announcements = export_announcements(c, args.course)
    with open(os.path.join(args.out, "announcements.yaml"), "w") as f:
        f.write(f"# Exported from course {args.course} — schema matches `canvas announcements apply`\n")
        yaml.dump(announcements, f, sort_keys=False, allow_unicode=True, width=100)
    print(f"wrote {len(announcements['announcements'])} announcements -> {args.out}/announcements.yaml")

    modules = export_modules(c, args.course)
    with open(os.path.join(args.out, "modules.yaml"), "w") as f:
        f.write(f"# Exported from course {args.course} — schema matches `canvas modules apply`\n")
        f.write(
            "# `modules apply` treats this file as the exact, complete set of modules and\n"
            "# items — a module or item missing from this file gets DELETED from the course,\n"
            "# not just left alone. Always --dry-run before applying an edited copy of this file.\n"
        )
        yaml.dump(modules, f, sort_keys=False, allow_unicode=True, width=100)
    total_items = sum(len(m["items"]) for m in modules["modules"])
    print(f"wrote {len(modules['modules'])} modules / {total_items} items -> {args.out}/modules.yaml")


if __name__ == "__main__":
    main()
