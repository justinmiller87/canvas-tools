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


def _safe_dirname(name, user_id):
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in (name or "")).strip()
    return f"{safe}_{user_id}" if safe else f"user_{user_id}"


def list_submissions(c, course_id, assignment_id):
    """All submissions for one assignment, with user/comments/rubric/attachments
    included in a single paginated call."""
    return c.get(
        f"courses/{course_id}/assignments/{assignment_id}/submissions",
        params={"per_page": 100, "include[]": SUBMISSION_INCLUDES},
    )


def download_submission_files(c, course_id, assignment_id, out_dir, verbose=False):
    """Save every file a student attached to their submission under
    `out_dir/<student name>_<user_id>/<original filename>`. Students with no
    file attachments (text-entry or unsubmitted) are skipped — there's
    nothing to download. Returns the number of files written."""
    submissions = list_submissions(c, course_id, assignment_id)
    os.makedirs(out_dir, exist_ok=True)
    downloaded = 0
    with Progress(len(submissions), "submissions", verbose=verbose) as progress:
        for s in submissions:
            user = s.get("user") or {}
            student_name = user.get("sortable_name") or user.get("name") or f"user {s.get('user_id')}"
            attachments = s.get("attachments") or []
            if attachments:
                student_dir = os.path.join(out_dir, _safe_dirname(student_name, s.get("user_id")))
                os.makedirs(student_dir, exist_ok=True)
                for att in attachments:
                    dest = os.path.join(student_dir, att["filename"])
                    c.download_file(att["url"], dest)
                    downloaded += 1
                    if verbose:
                        print(f"  downloaded: {student_name} -> {att['filename']}")
            progress.step(student_name)
    return downloaded


def export_submissions(c, course_id, assignment):
    """Grades, rubric assessments, comments, and submission metadata for one
    assignment -> the YAML schema `submissions apply` reads back. `user_id`
    is the authoritative key for `apply` (student names aren't guaranteed
    unique); `student` is kept alongside purely for human readability when
    editing the file."""
    submissions = list_submissions(c, course_id, assignment["id"])
    out = []
    for s in submissions:
        if s.get("workflow_state") == "unsubmitted" and s.get("score") is None:
            continue
        user = s.get("user") or {}
        item = {
            "student": user.get("sortable_name") or user.get("name") or f"user {s.get('user_id')}",
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
                {"criterion_id": crit_id, "points": data.get("points"), "comments": data.get("comments")}
                for crit_id, data in rubric_assessment.items()
            ]

        comments = s.get("submission_comments") or []
        if comments:
            item["comments"] = [
                {
                    "author": (cm.get("author_name") or ""),
                    "created_at": cm.get("created_at"),
                    "text": cm.get("comment"),
                }
                for cm in comments
            ]
        out.append(item)
    return {"assignment": assignment["name"], "submissions": out}


def apply_submissions(c, course_id, assignment_id, entries, dry_run=False, verbose=False):
    """Push `posted_grade`, `rubric_assessment`, and/or a new `comment` back
    to Canvas for each entry — one PUT per student carrying whichever of
    those three fields are present in that entry. `comment` is appended to
    the submission's comment stream (Canvas has no way to edit past
    comments), so re-applying the same file twice posts the comment twice;
    omit `comment` from an entry once it's already been sent.
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
            if entry.get("comment"):
                body["comment"] = {"text_comment": entry["comment"]}

            if not body:
                continue

            if dry_run:
                print(f"[dry-run] would UPDATE submission for {student} (user_id={user_id}): {body}")
                updated += 1
                continue

            c.put(f"courses/{course_id}/assignments/{assignment_id}/submissions/{user_id}", json=body)
            updated += 1
            if verbose:
                print(f"  updated: {student}")
    return updated
