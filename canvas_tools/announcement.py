#!/usr/bin/env python3
"""Post one announcement to multiple Canvas courses at once.

Usage:
    python3 -m canvas_tools.announcement --courses 10001 10002 --title "..." --message "<p>...</p>"
    python3 -m canvas_tools.announcement --courses 10001 10002 --title "..." --file message.html

Skips a course if an announcement with the same title already exists there
(so it's safe to re-run), unless --force is given.
"""
import argparse

from canvas_tools.client import CanvasClient, CanvasError
from canvas_tools.html_clean import clean_html
from canvas_tools.progress import Progress


def post_announcement(c, course_id, title, message, published=True, delayed_post_at=None, force=False, dry_run=False, verbose=True):
    existing = c.get(f"courses/{course_id}/discussion_topics", params={"only_announcements": True, "per_page": 100})
    match = next((a for a in existing if a["title"].strip().lower() == title.strip().lower()), None)

    if match and not force:
        if verbose:
            print(f"course {course_id}: SKIPPED — announcement {title!r} already exists (id={match['id']})")
        return

    body = {
        "title": title,
        "message": message,
        "is_announcement": True,
        "published": published,
    }
    if delayed_post_at:
        body["delayed_post_at"] = delayed_post_at

    if dry_run:
        verb = "UPDATE" if match else "CREATE"
        if verbose:
            print(f"[dry-run] course {course_id}: would {verb} announcement {title!r}")
        return

    if match:
        c.put(f"courses/{course_id}/discussion_topics/{match['id']}", json=body)
        if verbose:
            print(f"course {course_id}: updated (id={match['id']})")
    else:
        created = c.post(f"courses/{course_id}/discussion_topics", json=body)
        if verbose:
            print(f"course {course_id}: posted (id={created['id']})")


def main(argv=None):
    p = argparse.ArgumentParser(description="Post one announcement to multiple courses")
    p.add_argument("--courses", nargs="+", required=True, help="Canvas course IDs to post to")
    p.add_argument("--title", required=True)
    msg_group = p.add_mutually_exclusive_group(required=True)
    msg_group.add_argument("--message", help="Announcement HTML body, given directly")
    msg_group.add_argument("--file", help="Path to a file containing the HTML body")
    p.add_argument("--delayed-post-at", help="ISO8601 timestamp to schedule instead of posting immediately")
    p.add_argument("--unpublished", action="store_true", help="Save as draft instead of publishing")
    p.add_argument("--force", action="store_true", help="Update even if a same-titled announcement already exists")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true", help="Print each course's result as it happens instead of a progress bar")
    args = p.parse_args(argv)

    message = open(args.file).read() if args.file else args.message
    message = clean_html(message)

    c = CanvasClient()
    progress = Progress(len(args.courses), "courses", verbose=args.verbose)
    for course_id in args.courses:
        progress.step(str(course_id))
        try:
            post_announcement(
                c,
                course_id,
                args.title,
                message,
                published=not args.unpublished,
                delayed_post_at=args.delayed_post_at,
                force=args.force,
                dry_run=args.dry_run,
                verbose=args.verbose,
            )
        except CanvasError as e:
            print(f"course {course_id}: ERROR — {e}")
    progress.done()


if __name__ == "__main__":
    main()
