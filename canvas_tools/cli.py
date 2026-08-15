#!/usr/bin/env python3
import argparse
import csv
import io
import os
import sys
import time

import yaml

from canvas_tools.client import CanvasClient, CanvasError
from canvas_tools.html_clean import clean_html
from canvas_tools.rubrics import export_rubrics_csv, import_rubrics_csv, update_rubric_in_place, _parse_rubrics_csv
from canvas_tools.export_course import export_assignments, export_pages, export_announcements, export_modules
from canvas_tools.progress import Progress


def _write_single_yaml_export(args, c, export_fn, key, apply_cmd_name, **kwargs):
    data = export_fn(c, args.course, **kwargs)
    with open(args.out, "w") as f:
        f.write(f"# Exported from course {args.course} — schema matches `canvas {apply_cmd_name} apply`\n")
        yaml.dump(data, f, sort_keys=False, allow_unicode=True, width=100)
    print(f"wrote {len(data[key])} {key} -> {args.out}")


def cmd_assignments_export(args, c):
    _write_single_yaml_export(args, c, export_assignments, "assignments", "assignments")


def cmd_pages_export(args, c):
    _write_single_yaml_export(args, c, export_pages, "pages", "pages", verbose=args.verbose)


def cmd_announcements_export(args, c):
    _write_single_yaml_export(args, c, export_announcements, "announcements", "announcements")


def cmd_modules_export(args, c):
    data = export_modules(c, args.course)
    with open(args.out, "w") as f:
        f.write(f"# Exported from course {args.course} — schema matches `canvas modules apply`\n")
        f.write(
            "# `modules apply` treats this file as the exact, complete set of modules and\n"
            "# items — a module or item missing from this file gets DELETED from the course,\n"
            "# not just left alone. Always --dry-run before applying an edited copy of this file.\n"
        )
        yaml.dump(data, f, sort_keys=False, allow_unicode=True, width=100)
    total_items = sum(len(m["items"]) for m in data["modules"])
    print(f"wrote {len(data['modules'])} modules / {total_items} items -> {args.out}")


def cmd_courses_list(args, c):
    params = {"per_page": 100}
    if args.term:
        params["enrollment_type"] = "teacher"
    courses = c.get("courses", params={**params, "enrollment_type": "teacher", "state[]": args.state})
    courses.sort(key=lambda x: x.get("name") or "")
    for co in courses:
        print(f"{co['id']}\t{co.get('course_code', ''):20s}\t{co.get('name')}")


def _find_assignment_by_name(assignments, name):
    for a in assignments:
        if a["name"].strip().lower() == name.strip().lower():
            return a
    return None


CHECKPOINT_LABELS = ("reply_to_topic", "reply_to_entry")

_CHECKPOINTS_UPDATE_MUTATION = """
mutation UpdateCheckpoints($input: UpdateDiscussionTopicInput!) {
  updateDiscussionTopic(input: $input) {
    discussionTopic { _id }
    errors { attribute message }
  }
}
"""


def _apply_discussion_checkpoints(c, course_id, assignment_id, checkpoints_spec):
    """Update due dates/points on an *existing* checkpointed discussion.

    Canvas's REST API has no endpoint for this at all — checkpoint due dates
    only exist via the GraphQL mutation the redesigned Discussions UI uses
    internally. That mutation requires both checkpoints (reply_to_topic and
    reply_to_entry) in every call, so any field not given in `checkpoints_spec`
    is filled in from the checkpoint's current live value.
    """
    current = c.get(f"courses/{course_id}/assignments/{assignment_id}", params={"include[]": "checkpoints"})
    if not current.get("has_sub_assignments"):
        raise CanvasError(
            f"assignment {assignment_id} has no checkpoints yet — this tool only updates dates/points on "
            "checkpoints that already exist (set them up once in the Canvas UI, or via course copy)"
        )
    discussion_topic_id = current["discussion_topic"]["id"]
    current_by_tag = {cp["tag"]: cp for cp in current.get("checkpoints", [])}
    current_topic = c.get(f"courses/{course_id}/discussion_topics/{discussion_topic_id}")
    current_replies_required = current_topic.get("reply_to_entry_required_count") or 1

    spec_by_tag = {cp["tag"]: cp for cp in checkpoints_spec}
    for tag in spec_by_tag:
        if tag not in CHECKPOINT_LABELS:
            raise CanvasError(f"unknown checkpoint tag {tag!r} (expected one of {CHECKPOINT_LABELS})")

    gql_checkpoints = []
    for tag in CHECKPOINT_LABELS:
        spec = spec_by_tag.get(tag, {})
        cur = current_by_tag.get(tag, {})
        points = spec.get("points_possible")
        if points is None:
            points = cur.get("points_possible")
        due_at = spec.get("due_at")
        if due_at is None:
            due_at = cur.get("due_at")
        if points is None or due_at is None:
            raise CanvasError(f"checkpoint {tag!r}: no points_possible/due_at given and none found on the live checkpoint")
        entry = {
            "checkpointLabel": tag,
            "pointsPossible": points,
            "dates": [{"type": "everyone", "dueAt": due_at}],
        }
        if tag == "reply_to_entry":
            replies_required = spec.get("replies_required")
            entry["repliesRequired"] = replies_required if replies_required is not None else current_replies_required
        gql_checkpoints.append(entry)

    result = c.graphql(
        _CHECKPOINTS_UPDATE_MUTATION,
        {
            "input": {
                "discussionTopicId": str(discussion_topic_id),
                "assignment": {"forCheckpoints": True},
                "checkpoints": gql_checkpoints,
            }
        },
    )
    errors = result.get("updateDiscussionTopic", {}).get("errors")
    if errors:
        raise CanvasError(f"checkpoint update rejected: {errors}")


_CREATE_CHECKPOINTED_DISCUSSION_MUTATION = """
mutation CreateCheckpointedDiscussion($input: CreateDiscussionTopicInput!) {
  createDiscussionTopic(input: $input) {
    discussionTopic { _id assignment { _id } }
    errors { attribute message }
  }
}
"""


def _create_checkpointed_discussion(c, course_id, name, item, checkpoints_spec):
    """Create a brand-new checkpointed discussion (two required sub-assignments,
    reply_to_topic + reply_to_entry) from scratch. Canvas's REST API can't do
    this at all — same as it can't update an existing checkpointed discussion's
    dates (see _apply_discussion_checkpoints) — but unlike that gap, this one
    isn't a hard limit: schema introspection against a live instance confirmed
    `createDiscussionTopic`'s input type also accepts a `checkpoints` argument,
    mirroring `updateDiscussionTopic`, so it goes through the same GraphQL
    mutation family the redesigned Discussions UI uses internally."""
    spec_by_tag = {cp["tag"]: cp for cp in checkpoints_spec}
    for tag in spec_by_tag:
        if tag not in CHECKPOINT_LABELS:
            raise CanvasError(f"unknown checkpoint tag {tag!r} (expected one of {CHECKPOINT_LABELS})")
    missing = [tag for tag in CHECKPOINT_LABELS if tag not in spec_by_tag]
    if missing:
        raise CanvasError(f"creating a new checkpointed discussion needs both checkpoints given at once — missing {missing}")

    gql_checkpoints = []
    for tag in CHECKPOINT_LABELS:
        spec = spec_by_tag[tag]
        points = spec.get("points_possible")
        due_at = spec.get("due_at")
        if points is None or due_at is None:
            raise CanvasError(f"checkpoint {tag!r}: points_possible and due_at are both required to create a new checkpointed discussion")
        entry = {
            "checkpointLabel": tag,
            "pointsPossible": points,
            "dates": [{"type": "everyone", "dueAt": due_at}],
        }
        if tag == "reply_to_entry":
            entry["repliesRequired"] = spec.get("replies_required") or 1
        gql_checkpoints.append(entry)

    assignment_input = {"courseId": str(course_id), "name": name, "forCheckpoints": True}
    if item.get("assignment_group_id") is not None:
        assignment_input["assignmentGroupId"] = str(item["assignment_group_id"])
    if item.get("unlock_at"):
        assignment_input["unlockAt"] = item["unlock_at"]
    if item.get("lock_at"):
        assignment_input["lockAt"] = item["lock_at"]

    input_ = {
        "contextId": str(course_id),
        "contextType": "Course",
        "title": name,
        "discussionType": "threaded",
        "assignment": assignment_input,
        "checkpoints": gql_checkpoints,
    }
    if item.get("description"):
        input_["message"] = item["description"]
    if item.get("published") is not None:
        input_["published"] = item["published"]

    result = c.graphql(_CREATE_CHECKPOINTED_DISCUSSION_MUTATION, {"input": input_})
    errors = result.get("createDiscussionTopic", {}).get("errors")
    if errors:
        raise CanvasError(f"checkpointed discussion creation rejected: {errors}")
    return int(result["createDiscussionTopic"]["discussionTopic"]["assignment"]["_id"])


def _apply_rubric_association(c, course_id, assignment_id, rubric_title, rubrics, use_for_grading):
    matches = [r for r in rubrics if r["title"].strip().lower() == rubric_title.strip().lower()]
    if not matches:
        raise CanvasError(f"rubric {rubric_title!r} not found in course {course_id} (create it first)")
    # Multiple rubrics can share a title — e.g. `rubrics import` re-importing
    # an edited replacement under the same name as an existing rubric, which
    # doesn't update it in place (see rubrics.py). Canvas ids increase
    # monotonically, so the highest id is deterministically the most
    # recently created one — pick that, not whatever order the API returns.
    rubric = max(matches, key=lambda r: r["id"])
    if len(matches) > 1:
        print(f"  note: {len(matches)} rubrics named {rubric_title!r} exist — using the newest (id={rubric['id']})")
    c.post(
        f"courses/{course_id}/rubric_associations",
        json={
            "rubric_association": {
                "rubric_id": rubric["id"],
                "association_id": assignment_id,
                "association_type": "Assignment",
                "purpose": "grading",
                "use_for_grading": bool(use_for_grading),
            }
        },
    )


_ASSIGNMENT_RUBRIC_ASSOCIATION_QUERY = """
query($assignmentId: ID!) {
  assignment(id: $assignmentId) {
    rubricAssociation { _id }
  }
}
"""


def _remove_rubric_association(c, course_id, assignment_id):
    """Detach whatever rubric is on this assignment, freeing it up to attach a
    fresh one (e.g. after re-importing an edited rubric via `rubrics import`,
    since Canvas won't let you edit a rubric in place once it's used in
    multiple places). CAUTION: if this is the rubric's last remaining usage
    anywhere, Canvas can delete the rubric outright as a side effect of
    detaching it — a course-level "bookmark" association was assumed to
    reliably prevent that, but that assumption turned out to be wrong for at
    least one real rubric (see rubrics.py / README). Use `rubrics update`
    instead of this + a fresh `rubrics import` when the goal is editing an
    existing rubric's content — it's designed around this exact failure mode."""
    result = c.graphql(_ASSIGNMENT_RUBRIC_ASSOCIATION_QUERY, {"assignmentId": str(assignment_id)})
    association = (result.get("assignment") or {}).get("rubricAssociation")
    if not association:
        return False
    c.delete(f"courses/{course_id}/rubric_associations/{association['_id']}")
    return True


def cmd_assignments_apply(args, c):
    with open(args.file) as f:
        spec = yaml.safe_load(f)

    items = spec.get("assignments", spec if isinstance(spec, list) else [])
    if not items:
        print("No assignments found in file.")
        return

    existing = c.get(f"courses/{args.course}/assignments", params={"per_page": 100})
    assignment_groups = c.get(f"courses/{args.course}/assignment_groups", params={"per_page": 100})
    group_categories = c.get(f"courses/{args.course}/group_categories", params={"per_page": 100})
    rubrics = c.get(f"courses/{args.course}/rubrics", params={"per_page": 100})

    progress = Progress(len(items), "assignments", verbose=args.verbose)
    for item in items:
        progress.step(item.get("name"))
        name = item["name"]
        item = dict(item)
        checkpoints_spec = item.pop("checkpoints", None)
        rubric_title = item.pop("rubric", None)
        remove_rubric = item.pop("remove_rubric", False)

        if "assignment_group" in item:
            group_name = item.pop("assignment_group")
            if group_name is not None:
                group = next((g for g in assignment_groups if g["name"].strip().lower() == group_name.strip().lower()), None)
                if not group:
                    raise CanvasError(f"assignment group {group_name!r} not found in course {args.course}")
                item["assignment_group_id"] = group["id"]
        if "group_category" in item:
            cat_name = item.pop("group_category")
            if cat_name is not None:
                cat = next((g for g in group_categories if g["name"].strip().lower() == cat_name.strip().lower()), None)
                if not cat:
                    raise CanvasError(f"group set {cat_name!r} not found in course {args.course}")
                item["group_category_id"] = cat["id"]

        if item.get("description"):
            item["description"] = clean_html(item["description"])
        payload = {"assignment": {k: v for k, v in item.items() if v is not None}}
        match = _find_assignment_by_name(existing, name)
        if match:
            if args.dry_run:
                print(f"[dry-run] would UPDATE assignment {match['id']!r}: {name}")
                if checkpoints_spec:
                    print(f"[dry-run]   would UPDATE checkpoints: {[cp['tag'] for cp in checkpoints_spec]}")
                if remove_rubric:
                    print(f"[dry-run]   would REMOVE current rubric (if any)")
                if rubric_title:
                    print(f"[dry-run]   would ATTACH rubric: {rubric_title}")
                continue
            if payload["assignment"]:
                c.put(f"courses/{args.course}/assignments/{match['id']}", json=payload)
            if args.verbose:
                print(f"updated: {name} (id={match['id']})")
            if checkpoints_spec:
                _apply_discussion_checkpoints(c, args.course, match["id"], checkpoints_spec)
                if args.verbose:
                    print(f"  checkpoints updated: {[cp['tag'] for cp in checkpoints_spec]}")
            if remove_rubric:
                removed = _remove_rubric_association(c, args.course, match["id"])
                if args.verbose:
                    print(f"  rubric removed: {name}" if removed else "  no rubric to remove")
            if rubric_title:
                _apply_rubric_association(c, args.course, match["id"], rubric_title, rubrics, item.get("use_rubric_for_grading", False))
                if args.verbose:
                    print(f"  rubric attached: {rubric_title}")
        else:
            if args.dry_run:
                print(f"[dry-run] would CREATE assignment: {name}")
                if checkpoints_spec:
                    print(f"[dry-run]   would CREATE as a checkpointed discussion: {[cp['tag'] for cp in checkpoints_spec]}")
                if rubric_title:
                    print(f"[dry-run]   would ATTACH rubric: {rubric_title}")
                continue
            if checkpoints_spec:
                created_id = _create_checkpointed_discussion(c, args.course, name, item, checkpoints_spec)
                if args.verbose:
                    print(f"created checkpointed discussion: {name} (id={created_id})")
            else:
                created = c.post(f"courses/{args.course}/assignments", json=payload)
                created_id = created["id"]
                if args.verbose:
                    print(f"created: {name} (id={created_id})")
            if rubric_title:
                _apply_rubric_association(c, args.course, created_id, rubric_title, rubrics, item.get("use_rubric_for_grading", False))
                if args.verbose:
                    print(f"  rubric attached: {rubric_title}")
    progress.done()


def _find_page_by_title(pages, title):
    for p in pages:
        if p["title"].strip().lower() == title.strip().lower():
            return p
    return None


PAGE_FIELDS = ("title", "body", "published", "front_page", "editing_roles", "notify_of_update")


def cmd_pages_apply(args, c):
    with open(args.file) as f:
        spec = yaml.safe_load(f)

    items = spec.get("pages", spec if isinstance(spec, list) else [])
    if not items:
        print("No pages found in file.")
        return

    existing = c.get(f"courses/{args.course}/pages", params={"per_page": 100})

    progress = Progress(len(items), "pages", verbose=args.verbose)
    for item in items:
        progress.step(item.get("title"))
        title = item["title"]
        item = dict(item)
        # Canvas's response field is `todo_date`, but the write param is
        # `student_todo_at` (confirmed against source — `wiki_page[todo_date]`
        # is silently ignored, it's not in the permitted params list at all).
        # Setting it requires the write; clearing an existing one isn't
        # supported here (consistent with the blank-means-omitted rule
        # elsewhere in this tool — there's no way to distinguish "clear it"
        # from "don't touch it" without breaking that rule).
        todo_date = item.pop("todo_date", None)
        body = {k: v for k, v in item.items() if k in PAGE_FIELDS and v is not None}
        if todo_date is not None:
            body["student_todo_at"] = todo_date
            body["student_planner_checkbox"] = True
        if body.get("body"):
            body["body"] = clean_html(body["body"])
        payload = {"wiki_page": body}
        match = _find_page_by_title(existing, title)
        if match:
            if args.dry_run:
                print(f"[dry-run] would UPDATE page {match['url']!r}: {title}")
                continue
            c.put(f"courses/{args.course}/pages/{match['url']}", json=payload)
            if args.verbose:
                print(f"updated: {title} (url={match['url']})")
        else:
            if args.dry_run:
                print(f"[dry-run] would CREATE page: {title}")
                continue
            created = c.post(f"courses/{args.course}/pages", json=payload)
            if args.verbose:
                print(f"created: {title} (url={created['url']})")
    progress.done()


def _find_announcement_by_title(announcements, title):
    for a in announcements:
        if a["title"].strip().lower() == title.strip().lower():
            return a
    return None


ANNOUNCEMENT_FIELDS = (
    "title",
    "message",
    "published",
    "delayed_post_at",
    "lock_at",
    "require_initial_post",
    "pinned",
    "discussion_type",
    "allow_rating",
    "only_graders_can_rate",
    "sort_order",
    "sort_order_locked",
    "podcast_enabled",
    "podcast_has_student_posts",
    "todo_date",
    "position_after",
    "specific_sections",
    "expanded",
    "expanded_locked",
    "sort_by_rating",
)


def cmd_announcements_apply(args, c):
    with open(args.file) as f:
        spec = yaml.safe_load(f)

    items = spec.get("announcements", spec if isinstance(spec, list) else [])
    if not items:
        print("No announcements found in file.")
        return

    existing = c.get(f"courses/{args.course}/discussion_topics", params={"only_announcements": True, "per_page": 100})

    progress = Progress(len(items), "announcements", verbose=args.verbose)
    for item in items:
        progress.step(item.get("title"))
        title = item["title"]
        body = {k: v for k, v in item.items() if k in ANNOUNCEMENT_FIELDS and v is not None}
        body["is_announcement"] = True
        if body.get("message"):
            body["message"] = clean_html(body["message"])
        match = _find_announcement_by_title(existing, title)
        if match:
            if args.dry_run:
                print(f"[dry-run] would UPDATE announcement {match['id']}: {title}")
                continue
            c.put(f"courses/{args.course}/discussion_topics/{match['id']}", json=body)
            if args.verbose:
                print(f"updated: {title} (id={match['id']})")
        else:
            if args.dry_run:
                print(f"[dry-run] would CREATE announcement: {title}")
                continue
            created = c.post(f"courses/{args.course}/discussion_topics", json=body)
            if args.verbose:
                print(f"created: {title} (id={created['id']})")
    progress.done()


def _module_item_payload(
    item, existing_assignments, existing_pages, existing_quizzes, existing_discussions, existing_files
):
    title = item.get("title")
    itype = item["type"]
    indent = item.get("indent")
    body = {"module_item": {"title": title, "type": itype, "indent": indent if indent is not None else 0}}

    if itype == "ExternalUrl":
        body["module_item"]["external_url"] = item["url"]
    elif itype == "SubHeader":
        pass
    elif itype == "Page":
        page_url = item.get("page_url")
        if not page_url:
            match = next((p for p in existing_pages if p["title"].strip().lower() == title.strip().lower()), None)
            if not match:
                raise CanvasError(f"Page item {title!r}: no page_url given and no existing page matches by title")
            page_url = match["url"]
        body["module_item"]["page_url"] = page_url
    elif itype == "Assignment":
        match = next((a for a in existing_assignments if a["name"].strip().lower() == title.strip().lower()), None)
        if not match:
            raise CanvasError(f"Assignment item {title!r}: no existing assignment matches by title")
        body["module_item"]["content_id"] = match["id"]
    elif itype == "Quiz":
        match = next((q for q in existing_quizzes if q["title"].strip().lower() == title.strip().lower()), None)
        if not match:
            raise CanvasError(f"Quiz item {title!r}: no existing quiz matches by title")
        body["module_item"]["content_id"] = match["id"]
    elif itype == "Discussion":
        match = next((d for d in existing_discussions if d["title"].strip().lower() == title.strip().lower()), None)
        if not match:
            raise CanvasError(f"Discussion item {title!r}: no existing discussion matches by title")
        body["module_item"]["content_id"] = match["id"]
    elif itype == "File":
        content_id = item.get("content_id")
        if not content_id:
            match = next(
                (f for f in existing_files if f["display_name"].strip().lower() == title.strip().lower()), None
            )
            if not match:
                raise CanvasError(f"File item {title!r}: no content_id given and no existing file matches by title")
            content_id = match["id"]
        body["module_item"]["content_id"] = content_id
    elif itype == "ExternalTool":
        if not item.get("url"):
            raise CanvasError(f"ExternalTool item {title!r}: requires a `url` (the LTI launch URL)")
        body["module_item"]["external_url"] = item["url"]
        new_tab = item.get("new_tab")
        body["module_item"]["new_tab"] = new_tab if new_tab is not None else True
        if item.get("iframe_width") is not None or item.get("iframe_height") is not None:
            body["module_item"]["iframe"] = {}
            if item.get("iframe_width") is not None:
                body["module_item"]["iframe"]["width"] = item["iframe_width"]
            if item.get("iframe_height") is not None:
                body["module_item"]["iframe"]["height"] = item["iframe_height"]
    else:
        raise CanvasError(f"Unsupported module item type: {itype}")

    if item.get("position") is not None:
        body["module_item"]["position"] = item["position"]
    if item.get("completion_requirement"):
        cr = item["completion_requirement"]
        valid_types = ("must_view", "must_contribute", "must_submit", "must_mark_done", "min_score", "min_percentage")
        if cr.get("type") not in valid_types:
            raise CanvasError(f"item {title!r}: completion_requirement.type must be one of {valid_types}")
        if cr["type"] in ("min_score", "min_percentage") and cr.get("min_score") is None:
            raise CanvasError(f"item {title!r}: completion_requirement type {cr['type']!r} requires min_score")
        body["module_item"]["completion_requirement"] = {"type": cr["type"]}
        if cr.get("min_score") is not None:
            body["module_item"]["completion_requirement"]["min_score"] = cr["min_score"]

    return body


def cmd_modules_apply(args, c):
    with open(args.file) as f:
        spec = yaml.safe_load(f)

    modules = spec.get("modules", [])
    if not modules:
        print("No modules found in file.")
        return

    existing_modules = c.get(f"courses/{args.course}/modules", params={"per_page": 100})
    existing_assignments = c.get(f"courses/{args.course}/assignments", params={"per_page": 100})
    existing_pages = c.get(f"courses/{args.course}/pages", params={"per_page": 100})
    existing_quizzes = c.get(f"courses/{args.course}/quizzes", params={"per_page": 100})
    existing_discussions = c.get(f"courses/{args.course}/discussion_topics", params={"per_page": 100})
    existing_files = c.get(f"courses/{args.course}/files", params={"per_page": 100})

    # `modules apply` treats the file as the exact, complete set of modules
    # and (per-module) items — unlike every other `apply` command in this
    # tool, this ONE deletes: a module in Canvas but not in the file is
    # deleted outright, and an item in a kept module but not listed under it
    # is removed from that module. Module items are just pointers into a
    # module, not the underlying content, so removing one only unlinks it —
    # the page/assignment/etc. itself is untouched either way. Deleting a
    # whole module does not delete its former items' underlying content
    # either. Always run --dry-run first; a stale or incomplete file here
    # will delete real course structure, not just leave it alone.
    file_module_names = {mod["name"].strip().lower() for mod in modules}
    with Progress(len(existing_modules), "modules (checking for deletions)", verbose=args.verbose) as progress:
        for m in existing_modules:
            progress.step(m["name"])
            if m["name"].strip().lower() not in file_module_names:
                if args.dry_run:
                    print(f"[dry-run] would DELETE module (not in file): {m['name']} (id={m['id']})")
                else:
                    c.delete(f"courses/{args.course}/modules/{m['id']}")
                    if args.verbose:
                        print(f"deleted module (not in file): {m['name']} (id={m['id']})")

    kept_existing_modules = [m for m in existing_modules if m["name"].strip().lower() in file_module_names]

    # name (lowercased) -> id, seeded with modules kept from the course so
    # `prerequisites:` can reference modules outside this YAML file too —
    # but never a module just deleted above, so a stale prerequisite
    # reference fails loudly instead of pointing at a dead id.
    module_ids = {m["name"].strip().lower(): m["id"] for m in kept_existing_modules}

    # Pass 1: ensure every module in the file exists, so real ids are known
    # before pass 2 resolves prerequisite names into prerequisite_module_ids.
    newly_created = set()
    with Progress(len(modules), "modules (creating)", verbose=args.verbose) as progress:
        for mod in modules:
            mname = mod["name"]
            progress.step(mname)
            match = next((m for m in kept_existing_modules if m["name"].strip().lower() == mname.strip().lower()), None)
            if match:
                if args.verbose:
                    print(f"module exists: {mname} (id={match['id']})")
            elif args.dry_run:
                print(f"[dry-run] would CREATE module: {mname}")
                module_ids[mname.strip().lower()] = None
                newly_created.add(mname.strip().lower())
            else:
                # `published` is NOT accepted by Canvas's module create endpoint at
                # all (confirmed against source and live — it's silently dropped;
                # only the update endpoint takes it), so every newly created module
                # needs an explicit follow-up PUT in pass 2 to actually publish it.
                created = c.post(f"courses/{args.course}/modules", json={"module": {"name": mname}})
                module_ids[mname.strip().lower()] = created["id"]
                newly_created.add(mname.strip().lower())
                if args.verbose:
                    print(f"created module: {mname} (id={created['id']})")

    # Pass 2: unlock_at / require_sequential_progress / prerequisites, now that
    # every module referenced by name (including forward references) has an id.
    with Progress(len(modules), "modules (settings)", verbose=args.verbose) as progress:
        for mod in modules:
            mname = mod["name"]
            progress.step(mname)
            module_id = module_ids.get(mname.strip().lower())
            prereq_names = mod.get("prerequisites") or []
            update_fields = {
                k: mod[k]
                for k in ("unlock_at", "require_sequential_progress", "position", "publish_final_grade")
                if mod.get(k) is not None
            }
            if mod.get("published") is not None:
                update_fields["published"] = mod["published"]
            elif mname.strip().lower() in newly_created:
                # create ignores `published` entirely (see pass 1) — default to
                # published on create the same as everywhere else in this tool,
                # even though the file didn't ask for it explicitly.
                update_fields["published"] = True
            if prereq_names:
                prereq_ids = []
                for pname in prereq_names:
                    pid = module_ids.get(pname.strip().lower())
                    if pid is None and not args.dry_run:
                        raise CanvasError(f"module {mname!r}: prerequisite {pname!r} not found (create it first)")
                    prereq_ids.append(pid)
                update_fields["prerequisite_module_ids"] = prereq_ids

            if not update_fields:
                continue
            if args.dry_run:
                print(f"[dry-run] would SET on {mname!r}: {update_fields}")
                continue
            c.put(f"courses/{args.course}/modules/{module_id}", json={"module": update_fields})
            if args.verbose:
                print(f"  updated module settings: {mname} ({list(update_fields)})")

    with Progress(len(modules), "modules (syncing items)", verbose=args.verbose) as progress:
        for mod in modules:
            mname = mod["name"]
            progress.step(mname)
            module_id = module_ids.get(mname.strip().lower())
            existing_items = c.get(f"courses/{args.course}/modules/{module_id}/items", params={"per_page": 100}) if module_id else []
            file_item_titles = {(item.get("title") or "").strip().lower() for item in mod.get("items", [])}

            for existing_item in existing_items:
                if existing_item.get("title", "").strip().lower() not in file_item_titles:
                    if args.dry_run:
                        print(f"  [dry-run] would REMOVE item (not in file): {existing_item['title']}")
                    else:
                        c.delete(f"courses/{args.course}/modules/{module_id}/items/{existing_item['id']}")
                        if args.verbose:
                            print(f"  removed item (not in file): {existing_item['title']}")

            for item in mod.get("items", []):
                title = item.get("title")
                if any(i.get("title", "").strip().lower() == (title or "").strip().lower() for i in existing_items):
                    if args.verbose:
                        print(f"  item exists, skipping: {title}")
                    continue
                if args.dry_run:
                    print(f"  [dry-run] would ADD item: {title} ({item['type']})")
                    continue
                payload = _module_item_payload(
                    item, existing_assignments, existing_pages, existing_quizzes, existing_discussions, existing_files
                )
                created_item = c.post(f"courses/{args.course}/modules/{module_id}/items", json=payload)
                if args.verbose:
                    print(f"  added item: {title}")
                if item.get("published") is not None:
                    # `published` is not accepted on item create at all (confirmed
                    # against source — only the update endpoint handles it), so it
                    # needs this separate follow-up PUT.
                    c.put(
                        f"courses/{args.course}/modules/{module_id}/items/{created_item['id']}",
                        json={"module_item": {"published": item["published"]}},
                    )


def cmd_copy(args, c):
    body = {
        "migration_type": "course_copy_importer",
        "settings": {"source_course_id": args.source},
    }
    migration = c.post(f"courses/{args.dest}/content_migrations", json=body)
    mig_id = migration["id"]
    print(f"started content migration {mig_id}: course {args.source} -> {args.dest}")

    if not args.wait:
        return

    while True:
        status = c.get(f"courses/{args.dest}/content_migrations/{mig_id}")
        state = status["workflow_state"]
        print(f"  state: {state}")
        if state in ("completed", "failed"):
            break
        time.sleep(args.poll_interval)

    if state == "failed":
        sys.exit(1)


def cmd_rubrics_export(args, c):
    files = export_rubrics_csv(c, args.course, verbose=args.verbose)
    os.makedirs(args.out, exist_ok=True)
    for filename, csv_text in files:
        with open(os.path.join(args.out, filename), "w", newline="") as f:
            f.write(csv_text)
    print(f"wrote {len(files)} rubric(s) -> {args.out}/")


def cmd_rubrics_import(args, c):
    with open(args.file, "rb") as f:
        csv_bytes = f.read()
    if args.dry_run:
        reader = csv.reader(io.StringIO(csv_bytes.decode()))
        next(reader, None)  # header
        names = sorted({row[0] for row in reader if row and row[0].strip()})
        print(f"[dry-run] would import {len(names)} rubric(s): {names}")
        print("note: Canvas's rubric CSV import always CREATES new rubrics — re-importing the same file creates duplicates, it does not update existing ones by title")
        return
    result = import_rubrics_csv(c, args.course, csv_bytes, filename=args.file, wait=not args.no_wait, verbose=args.verbose)
    if args.no_wait:
        print(f"import started: id={result['id']} (not waiting — activation is skipped without --wait, run again without --no-wait or activate manually in the Canvas UI)")
    else:
        print(f"import finished: {result['workflow_state']}")
        activated = result.get("activated_rubrics", [])
        if activated:
            print(f"activated: {activated}")
        else:
            print("note: no matching draft rubrics found to activate (already active, or names didn't match)")


def cmd_rubrics_update(args, c):
    with open(args.file, "rb") as f:
        csv_bytes = f.read()
    rubrics_in_file = _parse_rubrics_csv(csv_bytes)
    if not rubrics_in_file:
        print("No rubrics found in file.")
        return
    if len(rubrics_in_file) > 1:
        raise CanvasError(
            f"{args.file} contains {len(rubrics_in_file)} rubrics ({list(rubrics_in_file)}) — "
            "`rubrics update` handles one rubric per file, see `rubrics export`"
        )

    for title, criteria_list in rubrics_in_file.items():
        if args.dry_run:
            result = update_rubric_in_place(c, args.course, title, criteria_list, dry_run=True)
            names = [a["title"] for a in result["assignments"]]
            print(f"[dry-run] would detach from {len(names)} assignment(s), update criteria, reattach: {names}")
            return
        result = update_rubric_in_place(c, args.course, title, criteria_list, verbose=args.verbose)
        names = [a["title"] for a in result["assignments"]]
        print(f"updated rubric {title!r} (id={result['rubric_id']}) — detached/reattached {len(names)} assignment(s): {names}")


def build_parser():
    p = argparse.ArgumentParser(prog="canvas", description="Canvas API helper CLI")
    sub = p.add_subparsers(dest="command", required=True)

    # Shared by every apply/export/import/update subcommand below via
    # parents=[verbose_parent] — a progress bar is the default for anything
    # that loops over multiple items with real API latency per item;
    # --verbose swaps that for a line printed per item instead.
    verbose_parent = argparse.ArgumentParser(add_help=False)
    verbose_parent.add_argument(
        "--verbose", action="store_true", help="Print each item as it happens instead of a progress bar"
    )

    p_courses = sub.add_parser("courses", help="Course operations")
    sub_courses = p_courses.add_subparsers(dest="subcommand", required=True)
    p_courses_list = sub_courses.add_parser("list", help="List your courses")
    p_courses_list.add_argument("--state", default="available", help="Course state filter (default: available)")
    p_courses_list.add_argument("--term", action="store_true", help="(reserved) filter by current term")
    p_courses_list.set_defaults(func=cmd_courses_list)

    p_assign = sub.add_parser("assignments", help="Assignment operations")
    sub_assign = p_assign.add_subparsers(dest="subcommand", required=True)
    p_assign_apply = sub_assign.add_parser("apply", help="Create/update assignments from a YAML file", parents=[verbose_parent])
    p_assign_apply.add_argument("--course", required=True, help="Canvas course ID")
    p_assign_apply.add_argument("--file", required=True, help="Path to assignments YAML file")
    p_assign_apply.add_argument("--dry-run", action="store_true")
    p_assign_apply.set_defaults(func=cmd_assignments_apply)
    p_assign_export = sub_assign.add_parser("export", help="Export this course's assignments to a YAML file", parents=[verbose_parent])
    p_assign_export.add_argument("--course", required=True, help="Canvas course ID")
    p_assign_export.add_argument("--out", required=True, help="Output YAML path")
    p_assign_export.set_defaults(func=cmd_assignments_export)

    p_pages = sub.add_parser("pages", help="Page operations")
    sub_pages = p_pages.add_subparsers(dest="subcommand", required=True)
    p_pages_apply = sub_pages.add_parser("apply", help="Create/update pages from a YAML file", parents=[verbose_parent])
    p_pages_apply.add_argument("--course", required=True, help="Canvas course ID")
    p_pages_apply.add_argument("--file", required=True, help="Path to pages YAML file")
    p_pages_apply.add_argument("--dry-run", action="store_true")
    p_pages_apply.set_defaults(func=cmd_pages_apply)
    p_pages_export = sub_pages.add_parser("export", help="Export this course's pages to a YAML file", parents=[verbose_parent])
    p_pages_export.add_argument("--course", required=True, help="Canvas course ID")
    p_pages_export.add_argument("--out", required=True, help="Output YAML path")
    p_pages_export.set_defaults(func=cmd_pages_export)

    p_ann = sub.add_parser("announcements", help="Announcement operations")
    sub_ann = p_ann.add_subparsers(dest="subcommand", required=True)
    p_ann_apply = sub_ann.add_parser("apply", help="Create/update announcements from a YAML file", parents=[verbose_parent])
    p_ann_apply.add_argument("--course", required=True, help="Canvas course ID")
    p_ann_apply.add_argument("--file", required=True, help="Path to announcements YAML file")
    p_ann_apply.add_argument("--dry-run", action="store_true")
    p_ann_apply.set_defaults(func=cmd_announcements_apply)
    p_ann_export = sub_ann.add_parser("export", help="Export this course's announcements to a YAML file", parents=[verbose_parent])
    p_ann_export.add_argument("--course", required=True, help="Canvas course ID")
    p_ann_export.add_argument("--out", required=True, help="Output YAML path")
    p_ann_export.set_defaults(func=cmd_announcements_export)

    p_mod = sub.add_parser("modules", help="Module operations")
    sub_mod = p_mod.add_subparsers(dest="subcommand", required=True)
    p_mod_apply = sub_mod.add_parser("apply", help="Create modules/items from a YAML file", parents=[verbose_parent])
    p_mod_apply.add_argument("--course", required=True, help="Canvas course ID")
    p_mod_apply.add_argument("--file", required=True, help="Path to modules YAML file")
    p_mod_apply.add_argument("--dry-run", action="store_true")
    p_mod_apply.set_defaults(func=cmd_modules_apply)
    p_mod_export = sub_mod.add_parser("export", help="Export this course's modules to a YAML file", parents=[verbose_parent])
    p_mod_export.add_argument("--course", required=True, help="Canvas course ID")
    p_mod_export.add_argument("--out", required=True, help="Output YAML path")
    p_mod_export.set_defaults(func=cmd_modules_export)

    p_copy = sub.add_parser("copy", help="Copy content from one course to another")
    p_copy.add_argument("--source", required=True, help="Source course ID")
    p_copy.add_argument("--dest", required=True, help="Destination course ID")
    p_copy.add_argument("--wait", action="store_true", help="Poll until migration finishes")
    p_copy.add_argument("--poll-interval", type=int, default=5)
    p_copy.set_defaults(func=cmd_copy)

    p_rubrics = sub.add_parser("rubrics", help="Rubric CSV import/export")
    sub_rubrics = p_rubrics.add_subparsers(dest="subcommand", required=True)
    p_rubrics_export = sub_rubrics.add_parser("export", help="Export course rubrics, one CSV file per rubric", parents=[verbose_parent])
    p_rubrics_export.add_argument("--course", required=True, help="Canvas course ID")
    p_rubrics_export.add_argument("--out", required=True, help="Output directory (one <rubric title>.csv per rubric)")
    p_rubrics_export.set_defaults(func=cmd_rubrics_export)

    p_rubrics_import = sub_rubrics.add_parser(
        "import", help="Import a new rubric from a CSV file (Canvas's native rubric CSV format)", parents=[verbose_parent]
    )
    p_rubrics_import.add_argument("--course", required=True, help="Canvas course ID")
    p_rubrics_import.add_argument("--file", required=True, help="Path to a rubric CSV file (one rubric per file, see `rubrics export`)")
    p_rubrics_import.add_argument("--no-wait", action="store_true", help="Don't poll for completion")
    p_rubrics_import.add_argument("--dry-run", action="store_true")
    p_rubrics_import.set_defaults(func=cmd_rubrics_import)

    p_rubrics_update = sub_rubrics.add_parser(
        "update", help="Update an existing rubric's criteria in place, without forking a duplicate", parents=[verbose_parent]
    )
    p_rubrics_update.add_argument("--course", required=True, help="Canvas course ID")
    p_rubrics_update.add_argument("--file", required=True, help="Path to a rubric CSV file (one rubric per file, see `rubrics export`)")
    p_rubrics_update.add_argument("--dry-run", action="store_true")
    p_rubrics_update.set_defaults(func=cmd_rubrics_update)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    c = CanvasClient()
    try:
        args.func(args, c)
    except CanvasError as e:
        print(f"Canvas API error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
