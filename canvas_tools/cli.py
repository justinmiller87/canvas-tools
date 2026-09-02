#!/usr/bin/env python3
import argparse
import contextlib
import csv
import io
import os
import re
import sys
import time

import yaml

from canvas_tools.client import CanvasClient, CanvasError
from canvas_tools.html_clean import clean_html
from canvas_tools.rubrics import export_rubrics_csv, import_rubrics_csv, update_rubric_in_place, _parse_rubrics_csv
from canvas_tools.submissions import (
    download_submission_files,
    export_submissions,
    apply_submissions,
    assignment_dir_name,
    pull_submissions,
)
from canvas_tools.export_course import (
    export_assignment_groups,
    export_assignments,
    export_pages,
    export_announcements,
    export_modules,
    dump_data,
    write_with_confirmation,
    write_text_with_confirmation,
    OverwritePolicy,
    _archive_existing,
    find_archive_dirs,
    list_archived_files,
    select_for_cleanup,
    TIME_WINDOWS,
    find_course_export_dir,
)
from datetime import datetime
from canvas_tools.progress import Progress


def _write_single_yaml_export(args, c, export_fn, key, apply_cmd_name, **kwargs):
    data = export_fn(c, args.course, **kwargs)
    if write_with_confirmation(
        data, args.out, header_comment=f"# Exported from course {args.course} — schema matches `canvas {apply_cmd_name} apply`\n"
    ):
        print(f"wrote {len(data[key])} {key} -> {args.out}")


def cmd_assignment_groups_export(args, c):
    _write_single_yaml_export(args, c, export_assignment_groups, "assignment_groups", "assignment_groups")


def cmd_assignments_export(args, c):
    _write_single_yaml_export(args, c, export_assignments, "assignments", "assignments")


def cmd_pages_export(args, c):
    _write_single_yaml_export(args, c, export_pages, "pages", "pages", verbose=args.verbose)


def cmd_announcements_export(args, c):
    _write_single_yaml_export(args, c, export_announcements, "announcements", "announcements")


def cmd_modules_export(args, c):
    data = export_modules(c, args.course)
    if write_with_confirmation(
        data,
        args.out,
        header_comment=(
            f"# Exported from course {args.course} — schema matches `canvas modules apply`\n"
            "# `modules apply` treats this file as the exact, complete set of modules and\n"
            "# items — a module or item missing from this file gets DELETED from the course,\n"
            "# not just left alone. Always --dry-run before applying an edited copy of this file.\n"
        ),
    ):
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


def _normalize_whitespace(s):
    """Collapse any run of whitespace to a single space and strip the ends —
    Canvas titles sometimes carry a stray double space (e.g. 'Chapter 1  Writing
    Assignment'), which is invisible when copy-pasted from the Canvas UI but
    breaks an exact-match lookup."""
    return re.sub(r"\s+", " ", s).strip()


def _find_assignment_by_name(assignments, name):
    target = _normalize_whitespace(name).lower()
    for a in assignments:
        if _normalize_whitespace(a["name"]).lower() == target:
            return a
    return None


def _resolve_rename_target(existing, finder, rename_from, value, key_field, kind, course_id):
    """Look up `value` (a name or title) against `existing` via `finder`,
    honoring an optional `rename_from:` to match an existing record by its
    *old* name/title instead — so it gets renamed via a plain update,
    instead of `value` alone both finding nothing (since nothing's named
    the *new* value yet) and creating an unwanted duplicate. Mirrors
    `modules apply`'s `rename_from:` for module names. Raises CanvasError
    if rename_from is set but doesn't match anything. Returns
    (match, is_rename).
    """
    match = finder(existing, rename_from or value)
    if rename_from and not match:
        raise CanvasError(f"{kind} {value!r}: rename_from {rename_from!r} not found in course {course_id}")
    is_rename = bool(rename_from) and match is not None and match[key_field].strip().lower() != value.strip().lower()
    return match, is_rename


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
    """Update due dates/points on a checkpointed discussion — or convert an
    existing *plain* graded discussion into one, in place.

    Canvas's REST API has no endpoint for this at all — checkpoints only
    exist via the GraphQL mutation the redesigned Discussions UI uses
    internally (`updateDiscussionTopic` with `assignment: {forCheckpoints:
    true}`). That same mutation also handles the plain-to-checkpointed
    upgrade for an assignment that doesn't have sub-assignments yet —
    confirmed live, not documented anywhere — so this no longer requires
    checkpoints to already exist. When they don't, both `points_possible`
    and `due_at` must be given in full for each checkpoint in
    `checkpoints_spec` (there's no live value to fall back to yet).
    """
    current = c.get(f"courses/{course_id}/assignments/{assignment_id}", params={"include[]": "checkpoints"})
    if not current.get("discussion_topic"):
        raise CanvasError(f"assignment {assignment_id} is not a discussion — checkpoints only apply to discussions")
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

    # Canvas rejects due_at/lock_at/unlock_at on the parent assignment for a
    # checkpointed discussion — confirmed live, not documented anywhere
    # ("Cannot set lock_at in the parent assignment for checkpoints", then
    # the same for unlock_at once lock_at was removed; Canvas validates one
    # field at a time rather than reporting every violation at once). Not
    # caught by schema introspection since the fields themselves are
    # accepted by the schema, only their use here is rejected at the
    # resolver level. All date/availability info for a checkpointed
    # discussion lives entirely on the checkpoints themselves.
    assignment_input = {"courseId": str(course_id), "name": name, "forCheckpoints": True}
    if item.get("assignment_group_id") is not None:
        assignment_input["assignmentGroupId"] = str(item["assignment_group_id"])

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


def _course_students_by_name(c, course_id):
    """name (lowercased sortable_name/name) -> [user_id, ...], for resolving
    an override's `students:` list. A list, not a single id, because two
    students can share a display name — `_resolve_override_student_ids`
    treats more than one match as an error telling the caller to use
    `student_ids` instead."""
    by_name = {}
    for u in c.get(f"courses/{course_id}/users", params={"enrollment_type[]": "student", "per_page": 100}):
        name = (u.get("sortable_name") or u.get("name") or "").strip().lower()
        by_name.setdefault(name, []).append(u["id"])
    return by_name


def _course_sections_by_name(c, course_id):
    return {s["name"].strip().lower(): s["id"] for s in c.get(f"courses/{course_id}/sections", params={"per_page": 100})}


def _resolve_override_student_ids(entry, students_by_name, course_id):
    if "student_ids" in entry:
        return list(entry["student_ids"])
    names = entry.get("students") or []
    if not names:
        raise CanvasError("override entry needs student_ids, students, or section")
    ids = []
    for name in names:
        matches = students_by_name.get(name.strip().lower())
        if not matches:
            raise CanvasError(f"student {name!r} not found in course {course_id}")
        if len(matches) > 1:
            raise CanvasError(f"student name {name!r} is ambiguous in course {course_id} — use student_ids instead")
        ids.append(matches[0])
    return ids


def _override_identity(student_ids, section_id):
    return ("students", frozenset(student_ids)) if student_ids else ("section", section_id)


def _apply_assignment_overrides(c, course_id, assignment_id, overrides_spec, students_by_name, sections_by_name, dry_run=False, verbose=False):
    """Reconcile one assignment's per-student/per-section override exceptions
    (e.g. an early unlock date for one student) to exactly the set given in
    `overrides_spec` — matching the "exact, complete set" convention this
    tool uses for modules/pages: an override present in Canvas but missing
    from the file gets deleted, one present in the file but not in Canvas
    gets created, and one present in both gets updated if its dates/title
    changed.

    Entries exported as `unmanaged: true` (a group override, or some other
    kind this tool doesn't understand) are skipped entirely — left alone
    whether or not they're still present in the file — since this tool has
    no way to recreate them if they were ever deleted.

    Matched by identity (the same student set, or the same section), not by
    Canvas's internal override id, so a hand-written override with no
    `override_id` still matches its live counterpart correctly.
    """
    existing = c.get(f"courses/{course_id}/assignments/{assignment_id}/overrides", params={"per_page": 100})
    existing_managed = [ov for ov in existing if ov.get("student_ids") or ov.get("course_section_id") is not None]
    existing_by_identity = {
        _override_identity(ov.get("student_ids"), ov.get("course_section_id")): ov for ov in existing_managed
    }

    desired_by_identity = {}
    for entry in overrides_spec:
        if entry.get("unmanaged"):
            continue
        section_id = None
        student_ids = None
        if entry.get("section"):
            section_name = entry["section"].strip().lower()
            section_id = sections_by_name.get(section_name)
            if section_id is None:
                raise CanvasError(f"section {entry['section']!r} not found in course {course_id}")
        else:
            student_ids = _resolve_override_student_ids(entry, students_by_name, course_id)
        desired_by_identity[_override_identity(student_ids, section_id)] = (entry, student_ids, section_id)

    for identity, ov in existing_by_identity.items():
        if identity not in desired_by_identity:
            label = ov.get("title") or (f"section {ov.get('course_section_id')}" if ov.get("course_section_id") else "override")
            if dry_run:
                print(f"[dry-run]   would DELETE override: {label}")
                continue
            c.delete(f"courses/{course_id}/assignments/{assignment_id}/overrides/{ov['id']}")
            if verbose:
                print(f"  override deleted: {label}")

    for identity, (entry, student_ids, section_id) in desired_by_identity.items():
        body = {"due_at": entry.get("due_at"), "unlock_at": entry.get("unlock_at"), "lock_at": entry.get("lock_at")}
        if student_ids:
            body["student_ids"] = student_ids
            body["title"] = entry.get("title") or (f"{len(student_ids)} student" + ("" if len(student_ids) == 1 else "s"))
        else:
            body["course_section_id"] = section_id
        body["unassign_item"] = bool(entry.get("unassign_item"))

        existing_ov = existing_by_identity.get(identity)
        label = body.get("title") or entry.get("section")
        if existing_ov:
            unchanged = all(bool(existing_ov.get(k)) == v if k == "unassign_item" else existing_ov.get(k) == v for k, v in body.items() if k != "student_ids")
            if unchanged:
                continue
            if dry_run:
                print(f"[dry-run]   would UPDATE override: {label}")
                continue
            c.put(f"courses/{course_id}/assignments/{assignment_id}/overrides/{existing_ov['id']}", json={"assignment_override": body})
            if verbose:
                print(f"  override updated: {label}")
        else:
            if dry_run:
                print(f"[dry-run]   would CREATE override: {label}")
                continue
            c.post(f"courses/{course_id}/assignments/{assignment_id}/overrides", json={"assignment_override": body})
            if verbose:
                print(f"  override created: {label}")


def _restore_assignment_dates_after_overrides(c, course_id, assignment_id, item, verbose=False):
    """Canvas silently resets an assignment's own due_at/unlock_at/lock_at to
    null as a side effect of creating or updating one of its overrides (via
    POST/PUT to the /overrides endpoint) — confirmed live, not documented
    anywhere. Since `_apply_assignment_overrides` runs after the assignment's
    own field update, any override churn in that call can undo dates we just
    set. Re-sending just the date fields afterward, once overrides have
    settled, is what actually makes them stick."""
    date_fields = {k: item[k] for k in ("due_at", "unlock_at", "lock_at") if item.get(k) is not None}
    if not date_fields:
        return
    c.put(f"courses/{course_id}/assignments/{assignment_id}", json={"assignment": date_fields})
    if verbose:
        print(f"  dates restored after override sync: {date_fields}")


def _find_assignment_group_by_name(groups, name):
    target = _normalize_whitespace(name).lower()
    for g in groups:
        if _normalize_whitespace(g["name"]).lower() == target:
            return g
    return None


# Canvas's assignment_groups endpoint also has `rules` (drop lowest/highest,
# never-drop) and `sis_source_id`, but those aren't wired in here — kept to
# the fields actually verified live (create + update both confirmed against
# a real course) rather than fields assumed from docs.
ASSIGNMENT_GROUP_FIELDS = ("name", "position", "group_weight")


def _conventional_export_path(file_path, kind):
    """The file `{kind} export` would normally write for whatever directory
    --file lives in — e.g. .../course_29457_.../assignments.yaml — matching
    the same format (.yaml vs .json) --file itself used. Deliberately
    ignores --file's own name: applying from a scratch/edit copy like
    assignments_new.yaml should still resync the course's real
    assignments.yaml, the one `assignmentsx` would write, not that scratch
    file."""
    directory = os.path.dirname(file_path)
    ext = os.path.splitext(file_path)[1].lstrip(".") or "yaml"
    return os.path.join(directory, f"{kind}.{ext}")


def _offer_resync(args, c, export_fn, kind, **export_kwargs):
    """After a real (non-dry-run) apply or delete completes, offer to
    immediately re-export Canvas's live state back to the course's
    conventional file(s) for this resource type — the same file/directory
    `{kind} export` would normally write, regardless of what --file was
    actually named. Without this, that file silently drifts out of sync
    with anything changed outside it (directly in the Canvas UI, or by a
    separate `apply`/`delete` run) — and a later `apply` against the stale
    copy can undo those changes, e.g. by recreating something already
    deleted. Purely a convenience prompt: anything but y/yes/a/archive
    leaves every target untouched.

    If both a .yaml and a .json copy already exist alongside --file, both
    get resynced together under one shared decision (editing one format by
    hand and letting the other silently drift is exactly the kind of
    inconsistency this whole prompt exists to prevent). If only one of the
    two exists, only that one is touched — this never starts a second
    format that was never in use. If neither exists yet, defaults to
    creating a fresh .yaml, same as every other export in this tool."""
    if args.dry_run:
        return
    # Resolve against the course's actual exports/course_{id}_*/ directory,
    # not wherever --file happened to live — otherwise running an apply from
    # a scratch/trial file (as recommended when testing changes before they
    # touch the real export) silently resyncs a throwaway copy instead of
    # the production files this prompt exists to keep in sync. Falls back to
    # --file's own directory only for a course that's never been exported
    # locally at all, so this still does something sensible then.
    directory = find_course_export_dir(args.course) or os.path.dirname(args.file)
    yaml_target = os.path.join(directory, f"{kind}.yaml")
    json_target = os.path.join(directory, f"{kind}.json")
    existing_targets = [p for p in (yaml_target, json_target) if os.path.exists(p)]
    targets = existing_targets or [yaml_target]

    if existing_targets:
        names = " and ".join(repr(p) for p in targets)
        reply = input(f"\nRe-export course {args.course}'s current state to {names}? [Y]es/[N]o/[A]rchive: ").strip().lower()
        while reply not in ("y", "yes", "n", "no", "a", "archive"):
            reply = input("Please answer Y, N, or A: ").strip().lower()
        if reply in ("n", "no"):
            return
        archive = reply in ("a", "archive")
    else:
        reply = input(f"\nRe-export course {args.course}'s current state to {targets[0]!r}? [y/N]: ").strip().lower()
        if reply not in ("y", "yes"):
            return
        archive = False
    data = export_fn(c, args.course, **export_kwargs)
    for target in targets:
        if archive and os.path.exists(target):
            archived = _archive_existing(target)
            print(f"archived old copy -> {archived!r}")
        dump_data(data, target, header_comment=f"# Exported from course {args.course} — schema matches `canvas {kind} apply`\n")
        print(f"wrote {len(data[kind])} {kind} -> {target}")


def cmd_assignment_groups_apply(args, c):
    with open(args.file) as f:
        spec = yaml.safe_load(f)

    items = spec.get("assignment_groups", spec if isinstance(spec, list) else [])
    if not items:
        print("No assignment groups found in file.")
        return

    existing = c.get(f"courses/{args.course}/assignment_groups", params={"per_page": 100})

    progress = Progress(len(items), "assignment groups", verbose=args.verbose)
    for item in items:
        progress.step(item.get("name"))
        name = item["name"]
        body = {k: v for k, v in item.items() if k in ASSIGNMENT_GROUP_FIELDS and v is not None}
        match = _find_assignment_group_by_name(existing, name)
        if match:
            if args.dry_run:
                print(f"[dry-run] would UPDATE assignment group {match['id']!r}: {name}")
                continue
            c.put(f"courses/{args.course}/assignment_groups/{match['id']}", json=body)
            if args.verbose:
                print(f"updated: {name} (id={match['id']})")
        else:
            if args.dry_run:
                print(f"[dry-run] would CREATE assignment group: {name}")
                continue
            created = c.post(f"courses/{args.course}/assignment_groups", json=body)
            if args.verbose:
                print(f"created: {name} (id={created['id']})")
    progress.done()
    _offer_resync(args, c, export_assignment_groups, "assignment_groups")


def _confirm_wipe_everything(args, kind, total_count):
    """Extra escalation gate for the case where a delete file's matches cover
    every single item of this type currently in the course — an easy mistake
    to make (e.g. running `delete` against a full export instead of a
    curated subset) with an outsized blast radius. A plain 'yes' isn't
    enough here — the course id has to be typed out, so it can't be
    muscle-memory'd through the same way."""
    print(f"\n*** This deletes ALL {total_count} {kind}s currently in course {args.course} — none would remain. ***")
    token = str(args.course)
    reply = input(f"Type the course ID ({token}) to confirm: ").strip()
    if reply != token:
        print("Aborted — nothing deleted.")
        return False
    return True


def _delete_flow(args, c, kind, key_field, id_field, list_path, find_fn, list_params=None):
    """Shared confirm-then-delete flow for pages/assignments/announcements
    `delete` commands: load names from --file, resolve each against a
    freshly fetched list from Canvas — never a local export — so a typo'd
    or already-gone name fails loudly before anything happens, in
    --dry-run and for real alike. Prints the plan and requires typing
    'yes' once before deleting anything for real, plus the course-id
    escalation above if the file covers every item that currently exists.
    Unlike assignment_groups delete, none of these carry another resource
    down with them, so there's no second confirmation for that.

    Returns the list of matches to delete, or None if the caller should
    stop (empty file, --dry-run, or the user didn't confirm).
    """
    with open(args.file) as f:
        spec = yaml.safe_load(f)

    entries = spec.get(f"{kind}s", spec if isinstance(spec, list) else [])
    if not entries:
        print(f"No {kind}s found in file.")
        return None

    existing = c.get(list_path, params={**{"per_page": 100}, **(list_params or {})})

    matches = []
    for entry in entries:
        name = entry[key_field] if isinstance(entry, dict) else entry
        match = find_fn(existing, name)
        if match is None:
            raise CanvasError(f"{kind} {name!r} not found in course {args.course} (already deleted, or a typo?)")
        matches.append(match)

    wipes_everything = bool(existing) and len(matches) == len(existing)

    if args.dry_run:
        for m in matches:
            print(f"[dry-run] would DELETE {kind} ({id_field}={m[id_field]}): {m[key_field]}")
        if wipes_everything:
            print(f"[dry-run] NOTE: this is ALL {len(existing)} {kind}s in course {args.course} — none would remain.")
        return None

    print(f"About to permanently delete {len(matches)} {kind}(s) from course {args.course}:")
    if wipes_everything:
        print(f"  *** every {kind} currently in the course — none would remain ***")
    for m in matches:
        print(f"  - {m[key_field]} ({id_field}={m[id_field]})")
    reply = input("Type 'yes' to confirm: ").strip().lower()
    if reply != "yes":
        print("Aborted — nothing deleted.")
        return None

    if wipes_everything and not _confirm_wipe_everything(args, kind, len(existing)):
        return None

    return matches


def _resolve_group_assignments_interactively(existing_groups, group_id, group_name, group_assignments):
    """Called on a real (non-dry-run) `assignment_groups delete` when a
    group's file entry didn't already say move_assignments_to/
    delete_assignments — asks right at the terminal instead of erroring,
    since forcing an edit-the-file round trip for something this small is
    needless friction. Returns ("move", target_group), ("delete", None), or
    ("skip", None) — skip leaves this one group alone and out of the plan
    entirely, it isn't a stand-in for "delete the group but not the
    assignments" (Canvas itself doesn't offer that)."""
    names = ", ".join(a["name"] for a in group_assignments)
    print(f"\n{group_name!r} still has {len(group_assignments)} assignment(s) in it: {names}")
    candidates = [g for g in existing_groups if g["id"] != group_id]
    while True:
        choice = input("Move them to another group [m], delete them with the group [d], or skip this group [s]? ").strip().lower()
        if choice in ("m", "move"):
            if not candidates:
                print("No other assignment groups exist in this course to move them to.")
                continue
            print("Move to which group?")
            for i, g in enumerate(candidates, 1):
                print(f"  [{i}] {g['name']}")
            selection = input(f"Choose 1-{len(candidates)}: ").strip()
            if not selection.isdigit() or not (1 <= int(selection) <= len(candidates)):
                print("Not a valid choice — try again.")
                continue
            return "move", candidates[int(selection) - 1]
        if choice in ("d", "delete"):
            return "delete", None
        if choice in ("s", "skip"):
            return "skip", None
        print("Please enter m, d, or s.")


def cmd_assignment_groups_delete(args, c):
    with open(args.file) as f:
        spec = yaml.safe_load(f)

    items = spec.get("assignment_groups", spec if isinstance(spec, list) else [])
    if not items:
        print("No assignment groups found in file.")
        return

    # Always pulled fresh from Canvas, never from a local export — a stale
    # export could point at a group that's already gone, or miss assignments
    # added to it since the export was taken.
    existing_groups = c.get(f"courses/{args.course}/assignment_groups", params={"per_page": 100})
    existing_assignments = c.get(f"courses/{args.course}/assignments", params={"per_page": 100})

    # Resolve every group (and move target) up front, so a typo'd name, or a
    # file that sets both move_assignments_to AND delete_assignments on the
    # same entry, fails loudly before anything is touched — in --dry-run
    # and for real alike. A non-empty group with *neither* set isn't a
    # file error, though: on a real run it's resolved interactively right
    # here (see _resolve_group_assignments_interactively); --dry-run has
    # nothing to ask, so it just flags the group as unresolved and leaves
    # it out of the previewed plan.
    plan = []
    for entry in items:
        name = entry["name"] if isinstance(entry, dict) else entry
        group = _find_assignment_group_by_name(existing_groups, name)
        if group is None:
            raise CanvasError(f"assignment group {name!r} not found in course {args.course} (already deleted, or a typo?)")

        move_to_name = entry.get("move_assignments_to") if isinstance(entry, dict) else None
        delete_assignments = bool(entry.get("delete_assignments")) if isinstance(entry, dict) else False
        if move_to_name and delete_assignments:
            raise CanvasError(f"assignment group {name!r}: specify move_assignments_to OR delete_assignments, not both")

        group_assignments = [a for a in existing_assignments if a.get("assignment_group_id") == group["id"]]

        target = None
        if move_to_name:
            target = _find_assignment_group_by_name(existing_groups, move_to_name)
            if target is None:
                raise CanvasError(f"move_assignments_to group {move_to_name!r} not found in course {args.course}")
            if target["id"] == group["id"]:
                raise CanvasError(f"assignment group {name!r}: move_assignments_to can't be the same group")
        elif group_assignments and not delete_assignments:
            if args.dry_run:
                names = ", ".join(a["name"] for a in group_assignments)
                print(
                    f"[dry-run] UNRESOLVED: assignment group {name!r} has {len(group_assignments)} assignment(s) "
                    f"({names}) and no move_assignments_to/delete_assignments in the file — will ask what to do "
                    f"with them on a real run."
                )
                continue
            action, chosen_target = _resolve_group_assignments_interactively(existing_groups, group["id"], name, group_assignments)
            if action == "skip":
                print(f"skipping (left alone): {name}")
                continue
            elif action == "move":
                target = chosen_target
            else:
                delete_assignments = True

        plan.append({"group": group, "assignments": group_assignments, "target": target, "delete_assignments": delete_assignments})

    if not plan:
        print("Nothing left to delete after resolving choices.")
        return

    wipes_everything = bool(existing_groups) and len(plan) == len(existing_groups)

    if args.dry_run:
        for p in plan:
            if p["target"]:
                print(f"[dry-run] would MOVE {len(p['assignments'])} assignment(s) from {p['group']['name']!r} -> {p['target']['name']!r}")
            elif p["assignments"]:
                names = ", ".join(a["name"] for a in p["assignments"])
                print(f"[dry-run] would DELETE {len(p['assignments'])} assignment(s) along with the group: {names}")
            print(f"[dry-run] would DELETE assignment group (id={p['group']['id']}): {p['group']['name']}")
        if wipes_everything:
            print(f"[dry-run] NOTE: this is ALL {len(existing_groups)} assignment groups in course {args.course} — none would remain.")
        return

    print(f"About to permanently delete {len(plan)} assignment group(s) from course {args.course}:")
    if wipes_everything:
        print(f"  *** every assignment group currently in the course — none would remain ***")
    for p in plan:
        if p["target"]:
            print(f"  - {p['group']['name']} (id={p['group']['id']}) — moving {len(p['assignments'])} assignment(s) to {p['target']['name']!r} first")
        elif p["assignments"]:
            print(f"  - {p['group']['name']} (id={p['group']['id']}) — WILL ALSO DELETE {len(p['assignments'])} assignment(s) in it")
        else:
            print(f"  - {p['group']['name']} (id={p['group']['id']}) — empty")
    reply = input("Type 'yes' to confirm: ").strip().lower()
    if reply != "yes":
        print("Aborted — nothing deleted.")
        return

    if wipes_everything and not _confirm_wipe_everything(args, "assignment group", len(existing_groups)):
        return

    # Actually deleting assignments (not just moving or removing an empty
    # group) is irreversible in a different, bigger way than a group
    # deletion — it gets its own explicit second confirmation naming every
    # assignment about to be lost, so a rushed 'yes' above can't nuke
    # assignments as a side effect without a clear second chance to stop.
    losing_assignments = [p for p in plan if p["delete_assignments"] and p["assignments"]]
    if losing_assignments:
        total = sum(len(p["assignments"]) for p in losing_assignments)
        print(f"\nThis will PERMANENTLY DELETE {total} assignment(s), not just the group(s) they're in:")
        for p in losing_assignments:
            for a in p["assignments"]:
                print(f"  - {a['name']} (id={a['id']}) [group: {p['group']['name']}]")
        reply2 = input("Type 'yes' again to confirm permanent assignment deletion: ").strip().lower()
        if reply2 != "yes":
            print("Aborted — nothing deleted.")
            return

    progress = Progress(len(plan), "assignment groups (deleting)", verbose=args.verbose)
    for p in plan:
        progress.step(p["group"]["name"])
        if p["target"]:
            c.delete(f"courses/{args.course}/assignment_groups/{p['group']['id']}?move_assignments_to={p['target']['id']}")
        else:
            c.delete(f"courses/{args.course}/assignment_groups/{p['group']['id']}")
        if args.verbose:
            print(f"deleted: {p['group']['name']} (id={p['group']['id']})")
    progress.done()
    _offer_resync(args, c, export_assignment_groups, "assignment_groups")


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
    students_by_name = None
    sections_by_name = None

    progress = Progress(len(items), "assignments", verbose=args.verbose)
    for item in items:
        progress.step(item.get("name"))
        name = item["name"]
        item = dict(item)
        item.pop("id", None)  # read-only, written by export purely for `--assignment <id>` lookups
        checkpoints_spec = item.pop("checkpoints", None)
        overrides_spec = item.pop("overrides", None)
        rubric_title = item.pop("rubric", None)
        remove_rubric = item.pop("remove_rubric", False)
        rename_from = item.pop("rename_from", None)
        if overrides_spec is not None and students_by_name is None:
            students_by_name = _course_students_by_name(c, args.course)
            sections_by_name = _course_sections_by_name(c, args.course)

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
        match, is_rename = _resolve_rename_target(existing, _find_assignment_by_name, rename_from, name, "name", "assignment", args.course)
        if match:
            if args.dry_run:
                if is_rename:
                    print(f"[dry-run] would RENAME assignment {match['id']!r}: {match['name']!r} -> {name!r}")
                else:
                    print(f"[dry-run] would UPDATE assignment {match['id']!r}: {name}")
                if checkpoints_spec:
                    print(f"[dry-run]   would UPDATE checkpoints: {[cp['tag'] for cp in checkpoints_spec]}")
                if remove_rubric:
                    print(f"[dry-run]   would REMOVE current rubric (if any)")
                if rubric_title:
                    print(f"[dry-run]   would ATTACH rubric: {rubric_title}")
                if overrides_spec is not None:
                    _apply_assignment_overrides(
                        c, args.course, match["id"], overrides_spec, students_by_name, sections_by_name, dry_run=True
                    )
                    date_fields = {k: item[k] for k in ("due_at", "unlock_at", "lock_at") if item.get(k) is not None}
                    if date_fields:
                        print(f"[dry-run]   would RESTORE dates after override sync: {date_fields}")
                continue
            if payload["assignment"]:
                c.put(f"courses/{args.course}/assignments/{match['id']}", json=payload)
            if args.verbose:
                if is_rename:
                    print(f"renamed: {match['name']!r} -> {name!r} (id={match['id']})")
                else:
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
            if overrides_spec is not None:
                _apply_assignment_overrides(
                    c, args.course, match["id"], overrides_spec, students_by_name, sections_by_name, verbose=args.verbose
                )
                _restore_assignment_dates_after_overrides(c, args.course, match["id"], item, verbose=args.verbose)
        else:
            if args.dry_run:
                print(f"[dry-run] would CREATE assignment: {name}")
                if checkpoints_spec:
                    print(f"[dry-run]   would CREATE as a checkpointed discussion: {[cp['tag'] for cp in checkpoints_spec]}")
                if rubric_title:
                    print(f"[dry-run]   would ATTACH rubric: {rubric_title}")
                if overrides_spec is not None:
                    print(f"[dry-run]   would CREATE {len(overrides_spec)} override(s)")
                    date_fields = {k: item[k] for k in ("due_at", "unlock_at", "lock_at") if item.get(k) is not None}
                    if date_fields:
                        print(f"[dry-run]   would RESTORE dates after override sync: {date_fields}")
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
            if overrides_spec is not None:
                _apply_assignment_overrides(
                    c, args.course, created_id, overrides_spec, students_by_name, sections_by_name, verbose=args.verbose
                )
                _restore_assignment_dates_after_overrides(c, args.course, created_id, item, verbose=args.verbose)
    progress.done()
    _offer_resync(args, c, export_assignments, "assignments")


def cmd_assignments_delete(args, c):
    matches = _delete_flow(args, c, "assignment", "name", "id", f"courses/{args.course}/assignments", _find_assignment_by_name)
    if not matches:
        return
    progress = Progress(len(matches), "assignments (deleting)", verbose=args.verbose)
    for m in matches:
        progress.step(m["name"])
        c.delete(f"courses/{args.course}/assignments/{m['id']}")
        if args.verbose:
            print(f"deleted: {m['name']} (id={m['id']})")
    progress.done()
    _offer_resync(args, c, export_assignments, "assignments")


def _find_page_by_title(pages, title):
    target = _normalize_whitespace(title).lower()
    for p in pages:
        if _normalize_whitespace(p["title"]).lower() == target:
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
        rename_from = item.pop("rename_from", None)
        body = {k: v for k, v in item.items() if k in PAGE_FIELDS and v is not None}
        if todo_date is not None:
            body["student_todo_at"] = todo_date
            body["student_planner_checkbox"] = True
        if body.get("body"):
            body["body"] = clean_html(body["body"])
        payload = {"wiki_page": body}
        match, is_rename = _resolve_rename_target(existing, _find_page_by_title, rename_from, title, "title", "page", args.course)
        if match:
            if args.dry_run:
                if is_rename:
                    print(f"[dry-run] would RENAME page {match['url']!r}: {match['title']!r} -> {title!r}")
                else:
                    print(f"[dry-run] would UPDATE page {match['url']!r}: {title}")
                continue
            c.put(f"courses/{args.course}/pages/{match['url']}", json=payload)
            if args.verbose:
                if is_rename:
                    print(f"renamed: {match['title']!r} -> {title!r} (url={match['url']})")
                else:
                    print(f"updated: {title} (url={match['url']})")
        else:
            if args.dry_run:
                print(f"[dry-run] would CREATE page: {title}")
                continue
            created = c.post(f"courses/{args.course}/pages", json=payload)
            if args.verbose:
                print(f"created: {title} (url={created['url']})")
    progress.done()
    _offer_resync(args, c, export_pages, "pages", verbose=args.verbose)


def cmd_pages_delete(args, c):
    matches = _delete_flow(args, c, "page", "title", "url", f"courses/{args.course}/pages", _find_page_by_title)
    if not matches:
        return
    progress = Progress(len(matches), "pages (deleting)", verbose=args.verbose)
    for m in matches:
        progress.step(m["title"])
        c.delete(f"courses/{args.course}/pages/{m['url']}")
        if args.verbose:
            print(f"deleted: {m['title']} (url={m['url']})")
    progress.done()
    _offer_resync(args, c, export_pages, "pages", verbose=args.verbose)


def _find_announcement_by_title(announcements, title):
    target = _normalize_whitespace(title).lower()
    for a in announcements:
        if _normalize_whitespace(a["title"]).lower() == target:
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
        item = dict(item)
        rename_from = item.pop("rename_from", None)
        body = {k: v for k, v in item.items() if k in ANNOUNCEMENT_FIELDS and v is not None}
        body["is_announcement"] = True
        if body.get("message"):
            body["message"] = clean_html(body["message"])
        match, is_rename = _resolve_rename_target(existing, _find_announcement_by_title, rename_from, title, "title", "announcement", args.course)
        if match:
            if args.dry_run:
                if is_rename:
                    print(f"[dry-run] would RENAME announcement {match['id']}: {match['title']!r} -> {title!r}")
                else:
                    print(f"[dry-run] would UPDATE announcement {match['id']}: {title}")
                continue
            c.put(f"courses/{args.course}/discussion_topics/{match['id']}", json=body)
            if args.verbose:
                if is_rename:
                    print(f"renamed: {match['title']!r} -> {title!r} (id={match['id']})")
                else:
                    print(f"updated: {title} (id={match['id']})")
        else:
            if args.dry_run:
                print(f"[dry-run] would CREATE announcement: {title}")
                continue
            created = c.post(f"courses/{args.course}/discussion_topics", json=body)
            if args.verbose:
                print(f"created: {title} (id={created['id']})")
    progress.done()
    _offer_resync(args, c, export_announcements, "announcements")


def cmd_announcements_delete(args, c):
    matches = _delete_flow(
        args,
        c,
        "announcement",
        "title",
        "id",
        f"courses/{args.course}/discussion_topics",
        _find_announcement_by_title,
        list_params={"only_announcements": True},
    )
    if not matches:
        return
    progress = Progress(len(matches), "announcements (deleting)", verbose=args.verbose)
    for m in matches:
        progress.step(m["title"])
        c.delete(f"courses/{args.course}/discussion_topics/{m['id']}")
        if args.verbose:
            print(f"deleted: {m['title']} (id={m['id']})")
    progress.done()
    _offer_resync(args, c, export_announcements, "announcements")


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
    #
    # Routine partial edits (dropping a module or two while restructuring)
    # stay frictionless here, same as always — no prompt. But if the file
    # shares no module names with what's actually in the course at all
    # (wrong file, wrong course, or a near-empty file applied by mistake),
    # every existing module would be deleted in one shot; that gets the
    # same course-id escalation as the dedicated `delete` commands before
    # anything — including the creates below — happens.
    file_module_names = {mod["name"].strip().lower() for mod in modules}

    # `rename_from:` lets a module entry match an existing Canvas module by
    # its old name so it gets renamed in place (same id, items, and
    # position) instead of being deleted and recreated — Canvas's
    # module-create endpoint always appends to the end with no way to
    # request a slot, so without this a plain name edit would silently lose
    # the module's position and recreate every one of its items under new
    # ids.
    old_name_to_new = {
        mod["rename_from"].strip().lower(): mod["name"].strip().lower() for mod in modules if mod.get("rename_from")
    }

    def _effective_name(existing_name):
        lowered = existing_name.strip().lower()
        return old_name_to_new.get(lowered, lowered)

    modules_to_delete = [m for m in existing_modules if _effective_name(m["name"]) not in file_module_names]
    wipes_everything = bool(existing_modules) and len(modules_to_delete) == len(existing_modules)
    if wipes_everything:
        if args.dry_run:
            print(
                f"[dry-run] NOTE: this file shares no module names with course {args.course} — "
                f"applying it for real would delete ALL {len(existing_modules)} existing modules."
            )
        elif not _confirm_wipe_everything(args, "module", len(existing_modules)):
            return

    with Progress(len(existing_modules), "modules (checking for deletions)", verbose=args.verbose) as progress:
        for m in existing_modules:
            progress.step(m["name"])
            if _effective_name(m["name"]) not in file_module_names:
                if args.dry_run:
                    print(f"[dry-run] would DELETE module (not in file): {m['name']} (id={m['id']})")
                else:
                    c.delete(f"courses/{args.course}/modules/{m['id']}")
                    if args.verbose:
                        print(f"deleted module (not in file): {m['name']} (id={m['id']})")

    kept_existing_modules = [m for m in existing_modules if _effective_name(m["name"]) in file_module_names]

    # name (lowercased) -> id, seeded with modules kept from the course so
    # `prerequisites:` can reference modules outside this YAML file too —
    # but never a module just deleted above, so a stale prerequisite
    # reference fails loudly instead of pointing at a dead id. Keyed by each
    # module's *effective* (post-rename) name so later lookups by the file's
    # `name:` value find renamed modules too.
    module_ids = {_effective_name(m["name"]): m["id"] for m in kept_existing_modules}

    # Pass 1: ensure every module in the file exists, so real ids are known
    # before pass 2 resolves prerequisite names into prerequisite_module_ids.
    newly_created = set()
    with Progress(len(modules), "modules (creating)", verbose=args.verbose) as progress:
        for mod in modules:
            mname = mod["name"]
            progress.step(mname)
            match = next((m for m in kept_existing_modules if _effective_name(m["name"]) == mname.strip().lower()), None)
            if match:
                if match["name"].strip().lower() != mname.strip().lower():
                    if args.dry_run:
                        print(f"[dry-run] would RENAME module: {match['name']!r} -> {mname!r}")
                    else:
                        c.put(f"courses/{args.course}/modules/{match['id']}", json={"module": {"name": mname}})
                        if args.verbose:
                            print(f"renamed module: {match['name']!r} -> {mname!r} (id={match['id']})")
                elif args.verbose:
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
        for idx, mod in enumerate(modules):
            mname = mod["name"]
            progress.step(mname)
            module_id = module_ids.get(mname.strip().lower())
            prereq_names = mod.get("prerequisites") or []
            update_fields = {
                k: mod[k]
                for k in ("unlock_at", "require_sequential_progress", "publish_final_grade")
                if mod.get(k) is not None
            }
            # The file's own row order is the only place module order lives —
            # export_modules doesn't emit a numeric `position` field, and
            # nothing else here ever tells Canvas where a kept module belongs.
            # Every module below already gets a settings PUT (`published` is
            # always present on an exported file), and any update that omits
            # `position` risks Canvas falling back to "append at the end" —
            # confirmed live: an otherwise-untouched module dropped to the
            # bottom of the list after an apply that never mentioned position.
            # Pinning position to the file's index every time makes the
            # file's order authoritative instead of accidental.
            update_fields["position"] = idx + 1
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
            existing_items.sort(key=lambda it: it.get("position") or 0)
            file_items = mod.get("items", [])
            file_item_titles = {(item.get("title") or "").strip().lower() for item in file_items}

            for existing_item in existing_items:
                if existing_item.get("title", "").strip().lower() not in file_item_titles:
                    if args.dry_run:
                        print(f"  [dry-run] would REMOVE item (not in file): {existing_item['title']}")
                    else:
                        c.delete(f"courses/{args.course}/modules/{module_id}/items/{existing_item['id']}")
                        if args.verbose:
                            print(f"  removed item (not in file): {existing_item['title']}")
            existing_items = [
                it for it in existing_items if it.get("title", "").strip().lower() in file_item_titles
            ]

            # Reconcile both membership AND order against the file — matching
            # by title alone (the old behavior) only ever appended new/
            # re-titled items to the end of the module, since Canvas doesn't
            # infer position from title matching. `current_order` tracks our
            # best understanding of the live order as we mutate it, so each
            # subsequent decision (already correctly placed / move existing /
            # create new) is judged against where things actually are now,
            # not the stale pre-sync snapshot.
            by_title = {}
            for it in existing_items:
                by_title.setdefault(it.get("title", "").strip().lower(), []).append(it)
            current_order = list(existing_items)

            for idx, item in enumerate(file_items):
                title = item.get("title")
                key = (title or "").strip().lower()
                desired_pos = idx + 1  # Canvas module item positions are 1-indexed

                if idx < len(current_order) and current_order[idx].get("title", "").strip().lower() == key:
                    continue  # already sitting in the right spot

                match_list = by_title.get(key)
                if match_list:
                    existing_item = match_list.pop(0)
                    if args.dry_run:
                        print(f"  [dry-run] would MOVE item to position {desired_pos}: {title}")
                    else:
                        c.put(
                            f"courses/{args.course}/modules/{module_id}/items/{existing_item['id']}",
                            json={"module_item": {"position": desired_pos}},
                        )
                        if args.verbose:
                            print(f"  moved item to position {desired_pos}: {title}")
                    current_order.remove(existing_item)
                    current_order.insert(idx, existing_item)
                else:
                    if args.dry_run:
                        print(f"  [dry-run] would ADD item at position {desired_pos}: {title} ({item['type']})")
                        current_order.insert(idx, {"title": title})
                        continue
                    payload = _module_item_payload(
                        item, existing_assignments, existing_pages, existing_quizzes, existing_discussions, existing_files
                    )
                    payload["module_item"]["position"] = desired_pos
                    created_item = c.post(f"courses/{args.course}/modules/{module_id}/items", json=payload)
                    if args.verbose:
                        print(f"  added item at position {desired_pos}: {title}")
                    if item.get("published") is not None:
                        # `published` is not accepted on item create at all (confirmed
                        # against source — only the update endpoint handles it), so it
                        # needs this separate follow-up PUT.
                        c.put(
                            f"courses/{args.course}/modules/{module_id}/items/{created_item['id']}",
                            json={"module_item": {"published": item["published"]}},
                        )
                    current_order.insert(idx, created_item)

    _offer_resync(args, c, export_modules, "modules")


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


def _human_size(num_bytes):
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024


def _count_files(directory):
    return sum(1 for f in os.listdir(directory) if os.path.isfile(os.path.join(directory, f)))


@contextlib.contextmanager
def _raw_terminal():
    """Puts stdin into raw (unbuffered, unechoed) mode for the duration of
    an interactive keypress-driven UI — set once for the whole interaction,
    not per keystroke, since toggling back to cooked mode between reads
    lets the terminal's own line discipline (buffering, local echo) mangle
    a fast-arriving multi-byte sequence like an arrow key before the next
    read re-enters raw mode. Restores the previous settings no matter how
    the block exits. No-op on Windows, where `msvcrt.getch()` already
    reads raw without needing a mode change."""
    try:
        import termios
        import tty
    except ImportError:
        yield
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _read_key():
    """Block for a single keypress (stdin must already be in raw mode — see
    `_raw_terminal`) and return one of 'up', 'down', 'space', 'enter', 'a',
    'n', 'escape', or None for anything else. Cross-platform: `msvcrt` on
    Windows, plain reads on POSIX. A lone Escape press is told apart from
    the start of an arrow-key escape sequence via a short non-blocking
    wait for the rest of the sequence, so pressing just Escape doesn't
    hang waiting for bytes that are never coming."""
    try:
        import msvcrt
    except ImportError:
        msvcrt = None

    if msvcrt is not None:
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):
            ch2 = msvcrt.getch()
            return {b"H": "up", b"P": "down"}.get(ch2)
        if ch == b"\x03":
            raise KeyboardInterrupt
        if ch in (b"\r", b"\n"):
            return "enter"
        if ch == b" ":
            return "space"
        if ch == b"\x1b":
            return "escape"
        try:
            c = ch.decode().lower()
        except UnicodeDecodeError:
            return None
        return c if c in ("a", "n") else None

    import select

    # `os.read` on the raw fd, not `sys.stdin.read` — Python's buffered
    # TextIOWrapper can slurp extra already-arrived bytes (e.g. the "[A" of
    # an arrow key) into its own userspace buffer on the first read, which
    # `select.select` on the fd then has no way to see, making the below
    # falsely conclude nothing else is coming and misread a real escape
    # sequence as a lone Escape press.
    fd = sys.stdin.fileno()
    ch = os.read(fd, 1).decode(errors="replace")
    if ch == "\x1b":
        if select.select([fd], [], [], 0.1)[0]:
            ch2 = os.read(fd, 1).decode(errors="replace")
            if ch2 == "[" and select.select([fd], [], [], 0.1)[0]:
                ch3 = os.read(fd, 1).decode(errors="replace")
                return {"A": "up", "B": "down"}.get(ch3)
            return None
        return "escape"
    if ch in ("\r", "\n"):
        return "enter"
    if ch == " ":
        return "space"
    if ch == "\x03":
        raise KeyboardInterrupt
    c = ch.lower()
    return c if c in ("a", "n") else None


def _select_archive_dirs_noninteractive(archive_dirs, root):
    """Plain numbered-prompt fallback for when stdin isn't a real terminal
    (piped input, non-interactive automation) — the arrow-key/checkbox UI
    needs raw keypress reading, which needs a real tty."""
    print(f"\nFound {len(archive_dirs)} archive folders:")
    for i, d in enumerate(archive_dirs, start=1):
        print(f"  [{i}] {os.path.relpath(d, root)} ({_count_files(d)} file(s))")
    print("  [A]ll\n  [N]one")

    while True:
        reply = input("\nWhich archive folder(s) should be cleaned up? (comma-separated numbers, 'all', or 'none'): ").strip().lower()
        if reply in ("a", "all"):
            return archive_dirs
        if reply in ("n", "none"):
            return []
        parts = [p.strip() for p in reply.split(",") if p.strip()]
        if parts and all(p.isdigit() and 1 <= int(p) <= len(archive_dirs) for p in parts):
            chosen = sorted({int(p) for p in parts})
            return [archive_dirs[i - 1] for i in chosen]
        print(f"Enter one or more numbers from 1-{len(archive_dirs)} (comma-separated), 'all', or 'none'.")


def _select_archive_dirs(archive_dirs, root):
    """If more than one archive/ folder was found, ask which one(s) to
    actually clean up — defaulting to "clean up every archive folder under
    --path" would be a real problem the first time --path is the whole
    exports/ tree covering every course, not just the one you meant.
    Interactive checkbox UI: ↑/↓ moves the active row, space toggles it,
    a/n select all/none, enter confirms whatever's checked."""
    if len(archive_dirs) == 1:
        return archive_dirs
    if not sys.stdin.isatty():
        return _select_archive_dirs_noninteractive(archive_dirs, root)

    labels = [f"{os.path.relpath(d, root)} ({_count_files(d)} file(s))" for d in archive_dirs]
    n = len(archive_dirs)
    selected = [False] * n
    cursor = 0

    def render():
        lines = [f"{'>' if i == cursor else ' '} [{'*' if selected[i] else ' '}] {label}" for i, label in enumerate(labels)]
        lines.append("  [A]ll")
        lines.append("  [N]one")
        return lines

    # Raw mode disables the terminal's own \n -> \r\n translation (ONLCR),
    # so every line written while it's active has to end with an explicit
    # \r\n itself, or each line starts one column further right than the
    # last (confirmed live: produced an actual staircase of lines running
    # off the right edge of the terminal). The whole interactive section —
    # including the very first render, not just the redraws after a
    # keypress — lives inside one `_raw_terminal()` block so every write
    # uses the same convention throughout, rather than mixing `print()`
    # (fine, but only outside raw mode) with raw `sys.stdout.write`.
    def write_line(text=""):
        sys.stdout.write(text + "\r\n")

    with _raw_terminal():
        write_line()
        write_line(f"Found {n} archive folders (↑/↓ move, space toggle, a=all, n=none, enter=confirm):")
        lines = render()
        for line in lines:
            write_line(line)
        sys.stdout.flush()

        while True:
            key = _read_key()
            if key == "up":
                cursor = (cursor - 1) % n
            elif key == "down":
                cursor = (cursor + 1) % n
            elif key == "space":
                selected[cursor] = not selected[cursor]
            elif key == "a":
                selected = [True] * n
            elif key == "n":
                selected = [False] * n
            elif key == "escape":
                selected = [False] * n
                break
            elif key == "enter":
                break
            else:
                continue
            new_lines = render()
            sys.stdout.write(f"\x1b[{len(lines)}A")
            for line in new_lines:
                sys.stdout.write("\r\x1b[2K" + line + "\r\n")
            sys.stdout.flush()
            lines = new_lines

    return [d for d, s in zip(archive_dirs, selected) if s]


def _prompt_cleanup_mode(label=None):
    """The [A]ll/[M]ost recent/[T]ime/[C]ancel menu, optionally headed
    "For <label>, ..." when it's being asked once per selected folder
    rather than once for the whole run. Returns (mode, cutoff), or None if
    cancelled."""
    header = f"For {label}, what would you like to clean up?" if label else "What would you like to clean up?"
    print(
        f"\n{header}\n"
        "  [A]ll — delete every archived file\n"
        "  [M]ost recent — keep only the newest archived copy of each file, delete the rest\n"
        "  [T]ime — delete archived files older than a chosen cutoff\n"
        "  [C]ancel"
    )
    choice = input("> ").strip().lower()

    if choice in ("a", "all"):
        return "all", None
    if choice in ("m", "most recent", "mostrecent", "most-recent"):
        return "keep_recent", None
    if choice in ("t", "time"):
        window_prompt = "\nDelete files older than:\n" + "\n".join(
            f"  [{key}] {label}" for key, (label, _delta) in TIME_WINDOWS.items()
        )
        print(window_prompt)
        window_choice = input("> ").strip()
        if window_choice not in TIME_WINDOWS:
            print("Not a valid choice.")
            return None
        _label, delta = TIME_WINDOWS[window_choice]
        return "older_than", datetime.now() - delta
    return None


def cmd_archive_cleanup(args, c):
    archive_dirs = find_archive_dirs(args.path)
    if not archive_dirs:
        print(f"No archive folders found under {args.path!r}.")
        return

    archive_dirs = _select_archive_dirs(archive_dirs, args.path)
    if not archive_dirs:
        print("No archive folders selected — nothing to clean up.")
        return

    per_folder = False
    if len(archive_dirs) > 1:
        reply = input(
            "\nApply the same cleanup choice to every selected folder, or choose separately for each?\n"
            "  [A]ll — same choice for every folder\n"
            "  [E]ach — choose separately per folder\n> "
        ).strip().lower()
        per_folder = reply in ("e", "each")

    if per_folder:
        selected = []
        for d in archive_dirs:
            course_label = os.path.relpath(os.path.dirname(d), args.path)
            entries = list_archived_files([d])
            if not entries:
                print(f"\nFor {course_label}: no archived files, skipping.")
                continue
            result = _prompt_cleanup_mode(label=course_label)
            if result is None:
                print(f"  cancelled for {course_label} — skipping.")
                continue
            mode, cutoff = result
            selected.extend(select_for_cleanup(entries, mode, cutoff=cutoff))
    else:
        entries = list_archived_files(archive_dirs)
        if not entries:
            print(f"Found {len(archive_dirs)} archive folder(s) under {args.path!r}, but none contain any files.")
            return

        total_size = sum(os.path.getsize(e["path"]) for e in entries)
        print(
            f"Found {len(entries)} archived file(s) across {len(archive_dirs)} archive folder(s) "
            f"under {args.path!r} ({_human_size(total_size)})."
        )

        result = _prompt_cleanup_mode()
        if result is None:
            print("Cancelled — nothing deleted.")
            return
        mode, cutoff = result
        selected = select_for_cleanup(entries, mode, cutoff=cutoff)

    if not selected:
        print("Nothing matches that — nothing to delete.")
        return

    print(f"\nThe following {len(selected)} file(s) would be deleted:")
    for e in sorted(selected, key=lambda e: e["path"]):
        print(f"  {e['path']}  (archived {e['when']:%Y-%m-%d %H:%M:%S})")

    if args.dry_run:
        print("\n[dry-run] nothing deleted.")
        return

    reply = input(f"\nType 'yes' to permanently delete these {len(selected)} file(s): ").strip().lower()
    if reply != "yes":
        print("Cancelled — nothing deleted.")
        return

    for e in selected:
        os.remove(e["path"])
    print(f"deleted {len(selected)} file(s).")


def cmd_rubrics_export(args, c):
    files = export_rubrics_csv(c, args.course, verbose=args.verbose)
    os.makedirs(args.out, exist_ok=True)
    written = 0
    for filename, csv_text in files:
        if write_text_with_confirmation(os.path.join(args.out, filename), csv_text, newline=""):
            written += 1
    print(f"wrote {written} rubric(s) -> {args.out}/")


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


def _resolve_assignment(c, course_id, name):
    if str(name).strip().isdigit():
        return c.get(f"courses/{course_id}/assignments/{str(name).strip()}")
    assignments = c.get(f"courses/{course_id}/assignments", params={"per_page": 100})
    match = _find_assignment_by_name(assignments, name)
    if not match:
        raise CanvasError(f"assignment {name!r} not found in course {course_id}")
    return match


def cmd_submissions_download(args, c):
    assignment = _resolve_assignment(c, args.course, args.assignment)
    written = download_submission_files(c, args.course, assignment["id"], args.out, verbose=args.verbose)
    print(f"downloaded {written} file(s) -> {args.out}/")


def cmd_submissions_export(args, c):
    assignment = _resolve_assignment(c, args.course, args.assignment)
    base, _ext = os.path.splitext(args.out)
    comment_attachments_dir = f"{base}_comment_attachments"
    data, downloaded = export_submissions(c, args.course, assignment, comment_attachments_dir=comment_attachments_dir, verbose=args.verbose)
    if write_with_confirmation(
        data,
        args.out,
        header_comment=(
            f"# Exported from course {args.course}, assignment {assignment['name']!r} — schema matches `canvas submissions apply`\n"
            "# `comment` fields aren't included here (Canvas comments are an append-only stream, not\n"
            "# an editable field) — add a `comment:` line under a student's entry yourself before\n"
            "# `apply` to post a new one. Re-applying the same `comment:` twice posts it twice.\n"
        ),
    ):
        print(f"wrote {len(data['submissions'])} submission(s) -> {args.out}")
        if downloaded:
            print(f"downloaded {downloaded} comment attachment(s) -> {comment_attachments_dir}/")


def cmd_submissions_pull(args, c):
    """`download` + `export` in one shot: the assignment's submission
    files, the exported YAML, and any comment attachments.

    Single-assignment mode: `--out`, if given, is the exact target
    directory; if omitted, defaults to `<assignment name>_<id>` in the
    current directory (see `assignment_dir_name`).

    `--all` mode: pulls every assignment in the course (optionally
    filtered by `--match`, a case-insensitive substring against each
    assignment's name), each into its own `<assignment name>_<id>`
    subfolder under `--out` (the PARENT directory here — defaults to the
    current directory if omitted)."""
    if args.assignment and args.all:
        raise CanvasError("--assignment and --all are mutually exclusive")
    if args.match and not args.all:
        raise CanvasError("--match only applies with --all")

    if args.all:
        assignments = c.get(f"courses/{args.course}/assignments", params={"per_page": 100})
        if args.match:
            needle = args.match.lower()
            matched = [a for a in assignments if needle in a["name"].lower()]
            print(f"matched {len(matched)}/{len(assignments)} assignments against {args.match!r}")
            assignments = matched
        if not assignments:
            print("No assignments found.")
            return
        parent = args.out or "."
        policy = OverwritePolicy()
        for i, assignment in enumerate(assignments):
            if len(assignments) > 1:
                print(f"\n[{i + 1}/{len(assignments)}] {assignment['name']}")
            pull_submissions(c, args.course, assignment, os.path.join(parent, assignment_dir_name(assignment)), verbose=args.verbose, policy=policy)
        return

    if not args.assignment:
        raise CanvasError("--assignment is required unless --all is given")
    assignment = _resolve_assignment(c, args.course, args.assignment)
    out_dir = args.out or assignment_dir_name(assignment)
    pull_submissions(c, args.course, assignment, out_dir, verbose=args.verbose)


def cmd_submissions_apply(args, c):
    with open(args.file) as f:
        spec = yaml.safe_load(f)

    entries = spec.get("submissions", spec if isinstance(spec, list) else [])
    if not entries:
        print("No submissions found in file.")
        return

    assignment_name = spec.get("assignment") or args.assignment
    if not assignment_name:
        raise CanvasError("no assignment name in file and no --assignment given")
    assignment = _resolve_assignment(c, args.course, assignment_name)

    updated = apply_submissions(c, args.course, assignment["id"], entries, dry_run=args.dry_run, verbose=args.verbose)
    if args.dry_run:
        print(f"[dry-run] would update {updated} submission(s)")
    else:
        print(f"updated {updated} submission(s)")
        # Unlike assignments/pages/announcements/modules apply, there's no
        # canonical per-course filename to auto-resync here — submissions
        # export files are named per batch (chapter1.yaml, rooney.yaml, ...),
        # so re-syncing has to stay a manual step.
        print("run `submissions export` (subx) again to refresh your local copy with this course's current grades/comments.")


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

    p_ag = sub.add_parser("assignment_groups", help="Assignment group operations")
    sub_ag = p_ag.add_subparsers(dest="subcommand", required=True)
    p_ag_apply = sub_ag.add_parser("apply", help="Create/update assignment groups from a YAML file", parents=[verbose_parent])
    p_ag_apply.add_argument("--course", required=True, help="Canvas course ID")
    p_ag_apply.add_argument("--file", required=True, help="Path to assignment groups YAML file")
    p_ag_apply.add_argument("--dry-run", action="store_true")
    p_ag_apply.set_defaults(func=cmd_assignment_groups_apply)
    p_ag_export = sub_ag.add_parser("export", help="Export this course's assignment groups to a YAML file", parents=[verbose_parent])
    p_ag_export.add_argument("--course", required=True, help="Canvas course ID")
    p_ag_export.add_argument("--out", required=True, help="Output YAML path")
    p_ag_export.set_defaults(func=cmd_assignment_groups_export)
    p_ag_delete = sub_ag.add_parser(
        "delete", help="Delete assignment groups named in a YAML file (asks for confirmation)", parents=[verbose_parent]
    )
    p_ag_delete.add_argument("--course", required=True, help="Canvas course ID")
    p_ag_delete.add_argument("--file", required=True, help="Path to assignment groups delete YAML file")
    p_ag_delete.add_argument("--dry-run", action="store_true")
    p_ag_delete.set_defaults(func=cmd_assignment_groups_delete)

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
    p_assign_delete = sub_assign.add_parser(
        "delete", help="Delete assignments named in a YAML file (asks for confirmation)", parents=[verbose_parent]
    )
    p_assign_delete.add_argument("--course", required=True, help="Canvas course ID")
    p_assign_delete.add_argument("--file", required=True, help="Path to assignments delete YAML file")
    p_assign_delete.add_argument("--dry-run", action="store_true")
    p_assign_delete.set_defaults(func=cmd_assignments_delete)

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
    p_pages_delete = sub_pages.add_parser(
        "delete", help="Delete pages named in a YAML file (asks for confirmation)", parents=[verbose_parent]
    )
    p_pages_delete.add_argument("--course", required=True, help="Canvas course ID")
    p_pages_delete.add_argument("--file", required=True, help="Path to pages delete YAML file")
    p_pages_delete.add_argument("--dry-run", action="store_true")
    p_pages_delete.set_defaults(func=cmd_pages_delete)

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
    p_ann_delete = sub_ann.add_parser(
        "delete", help="Delete announcements named in a YAML file (asks for confirmation)", parents=[verbose_parent]
    )
    p_ann_delete.add_argument("--course", required=True, help="Canvas course ID")
    p_ann_delete.add_argument("--file", required=True, help="Path to announcements delete YAML file")
    p_ann_delete.add_argument("--dry-run", action="store_true")
    p_ann_delete.set_defaults(func=cmd_announcements_delete)

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

    p_archive = sub.add_parser("archive", help="Manage archived files left behind by the [A]rchive overwrite choice")
    sub_archive = p_archive.add_subparsers(dest="subcommand", required=True)
    p_archive_cleanup = sub_archive.add_parser("cleanup", help="Interactively delete old archived files")
    p_archive_cleanup.add_argument(
        "--path",
        default=os.getcwd(),
        help="Directory to search (recursively) for archive/ folders — default: the current directory, "
        "so running this from inside one course's export folder only ever touches that course's archives",
    )
    p_archive_cleanup.add_argument("--dry-run", action="store_true", help="Show what would be deleted without deleting anything")
    p_archive_cleanup.set_defaults(func=cmd_archive_cleanup)

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

    p_sub = sub.add_parser("submissions", help="Download, export, and grade student submissions")
    sub_sub = p_sub.add_subparsers(dest="subcommand", required=True)

    p_sub_download = sub_sub.add_parser(
        "download", help="Download every student's attached submission file(s) for one assignment", parents=[verbose_parent]
    )
    p_sub_download.add_argument("--course", required=True, help="Canvas course ID")
    p_sub_download.add_argument("--assignment", required=True, help="Assignment name (exact match)")
    p_sub_download.add_argument("--out", required=True, help="Output directory (one subfolder per student)")
    p_sub_download.set_defaults(func=cmd_submissions_download)

    p_sub_export = sub_sub.add_parser(
        "export",
        help="Export grades, rubric assessments, and comments (+ comment attachment files) for one assignment to a YAML file",
        parents=[verbose_parent],
    )
    p_sub_export.add_argument("--course", required=True, help="Canvas course ID")
    p_sub_export.add_argument("--assignment", required=True, help="Assignment name (exact match)")
    p_sub_export.add_argument(
        "--out", required=True, help="Output YAML path — comment attachment files (if any) go to '<out, minus extension>_comment_attachments/'"
    )
    p_sub_export.set_defaults(func=cmd_submissions_export)

    p_sub_pull = sub_sub.add_parser(
        "pull",
        help="download + export combined: submission files, exported YAML, and comment attachment files, all under one directory",
        parents=[verbose_parent],
    )
    p_sub_pull.add_argument("--course", required=True, help="Canvas course ID")
    p_sub_pull.add_argument("--assignment", help="Assignment name (exact match) — required unless --all is given")
    p_sub_pull.add_argument("--all", action="store_true", help="Pull every assignment in the course instead of one")
    p_sub_pull.add_argument(
        "--match", help="Only with --all: case-insensitive substring to match against each assignment's name"
    )
    p_sub_pull.add_argument(
        "--out",
        help="Output directory (submission_files/, submissions.yaml, comment_attachments/). Default: "
        "'<assignment name>_<id>' in the current directory. With --all, this is instead the PARENT "
        "directory each assignment's own '<name>_<id>' subfolder is created under (default: current directory).",
    )
    p_sub_pull.set_defaults(func=cmd_submissions_pull)

    p_sub_apply = sub_sub.add_parser(
        "apply",
        help="Push posted_grade / rubric_assessment / comment back to Canvas from a YAML file, one PUT per student",
        parents=[verbose_parent],
    )
    p_sub_apply.add_argument("--course", required=True, help="Canvas course ID")
    p_sub_apply.add_argument(
        "--assignment", help="Assignment name (exact match) — optional if the file has a top-level `assignment:` key, as `export` writes"
    )
    p_sub_apply.add_argument("--file", required=True, help="Path to a submissions YAML file (see `submissions export`)")
    p_sub_apply.add_argument("--dry-run", action="store_true")
    p_sub_apply.set_defaults(func=cmd_submissions_apply)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    c = CanvasClient()
    try:
        args.func(args, c)
    except CanvasError as e:
        print(f"Canvas API error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
