"""Canvas rubric CSV import/export — the same CSV format Canvas's own
"Import Rubric" feature in the UI uses (GET /rubrics/upload_template).

Column shape (confirmed against the live template endpoint and Canvas's
RubricImport source): Rubric Name, Criteria Name, Criteria Description,
Criteria Enable Range, then repeating (Rating Name, Rating Description,
Rating Points) triples — one column-triple per rating, as many as the
widest criterion in the file needs. Multiple rows sharing the same Rubric
Name become criteria of one rubric.

Notes from testing against a live course:
- Import creates rubrics with workflow_state "draft", associated only with
  the course — they won't show up in `GET .../rubrics` (which excludes
  drafts), the Canvas UI's rubric picker for other assignments, or
  `export_rubrics_csv` above until activated. Attaching a draft rubric
  directly to an assignment via rubric_associations (see cli.py's rubric
  handling in `assignments apply`) does work without activating first —
  functionally the rubric grades fine — but it stays invisible everywhere
  else in Canvas, including to a human picking a rubric by hand in the UI,
  until it's activated. So this module activates on import automatically.
- The Canvas UI's own "Save Rubric" button (kebab menu > Edit > Save
  Rubric on a draft) is what activates a rubric. Traced its network
  request: it PATCHes the CLASSIC (non-`/api/v1/`) route
  `/courses/:course_id/rubrics/:id`, which requires a logged-in browser
  session — a personal API token gets "Session Timeout" there, confirmed
  by trying it directly. The API-token-reachable route
  (`/api/v1/courses/:course_id/rubrics/:id`) accepts the same fields
  though, and produces the identical result — that's what's used below.
- Do NOT PUT only `rubric[workflow_state]` on either route — confirmed
  destructive: without resupplying the full `rubric[title]` and
  `rubric[criteria]` in the same call, it silently wipes both instead of
  just changing state. Always resend the complete rubric.
"""
import csv
import io
import sys
import time

from canvas_tools.client import CanvasError
from canvas_tools.progress import Progress


def _safe_filename(title):
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in title).strip()
    return f"{safe}.csv"


def export_rubrics_csv(c, course_id, verbose=False):
    """One CSV file per rubric — not one file for the whole course. Editing a
    single rubric (the common case, e.g. via `rubrics update`) means editing
    one small, unambiguous file, not picking the right rows out of a big one
    shared across every rubric in the course."""
    rubrics = c.get(f"courses/{course_id}/rubrics", params={"per_page": 100})
    files = []
    progress = Progress(len(rubrics), "rubrics", verbose=verbose)
    for r in rubrics:
        full = c.get(f"courses/{course_id}/rubrics/{r['id']}")
        if verbose:
            print(f"  fetched: {full['title']}")
        rows = []
        max_ratings = 0
        for criterion in full.get("data", []):
            ratings = criterion.get("ratings", [])
            max_ratings = max(max_ratings, len(ratings))
            row = [
                full["title"],
                criterion.get("description", ""),
                criterion.get("long_description", ""),
                "true" if criterion.get("criterion_use_range") else "false",
            ]
            for rating in ratings:
                row += [rating.get("description", ""), rating.get("long_description", ""), rating.get("points", "")]
            rows.append(row)

        header = ["Rubric Name", "Criteria Name", "Criteria Description", "Criteria Enable Range"]
        for _ in range(max_ratings):
            header += ["Rating Name", "Rating Description", "Rating Points"]

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row + [""] * (len(header) - len(row)))
        files.append((_safe_filename(full["title"]), buf.getvalue()))
        progress.step(full["title"])

    progress.done()
    return files


_RUBRICS_BY_COURSE_QUERY = """
query($courseId: ID!) {
  course(id: $courseId) {
    rubricsConnection { nodes { _id title workflowState } }
  }
}
"""


def activate_rubric(c, course_id, rubric_id):
    """Move a draft rubric to active, the same way the Canvas UI's "Save
    Rubric" button does — resending its full title/criteria alongside the
    state change (see module docstring for why that's required)."""
    current = c.get(f"courses/{course_id}/rubrics/{rubric_id}")

    criteria = {}
    for i, crit in enumerate(current.get("data", [])):
        ratings = {
            str(j): {
                "description": r.get("description", ""),
                "long_description": r.get("long_description", ""),
                "points": r.get("points"),
            }
            for j, r in enumerate(crit.get("ratings", []))
        }
        criteria[str(i)] = {
            "description": crit.get("description", ""),
            "long_description": crit.get("long_description", ""),
            "points": crit.get("points"),
            "criterion_use_range": crit.get("criterion_use_range", False),
            "ratings": ratings,
        }

    payload = {
        "rubric": {
            "title": current["title"],
            "criteria": criteria,
            "workflow_state": "active",
            "free_form_criterion_comments": current.get("free_form_criterion_comments", False),
        },
        "rubric_association": {
            "association_type": "Course",
            "association_id": course_id,
            "purpose": "bookmark",
        },
    }
    c.put(f"courses/{course_id}/rubrics/{rubric_id}", json=payload)


def import_rubrics_csv(c, course_id, csv_bytes, filename="rubrics.csv", wait=True, poll_interval=1, timeout=60, verbose=False):
    resp = c._request(
        "POST",
        f"courses/{course_id}/rubrics/upload",
        files={"attachment": (filename, csv_bytes, "text/csv")},
    )
    result = resp.json()
    import_id = result["id"]

    if not wait:
        return result

    elapsed = 0
    status = None
    while elapsed < timeout:
        status = c.get(f"courses/{course_id}/rubrics/upload/{import_id}")
        if verbose:
            print(f"  import status: {status['workflow_state']} ({status.get('progress', 0)}%)")
        elif sys.stdout.isatty():
            sys.stdout.write(".")
            sys.stdout.flush()
        if status["workflow_state"] in ("succeeded", "succeeded_with_errors", "failed"):
            if status["workflow_state"] == "failed" or status.get("error_count"):
                raise CanvasError(f"rubric import failed: {status.get('error_data')}")
            break
        time.sleep(poll_interval)
        elapsed += poll_interval
    else:
        raise CanvasError(f"rubric import {import_id} did not finish within {timeout}s")
    if not verbose and sys.stdout.isatty():
        sys.stdout.write("\n")

    # The import API never returns which rubric ids it created, so match by
    # title against the names actually in this CSV, restricted to drafts (a
    # prior activated rubric that happens to share a title is left alone).
    reader = csv.reader(io.StringIO(csv_bytes.decode()))
    next(reader, None)
    imported_names = {row[0].strip().lower() for row in reader if row and row[0].strip()}

    course_rubrics = c.graphql(_RUBRICS_BY_COURSE_QUERY, {"courseId": str(course_id)})["course"]["rubricsConnection"][
        "nodes"
    ]
    activated = []
    for r in course_rubrics:
        if r["workflowState"] == "draft" and r["title"].strip().lower() in imported_names:
            activate_rubric(c, course_id, r["_id"])
            activated.append(r["title"])

    status["activated_rubrics"] = activated
    return status


def _parse_rubrics_csv(csv_bytes):
    """Group CSV rows into {rubric title: [criterion, ...]}, each criterion
    shaped like {description, long_description, criterion_use_range,
    ratings: [{description, long_description, points}, ...]}."""
    reader = csv.reader(io.StringIO(csv_bytes.decode()))
    next(reader, None)  # header
    rubrics = {}
    for row in reader:
        if not row or not row[0].strip():
            continue
        title, crit_name, crit_desc, enable_range = row[0], row[1], row[2], row[3]
        ratings = []
        for i in range(4, len(row), 3):
            triple = row[i : i + 3]
            if len(triple) < 3 or not triple[0].strip():
                continue
            ratings.append(
                {"description": triple[0], "long_description": triple[1], "points": float(triple[2])}
            )
        rubrics.setdefault(title.strip(), []).append(
            {
                "description": crit_name,
                "long_description": crit_desc,
                "criterion_use_range": enable_range.strip().lower() == "true",
                "ratings": ratings,
            }
        )
    return rubrics


def _criteria_list_to_payload_dict(criteria_list):
    return {
        str(i): {
            "description": crit["description"],
            "long_description": crit["long_description"],
            "criterion_use_range": crit["criterion_use_range"],
            "points": max((r["points"] for r in crit["ratings"]), default=0),
            "ratings": {
                str(j): {"description": r["description"], "long_description": r["long_description"], "points": r["points"]}
                for j, r in enumerate(crit["ratings"])
            },
        }
        for i, crit in enumerate(criteria_list)
    }


def update_rubric_in_place(c, course_id, rubric_title, criteria_list, dry_run=False, verbose=False):
    """Update an existing rubric's criteria.

    Earlier version of this tried to pre-emptively detach every assignment
    using the rubric, then edit it "safely" with zero usages. That's
    backwards and it cost a real rubric: Canvas auto-deletes a rubric when
    its last grading association is removed UNLESS it also has a
    course-level "bookmark" association, and there's no reliable way from
    the API to add that bookmark to a rubric that doesn't already have
    one — every method tried (a direct rubric_associations POST, and the
    rubrics/:id PUT that works fine for rubrics this tool created itself)
    either 500s or forks into a new rubric on some rubrics, unpredictably,
    for reasons that didn't fully resolve even reading Canvas's source.

    So this works WITH Canvas's fork behavior instead of trying to prevent
    it. Canvas already guarantees: submitting an update to a rubric with
    2+ grading associations forks — creates a new rubric with the new
    content, auto-renamed if needed to avoid a title collision, and never
    touches the original or its associations. That's true no matter what
    the original's bookmark status is, so it can't hit the same failure.
    Sequence:
      1. Submit the new criteria while the rubric is still fully attached
         everywhere it started — this either updates in place (if it only
         had 0-1 usages to begin with, nothing left to do) or forks.
      2. If it forked: detach the OLD rubric from every assignment it was
         on (safe to let Canvas do whatever it wants with the orphaned
         original now — delete it, leave it — its content already lives
         in the fork, so it no longer matters).
      3. Rename the fork back to the clean requested title (safe now that
         the original isn't holding that title against it) and reattach
         it everywhere the original was, restoring each assignment's
         use_for_grading setting.
    """
    rubrics = c.get(f"courses/{course_id}/rubrics", params={"per_page": 100})
    rubric = next((r for r in rubrics if r["title"].strip().lower() == rubric_title.strip().lower()), None)
    if not rubric:
        raise CanvasError(f"rubric {rubric_title!r} not found in course {course_id} — use `rubrics import` to create it first")
    old_id = rubric["id"]

    locations = c.get(f"courses/{course_id}/rubrics/{old_id}/used_locations")
    assignments = [a for loc in locations for a in loc.get("assignments", [])]

    if dry_run:
        return {"rubric_id": old_id, "assignments": assignments, "dry_run": True}

    # Capture use_for_grading per assignment, and each one's current
    # association id, BEFORE submitting the edit — need both to restore
    # them later regardless of which rubric id ends up holding the title.
    current_assoc = {}
    with Progress(len(assignments), "reading current settings", verbose=verbose) as progress:
        for a in assignments:
            full = c.get(f"courses/{course_id}/assignments/{a['id']}")
            assoc = c.graphql(
                "query($id: ID!) { assignment(id: $id) { rubricAssociation { _id } } }", {"id": str(a["id"])}
            )["assignment"]["rubricAssociation"]
            current_assoc[a["id"]] = {
                "title": a["title"],
                "use_for_grading": full.get("use_rubric_for_grading", False),
                "association_id": assoc["_id"] if assoc else None,
            }
            progress.step(a["title"])
            if verbose:
                print(f"  read: {a['title']}")

    current = c.get(f"courses/{course_id}/rubrics/{old_id}")
    payload = {
        "rubric": {
            "title": rubric_title,
            "criteria": _criteria_list_to_payload_dict(criteria_list),
            "workflow_state": "active",
            "free_form_criterion_comments": current.get("free_form_criterion_comments", False),
        },
    }
    result = c.put(f"courses/{course_id}/rubrics/{old_id}", json=payload)
    new_id = result["rubric"]["id"]

    if new_id == old_id:
        # 0-1 usages to begin with — updated cleanly in place, nothing else to do.
        return {"rubric_id": new_id, "assignments": list(current_assoc.values()), "dry_run": False, "forked": False}

    # Forked — the fork holds an auto-renamed title (e.g. "X (1)") since the
    # old rubric still holds the clean one at this point. Detach the old
    # rubric from everywhere it was.
    with Progress(len(current_assoc), "detaching", verbose=verbose) as progress:
        for aid, info in current_assoc.items():
            if info["association_id"]:
                c.delete(f"courses/{course_id}/rubric_associations/{info['association_id']}")
            progress.step(info["title"])
            if verbose:
                print(f"  detached: {info['title']}")

    # Don't assume the old rubric disappears on its own — the exact
    # uncertainty that caused the original incident. Check explicitly and
    # delete it if it's still hanging around; safe now, its content already
    # lives in the fork. Only then is the clean title actually free.
    still_there = c.get(f"courses/{course_id}/rubrics", params={"per_page": 100})
    if any(r["id"] == old_id for r in still_there):
        c.delete(f"courses/{course_id}/rubrics/{old_id}")

    # Rename the fork to the clean requested title — must pass it
    # explicitly, not resend whatever `activate_rubric` would read back as
    # the fork's current (auto-renamed) title.
    fork_criteria = _criteria_list_to_payload_dict(criteria_list)
    c.put(
        f"courses/{course_id}/rubrics/{new_id}",
        json={
            "rubric": {
                "title": rubric_title,
                "criteria": fork_criteria,
                "workflow_state": "active",
                "free_form_criterion_comments": current.get("free_form_criterion_comments", False),
            }
        },
    )

    with Progress(len(current_assoc), "reattaching", verbose=verbose) as progress:
        for aid, info in current_assoc.items():
            c.post(
                f"courses/{course_id}/rubric_associations",
                json={
                    "rubric_association": {
                        "rubric_id": new_id,
                        "association_id": aid,
                        "association_type": "Assignment",
                        "purpose": "grading",
                        "use_for_grading": info["use_for_grading"],
                    }
                },
            )
            progress.step(info["title"])
            if verbose:
                print(f"  reattached: {info['title']}")

    return {"rubric_id": new_id, "assignments": list(current_assoc.values()), "dry_run": False, "forked": True}
