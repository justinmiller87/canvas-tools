"""Download/export/grade student submissions for one assignment.

Canvas's submissions endpoint doubles as both the read and write path: the
same `PUT .../submissions/:user_id` call that posts a comment also accepts
`posted_grade` and a `rubric_assessment` map keyed by criterion id — so
`apply_submissions` below sends all three in one request per student rather
than three separate ones.
"""
import os

from canvas_tools.progress import Progress

SUBMISSION_INCLUDES = ["user", "submission_comments", "rubric_assessment", "attachments"]


def _safe_prefix(name, user_id):
    """Sanitize a student's name into a filesystem-safe `<name>_<user_id>`
    prefix, used to disambiguate files from different students that land
    in the same flat output folder (e.g. two students both submitting
    `essay.docx`)."""
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in (name or "")).strip()
    return f"{safe}_{user_id}" if safe else f"user_{user_id}"


def rubric_criteria_reference(c, course_id, assignment):
    """The assignment's rubric criteria (id, name, max points) — the same
    `criterion_id`s that show up in a graded student's `rubric_assessment`,
    but available up front instead of only after someone's been graded, and
    with the criterion's actual name attached (a bare id like `_8609` alone
    doesn't tell you which rubric row it is). Returns None if the assignment
    has no rubric attached."""
    rubric_settings = assignment.get("rubric_settings") or {}
    rubric_id = rubric_settings.get("id")
    if not rubric_id:
        return None
    full = c.get(f"courses/{course_id}/rubrics/{rubric_id}")
    return [
        {"criterion_id": criterion["id"], "name": criterion.get("description", ""), "points": criterion.get("points")}
        for criterion in full.get("data", [])
    ]


def list_submissions(c, course_id, assignment_id):
    """All submissions for one assignment, with user/comments/rubric/attachments
    included in a single paginated call."""
    return c.get(
        f"courses/{course_id}/assignments/{assignment_id}/submissions",
        params={"per_page": 100, "include[]": SUBMISSION_INCLUDES},
    )


def download_submission_files(c, course_id, assignment_id, out_dir, verbose=False):
    """Save every file a student attached to their submission into the
    single flat folder `out_dir`, named `<student name>_<user_id>_<original
    filename>` — the student prefix disambiguates files that would
    otherwise collide (two students both submitting `essay.docx`). Students
    with no file attachments (text-entry or unsubmitted) are skipped —
    there's nothing to download. Returns the number of files written."""
    submissions = list_submissions(c, course_id, assignment_id)
    os.makedirs(out_dir, exist_ok=True)
    downloaded = 0
    with Progress(len(submissions), "submissions", verbose=verbose) as progress:
        for s in submissions:
            user = s.get("user") or {}
            student_name = user.get("sortable_name") or user.get("name") or f"user {s.get('user_id')}"
            attachments = s.get("attachments") or []
            if attachments:
                prefix = _safe_prefix(student_name, s.get("user_id"))
                for att in attachments:
                    dest = os.path.join(out_dir, f"{prefix}_{att['filename']}")
                    c.download_file(att["url"], dest)
                    downloaded += 1
                    if verbose:
                        print(f"  downloaded: {student_name} -> {att['filename']}")
            progress.step(student_name)
    return downloaded


def export_submissions(c, course_id, assignment, comment_attachments_dir=None, verbose=False):
    """Grades, rubric assessments, comments, and submission metadata for one
    assignment -> the YAML schema `submissions apply` reads back. `user_id`
    is the authoritative key for `apply` (student names aren't guaranteed
    unique); `student` is kept alongside purely for human readability when
    editing the file.

    A file a student submitted as their assignment work is a *submission*
    attachment (see `download_submission_files`); a file someone attached
    to a comment (e.g. via `apply`'s `comment_attachments`) is a *comment*
    attachment — a separate list Canvas tracks per comment. If
    `comment_attachments_dir` is given, every comment attachment is also
    downloaded into that single flat folder, named `<student name>_
    <user_id>_<original filename>`, alongside listing its filename/id in
    the YAML.

    If the assignment has a rubric, a top-level `rubric_criteria:` list is
    also included (see `rubric_criteria_reference`) — the criterion ids,
    names, and max points, available up front rather than only after
    someone's already been graded. Read-only, like `attachments:` —
    `apply` doesn't read this key back.

    Returns `(data, downloaded_count)`."""
    if comment_attachments_dir:
        os.makedirs(comment_attachments_dir, exist_ok=True)
    submissions = list_submissions(c, course_id, assignment["id"])
    out = []
    downloaded = 0
    with Progress(len(submissions), "submissions", verbose=verbose) as progress:
        for s in submissions:
            if s.get("workflow_state") == "unsubmitted" and s.get("score") is None:
                continue
            user = s.get("user") or {}
            student_name = user.get("sortable_name") or user.get("name") or f"user {s.get('user_id')}"
            progress.step(student_name)
            item = {
                "student": student_name,
                "user_id": s.get("user_id"),
                "workflow_state": s.get("workflow_state"),
            }
            if s.get("submitted_at"):
                item["submitted_at"] = s["submitted_at"]
            if s.get("late"):
                item["late"] = True
            if s.get("missing"):
                item["missing"] = True
            if s.get("score") is not None:
                item["score"] = s["score"]
            if s.get("grade") is not None:
                item["grade"] = s["grade"]
            if s.get("attachments"):
                item["attachments"] = [{"filename": a["filename"], "id": a["id"]} for a in s["attachments"]]

            rubric_assessment = s.get("rubric_assessment") or {}
            if rubric_assessment:
                item["rubric_assessment"] = [
                    {"criterion_id": crit_id, "points": crit_data.get("points"), "comments": crit_data.get("comments")}
                    for crit_id, crit_data in rubric_assessment.items()
                ]

            comments = s.get("submission_comments") or []
            if comments:
                comment_items = []
                for cm in comments:
                    comment_item = {
                        "author": (cm.get("author_name") or ""),
                        "created_at": cm.get("created_at"),
                        "text": cm.get("comment"),
                    }
                    cm_attachments = cm.get("attachments") or []
                    if cm_attachments:
                        comment_item["attachments"] = [{"filename": a["filename"], "id": a["id"]} for a in cm_attachments]
                        if comment_attachments_dir:
                            prefix = _safe_prefix(student_name, s.get("user_id"))
                            for a in cm_attachments:
                                dest = os.path.join(comment_attachments_dir, f"{prefix}_{a['filename']}")
                                c.download_file(a["url"], dest)
                                downloaded += 1
                                if verbose:
                                    print(f"  downloaded comment attachment: {student_name} -> {a['filename']}")
                    comment_items.append(comment_item)
                item["comments"] = comment_items
            out.append(item)
    result = {"assignment": assignment["name"]}
    rubric_criteria = rubric_criteria_reference(c, course_id, assignment)
    if rubric_criteria:
        result["rubric_criteria"] = rubric_criteria
    result["submissions"] = out
    return result, downloaded


def apply_submissions(c, course_id, assignment_id, entries, dry_run=False, verbose=False):
    """Push `posted_grade`, `rubric_assessment`, and/or a new `comment`
    (optionally with `comment_attachments`) back to Canvas for each entry —
    one PUT per student carrying whichever of those fields are present in
    that entry. `comment`/`comment_attachments` are appended to the
    submission's comment stream (Canvas has no way to edit past comments),
    so re-applying the same file twice posts the comment (and re-uploads
    its attachments) twice; omit them from an entry once already sent.
    `comment_attachments` are local file paths, resolved relative to the
    current working directory — each is uploaded via Canvas's per-comment
    file-upload endpoint before the PUT, and can stand alone without
    `comment` text.
    Returns the number of students actually updated (or that would be, in
    dry-run)."""
    updated = 0
    with Progress(len(entries), "submissions", verbose=verbose) as progress:
        for entry in entries:
            student = entry.get("student", f"user {entry.get('user_id')}")
            progress.step(student)
            user_id = entry["user_id"]

            body = {}
            if entry.get("posted_grade") is not None:
                body["submission"] = {"posted_grade": entry["posted_grade"]}
            if entry.get("rubric_assessment"):
                body["rubric_assessment"] = {
                    str(crit["criterion_id"]): {
                        k: v for k, v in {"points": crit.get("points"), "comments": crit.get("comments")}.items()
                        if v is not None
                    }
                    for crit in entry["rubric_assessment"]
                }
            attachment_paths = entry.get("comment_attachments") or []
            if entry.get("comment") or attachment_paths:
                comment = {}
                if entry.get("comment"):
                    comment["text_comment"] = entry["comment"]
                if attachment_paths:
                    comment["file_ids"] = attachment_paths  # placeholder; resolved to real ids below (or shown as paths in dry-run)
                body["comment"] = comment

            if not body:
                continue

            if dry_run:
                print(f"[dry-run] would UPDATE submission for {student} (user_id={user_id}): {body}")
                updated += 1
                continue

            if attachment_paths:
                upload_path = f"courses/{course_id}/assignments/{assignment_id}/submissions/{user_id}/comments/files"
                body["comment"]["file_ids"] = [c.upload_file(upload_path, path)["id"] for path in attachment_paths]
                if verbose:
                    print(f"  uploaded {len(attachment_paths)} attachment(s) for {student}")

            c.put(f"courses/{course_id}/assignments/{assignment_id}/submissions/{user_id}", json=body)
            updated += 1
            if verbose:
                print(f"  updated: {student}")
    return updated
