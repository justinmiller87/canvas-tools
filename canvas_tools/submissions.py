"""Download/export/grade student submissions for one assignment.

Canvas's submissions endpoint doubles as both the read and write path: the
same `PUT .../submissions/:user_id` call that posts a comment also accepts
`posted_grade` and a `rubric_assessment` map keyed by criterion id — so
`apply_submissions` below sends all three in one request per student rather
than three separate ones.
"""
import os
import zipfile

from canvas_tools.progress import Progress

SUBMISSION_INCLUDES = ["user", "submission_comments", "rubric_assessment", "attachments"]

# Assignment `submission_types` with no actual submission content for this
# tool to pull — a quiz's/discussion's own answers live elsewhere (the
# quiz-submissions/discussion-entries APIs, not this one), and these three
# have nothing at all. An assignment with any OTHER type (online_upload,
# online_text_entry, online_url, media_recording, student_annotation, ...)
# is kept even if it also lists one of these.
_NO_SUBMISSION_CONTENT_TYPES = {"online_quiz", "discussion_topic", "not_graded", "none", "external_tool", "wiki_page", "on_paper"}


def has_downloadable_submissions(assignment):
    """True if the assignment's `submission_types` indicates students
    actually turn in something (a file, text, url, or recording) worth
    pulling via `submissions download`/`export`/`pull` — false for
    quizzes, discussions, and other assignment shadow-types Canvas's
    `/assignments` endpoint returns that have nothing of that kind. Used
    to filter bulk pulls (`submissions pull --all`, `course export
    --submissions`) — an explicitly-named single `--assignment` is never
    filtered, in case someone really does want one of these."""
    types = set(assignment.get("submission_types") or [])
    return bool(types) and not types.issubset(_NO_SUBMISSION_CONTENT_TYPES)


def _download_and_extract(c, url, dest):
    """`CanvasClient.download_file`, plus: if what got downloaded is a
    .zip (a student can zip up a multi-file project before submitting),
    also extract it into a same-named sibling folder next to it — the zip
    itself is kept, not deleted, in case anything about the original
    matters (its raw bytes, a broken extraction, etc.)."""
    c.download_file(url, dest)
    if dest.lower().endswith(".zip"):
        extract_dir = dest[:-4]
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(dest) as zf:
            zf.extractall(extract_dir)


def _safe_prefix(name, user_id):
    """Sanitize a student's name into a filesystem-safe `<name>_<user_id>`
    prefix, used to disambiguate files from different students that land
    in the same flat output folder (e.g. two students both submitting
    `essay.docx`)."""
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in (name or "")).strip()
    return f"{safe}_{user_id}" if safe else f"user_{user_id}"


def assignment_dir_name(assignment):
    """Sanitize an assignment's name into a filesystem-safe `<id>_<name>`
    folder name (e.g. `928431_Module_01_-_Real-World_Exercises`) — the
    default `submissions pull` target when `--out` isn't given, and each
    assignment's own subfolder name under `--all`/`course export
    --submissions`. Id first so folders sort numerically/chronologically
    by assignment creation order rather than alphabetically by name."""
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in (assignment.get("name") or "")).strip()
    safe = safe.replace(" ", "_")
    return f"{assignment['id']}_{safe}" if safe else str(assignment["id"])


def pull_submissions(c, course_id, assignment, out_dir, verbose=False, policy=None):
    """`download` + `export` combined into one `out_dir`: the assignment's
    submission files, the exported YAML, and any comment attachments.
    Shared by `submissions pull` (single assignment or --all) and `course
    export --submissions`, so both call sites behave identically. `policy`
    is an `OverwritePolicy` to share a single Y/N/A decision across a
    multi-assignment run — see `write_with_confirmation`.
    Returns (submission_count, downloaded_comment_attachment_count)."""
    # Imported here, not at module level: export_course.py doesn't import
    # anything from this module, so importing it back would be a cycle —
    # this module is the one that has to defer instead.
    from canvas_tools.export_course import write_with_confirmation

    os.makedirs(out_dir, exist_ok=True)
    written = download_submission_files(c, course_id, assignment["id"], os.path.join(out_dir, "submission_files"), verbose=verbose)
    print(f"downloaded {written} submission file(s) -> {out_dir}/submission_files/")

    comment_attachments_dir = os.path.join(out_dir, "comment_attachments")
    data, downloaded = export_submissions(c, course_id, assignment, comment_attachments_dir=comment_attachments_dir, verbose=verbose)
    yaml_path = os.path.join(out_dir, "submissions.yaml")
    if write_with_confirmation(
        data,
        yaml_path,
        header_comment=(
            f"# Exported from course {course_id}, assignment {assignment['name']!r} — schema matches `canvas submissions apply`\n"
            "# `comment` fields aren't included here (Canvas comments are an append-only stream, not\n"
            "# an editable field) — add a `comment:` line under a student's entry yourself before\n"
            "# `apply` to post a new one. Re-applying the same `comment:` twice posts it twice.\n"
        ),
        policy=policy,
    ):
        print(f"wrote {len(data['submissions'])} submission(s) -> {yaml_path}")
        if downloaded:
            print(f"downloaded {downloaded} comment attachment(s) -> {comment_attachments_dir}/")
    return len(data["submissions"]), downloaded


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


def list_submissions(c, course_id, assignment_id, extra_includes=None):
    """All submissions for one assignment, with user/comments/rubric/attachments
    included in a single paginated call. `extra_includes` adds to
    `SUBMISSION_INCLUDES` for one call site without affecting the others
    (e.g. `download_submission_files` alone needs `submission_history`)."""
    return c.get(
        f"courses/{course_id}/assignments/{assignment_id}/submissions",
        params={"per_page": 100, "include[]": SUBMISSION_INCLUDES + list(extra_includes or [])},
    )


def download_submission_files(c, course_id, assignment_id, out_dir, verbose=False):
    """Save every file a student attached to their submission into
    `out_dir`, across every attempt (not just their current/latest one) —
    a resubmission can reuse the same filename on a later attempt, which
    would otherwise silently overwrite the earlier attempt's file.

    Subfolders are only ever created for a genuinely multi-file attempt —
    single-file attempts always stay flat, whether or not there are
    multiple attempts:
    - One attempt, one file: `<student name>_<user_id>_<original
      filename>` — the prefix disambiguates files that would otherwise
      collide across students (two people both submitting `essay.docx`).
    - One attempt, multiple files: `<student name>_<user_id>/<original
      filename>` — the student's own subfolder, unprefixed inside it, same
      as Canvas presented them.
    - Multiple attempts, a given attempt with one file: `<student name>_
      <user_id>_Attempt_<n>_<original filename>` — still flat, just with
      the attempt number worked into the filename, since same-named files
      across attempts would otherwise collide.
    - Multiple attempts, a given attempt with multiple files: `<student
      name>_<user_id>/Attempt_<n>/<original filename>` — that attempt's
      own subfolder under the student's.

    Students with no file attachments on any attempt (text-entry only, or
    never submitted) are skipped — there's nothing to download. Returns
    the number of files written."""
    submissions = list_submissions(c, course_id, assignment_id, extra_includes=["submission_history"])
    os.makedirs(out_dir, exist_ok=True)
    downloaded = 0
    with Progress(len(submissions), "submissions", verbose=verbose) as progress:
        for s in submissions:
            user = s.get("user") or {}
            student_name = user.get("sortable_name") or user.get("name") or f"user {s.get('user_id')}"
            attempts = [h for h in (s.get("submission_history") or []) if h.get("attachments")]
            if not attempts:
                progress.step(student_name)
                continue
            prefix = _safe_prefix(student_name, s.get("user_id"))
            multi_attempt = len(attempts) > 1
            for h in attempts:
                attachments = h["attachments"]
                attempt_tag = f"Attempt_{h.get('attempt')}" if multi_attempt else None
                if len(attachments) > 1:
                    base_dir = os.path.join(out_dir, prefix, attempt_tag) if attempt_tag else os.path.join(out_dir, prefix)
                    os.makedirs(base_dir, exist_ok=True)
                else:
                    base_dir = None
                for att in attachments:
                    if base_dir:
                        dest = os.path.join(base_dir, att["filename"])
                    elif attempt_tag:
                        dest = os.path.join(out_dir, f"{prefix}_{attempt_tag}_{att['filename']}")
                    else:
                        dest = os.path.join(out_dir, f"{prefix}_{att['filename']}")
                    _download_and_extract(c, att["url"], dest)
                    downloaded += 1
                    if verbose:
                        label = f"{student_name} attempt {h.get('attempt')}" if multi_attempt else student_name
                        print(f"  downloaded: {label} -> {att['filename']}")
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
            if s.get("attempt") is not None:
                item["attempt"] = s["attempt"]
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
                                _download_and_extract(c, a["url"], dest)
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

    If the entry has an `attempt` (as `export` now includes), the comment
    is tagged to that attempt number. Without this, a student with more
    than one submission attempt can end up with a comment SpeedGrader
    doesn't surface under their current attempt — Canvas leaves an
    untagged comment's `attempt` as null, and its newer per-attempt
    SpeedGrader view only reliably shows a comment under the specific
    attempt it's tagged with (confirmed by two multi-attempt students
    whose otherwise-identical comments weren't visible until switching
    SpeedGrader to an earlier attempt tab).

    `late_policy_status` (one of Canvas's own values: "late", "missing",
    "none", "extended") overrides Canvas's automatic late/missing
    determination for that submission — e.g. "none" clears an
    automatically-applied late penalty for a student whose late resubmission
    shouldn't count against them (their first, on-time attempt is what's
    actually being graded).

    Returns the number of students actually updated (or that would be, in
    dry-run)."""
    updated = 0
    with Progress(len(entries), "submissions", verbose=verbose) as progress:
        for entry in entries:
            student = entry.get("student", f"user {entry.get('user_id')}")
            progress.step(student)
            user_id = entry["user_id"]

            body = {}
            submission_fields = {
                k: v
                for k, v in {
                    "posted_grade": entry.get("posted_grade"),
                    "late_policy_status": entry.get("late_policy_status"),
                }.items()
                if v is not None
            }
            if submission_fields:
                body["submission"] = submission_fields
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
                if entry.get("attempt") is not None:
                    comment["attempt"] = entry["attempt"]
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
