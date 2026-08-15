# Canvas API Tools

Lightweight CLI for common course-setup tasks against your Canvas
instance, using the Canvas REST API directly.

## Setup

### Instructions to get an API Token:

1. Open your Canvas instance in a browser
2. Click account
3. Click settings
4. Scroll to the bottom of "Approved Integrations"
5. Click "+ New Access Token"
6. Add your purpose reason, and set your expiration date and time
7. Click Generate Token

### Adding the Token to the Script:

1. Copy the token given to you
2. Open the file .env.example
3. Update the `CANVAS_API_URL` field with the URL for your school's Canvas instance.
4. Paste the API token you just generated in the `CANVAS_API_TOKEN` field as-is. Do not encapsulate it in quotes.
5. Save this file as .env

Note: The .env file must be named exactly. If you have forked this, the .gitignore file will not publish your private credentials if you push to a public repo.

### Dependencies

Dependencies (`requests`, `pyyaml`, `python-dotenv`) are already installed
in this environment.

## How to Use

Run everything from this directory as a module:

```
python3 -m canvas_tools.cli <command> ...
```

Nothing in this toolkit is Linux-specific — it's plain Python with no
shell-outs and no OS-specific paths, so it runs the same on macOS/Windows.
The one thing to adjust is the command name itself: every example here
uses `python3`, but a standard Windows Python install usually only
provides `python` (or the `py` launcher) — substitute accordingly.

## Commands

### List your courses

```
python3 -m canvas_tools.cli courses list
python3 -m canvas_tools.cli courses list --state unpublished
```

Prints `id  course_code  name` — grab the ID for use in other commands.

### Order of operations, when creating new content

If everything you're applying already exists (you're just editing content),
order doesn't matter — each command matches by name/title against whatever's
currently live. It only matters when a file references something *by name*
that doesn't exist in Canvas yet, since that lookup happens at apply-time,
not after everything finishes:

1. **Rubrics and assignment groups** first — an assignment's `rubric:` and
   `assignment_group:` fields each have to match something that already
   exists in the course; neither gets created automatically by
   `assignments apply`.
2. **Assignments and pages** next — `modules apply`'s `Assignment`/`Page`
   items are matched by title/`page_url` against existing content; they
   don't create it. Assignments and pages don't depend on each other, so
   their order relative to one another doesn't matter.
3. **Modules last** — not because anything strictly requires it, but because
   `modules apply` is the one destructive, full-sync command (see below), so
   running it after everything else has settled avoids syncing against
   content that's still about to change.

**Announcements aren't part of this chain at all.** A module item can only
be `ExternalUrl`, `SubHeader`, `Page`, `Assignment`, `Quiz`, `Discussion`,
`File`, or `ExternalTool` — there's no way to link an announcement into a
module, so announcements have no ordering dependency with anything and can
go whenever.

### Export an existing course from Canvas to local files

Pulls a live course's rubrics, assignment groups, assignments, pages,
announcements, and modules and writes them out as `rubrics/<title>.csv`
(one file per rubric) / `assignment_groups.yaml` / `assignments.yaml` /
`pages.yaml` / `announcements.yaml` / `modules.yaml` in the same schema
their respective `apply`/`import`/`update` commands expect. Useful for
seeing how a real built course maps to these tools, or as a starting
template for a new course. The YAML files round-trip: applying them back
to their source course is a no-op. The rubric CSVs round-trip through
`rubrics update`, not `rubrics import` — re-importing one would create a
new rubric (or an auto-renamed one, see above) rather than updating the
original; they're exported for reference, for editing via `rubrics
update`, or to import fresh into a _different_ course.

```
python3 -m canvas_tools.export_course --course 10001 --out exports
python3 -m canvas_tools.export_course --course 10001 10002 10003 --out exports
python3 -m canvas_tools.export_course --all --out exports
python3 -m canvas_tools.export_course --all --match "26/FA" --out exports
python3 -m canvas_tools.export_course --course 10001 --out exports --format json
```

`--course` takes one or more course IDs — each gets exported in turn, its
own subfolder, in a single run. `--all` exports every course you teach
(anything Canvas returns as `state: available` for your teacher
enrollments) instead of listing IDs by hand — useful for an initial sync
onto a new machine. `--course` and `--all` are mutually exclusive. In a
multi-course run, one course failing (e.g. a bad ID) prints an error and
continues on to the rest rather than aborting the whole run; a summary of
how many succeeded prints at the end.

`--match TEXT` (only with `--all`) narrows that to courses whose code or
name contains `TEXT`, case-insensitive — e.g. only exporting one term.
It's a plain substring match against whatever your school's actual course
codes/names contain, not tied to any particular format — some schools'
codes look like `26/FA XXX-100-OL01`, in which case `--match "26/FA"`
grabs just that term, but the match itself doesn't assume that shape.

`--out` is the _parent_ directory (default: `exports`) — each course's own
subfolder is created underneath it automatically, named
`course_<id>_<course code>` (e.g. `course_10001_26-FA_XXX-100-OL01`), not
just the bare id. A course code alone isn't memorable months later across
a dozen sections; the sanitized code (Canvas course codes look like
`26/FA XXX-100-OL01` — `/` isn't valid in a directory name, so `/` becomes
`-` and spaces become `_`) makes it obvious which folder is which course
without having to look it up.

`--format json` writes `.json` files instead of `.yaml` (rubrics stay CSV
either way — that format is dictated by Canvas's own rubric import
endpoint, not a style choice). Every `apply` command already reads either
format with no changes needed — YAML is a syntactic superset of JSON, so
`yaml.safe_load()` parses a plain JSON file correctly (confirmed directly,
not assumed) — so a hand-written `.json` file works today even without
`--format json`. The one thing JSON loses: no comments, and multi-line
HTML shows up as one line with escaped `\n`s instead of YAML's readable
block-literal style, which matters more the more you're hand-editing
descriptions/bodies. `assignments export`/`pages export`/etc. (below) also
switch to JSON if you give them an `--out` path ending in `.json`.

**This overwrites every local file for the course with whatever is
currently live in Canvas** — assignment_groups.yaml, assignments.yaml,
pages.yaml, announcements.yaml, modules.yaml, and the rubric CSVs all get
replaced in one pass. If you have local edits in any of those files that
haven't been pushed to Canvas yet (`apply`'d) or committed, running this
discards them silently — confirmed the hard way earlier in this project's
history. Push or commit pending edits _before_ pulling a fresh export, not
after.

If you only need to refresh one file — say you fixed a single assignment
by hand in the Canvas UI and don't want to touch pages/modules/
announcements/rubrics at all — each resource has its own `export`
subcommand instead. These take a literal file path (not a parent
directory), so point them at the file inside the course's existing folder:

```
python3 -m canvas_tools.cli assignment_groups export --course 10001 --out "exports/course_10001_26-FA_XXX-100-OL01/assignment_groups.yaml"
python3 -m canvas_tools.cli assignments      export --course 10001 --out "exports/course_10001_26-FA_XXX-100-OL01/assignments.yaml"
python3 -m canvas_tools.cli pages            export --course 10001 --out "exports/course_10001_26-FA_XXX-100-OL01/pages.yaml"
python3 -m canvas_tools.cli announcements    export --course 10001 --out "exports/course_10001_26-FA_XXX-100-OL01/announcements.yaml"
python3 -m canvas_tools.cli modules          export --course 10001 --out "exports/course_10001_26-FA_XXX-100-OL01/modules.yaml"
```

Each overwrites only the one file it targets.

### Create/update assignment groups

Define assignment groups in a YAML file. Matched by exact name
(case-insensitive): existing groups are updated, new ones are created.
Fields: `name` (required), `position` (optional — display order), and
`group_weight` (optional — only meaningful if the course uses weighted
assignment groups for grading). Create these _before_ referencing them in
an `assignments apply` file — `assignment_group:` there matches by name
against existing groups, it doesn't create one.

```yaml
assignment_groups:
  - name: "Discussion Assignments"
    position: 1
    group_weight: 20.0
```

```
python3 -m canvas_tools.cli assignment_groups apply --course 10001 --file my_groups.yaml --dry-run
python3 -m canvas_tools.cli assignment_groups apply --course 10001 --file my_groups.yaml
```

### Bulk create/update assignments

Define assignments in a YAML file (see `examples/assignments.example.yaml`
— or `examples/assignments.example.json` for the same thing as JSON, above
— or `examples/master.example.yaml` for every field with REQUIRED/optional
notes). Any field accepted by the Canvas Assignments API can be used
(`points_possible`, `due_at`, `description`, `submission_types`,
`allowed_extensions`, `allowed_attempts`, `omit_from_final_grade`,
`peer_reviews` and friends, `published`, etc. — not whitelisted, so
anything Canvas's Assignments API takes will pass through even if not
listed here). Matching for create-vs-update is by exact assignment name
(case-insensitive) within the target course.

Two fields are names, not raw Canvas IDs, and get resolved automatically:
`assignment_group` (matched against the course's assignment groups —
create one first with `assignment_groups apply`, above, if it doesn't
exist yet) and `group_category` (matched against the course's group sets,
for group assignments — pair with `grade_group_students_individually`).
Both must already exist in the course.

`rubric: "Rubric Title"` attaches an existing rubric (matched by title —
import one first with `rubrics import`, see below) to the assignment for
grading; pair with `use_rubric_for_grading: true` if the rubric's score
should set the grade rather than being purely for feedback.

`remove_rubric: true` detaches whatever rubric is currently on the
assignment — a distinct field rather than blanking `rubric:`, since blank
means "don't touch" everywhere in this tool and detaching needs to be an
explicit choice. **Don't use this to edit an existing rubric's content —
use** **`rubrics update`** **instead, with the instructions below.** `remove_rubric` here is for the narrower case of
detaching a rubric from one assignment and _not_ replacing it.

**Checkpointed discussions** (two required submissions — an initial post
plus required replies, each with its own due date/points) are handled
through `checkpoints:`, a list of `{tag, points_possible, due_at,
replies_required}` entries (`tag` is `reply_to_topic` or `reply_to_entry`).
Canvas's REST API can't read or write checkpoint data at all — this goes
through the same GraphQL mutations the redesigned Discussions UI uses
internally, confirmed via schema introspection against a live instance.
Updating an existing checkpointed discussion's dates/points works like any
other field (matched by name, blank checkpoint fields fall back to the
current live value); creating a brand-new one needs both checkpoints given
in full, since there's no live checkpoint yet to fall back to. Either way,
`due_at`/`unlock_at`/`lock_at` on the assignment entry itself are ignored —
Canvas rejects any date/availability field on a checkpointed discussion's
parent assignment (confirmed live, not documented anywhere); all of that
lives on the checkpoints. See `examples/master.example.yaml` for the full
field reference and both the update and create shapes.

```
python3 -m canvas_tools.cli assignments apply --course 10001 --file my_assignments.yaml --dry-run
python3 -m canvas_tools.cli assignments apply --course 10001 --file my_assignments.yaml
```

Always run with `--dry-run` first to preview what will be created/updated.

**This applies every assignment in the file, not just the ones you
changed** — `--file` doesn't have to point at the full course export,
though. For a single small change, a standalone YAML with just that one
assignment entry works the same way and avoids re-sending the other 50+
unchanged:

```yaml
# my_one_fix.yaml
assignments:
  - name: "Pre-Quiz: Chapter 3"
    points_possible: 20
```

```
python3 -m canvas_tools.cli assignments apply --course 10001 --file my_one_fix.yaml
```

The exact same fix as `my_one_fix.json` instead — same schema, same field
names, just a different file format (see `--format json` above; unlike
YAML, JSON has no comment syntax, so the filename can't be noted inline
the way it is above):

```json
{
  "assignments": [
    {
      "name": "Pre-Quiz: Chapter 3",
      "points_possible": 20
    }
  ]
}
```

```
python3 -m canvas_tools.cli assignments apply --course 10001 --file my_one_fix.json
```

**Not supported: plagiarism review (e.g. Turnitin/Copyleaks/VeriCite).**
Verified directly against a live assignment — this setting isn't in the
Assignments API response at all, isn't in any form field, and doesn't
appear even after saving it through the Canvas UI and inspecting the
resulting network traffic. It's Canvas's newer LTI-based Plagiarism
Platform, stored outside the Assignment model entirely; it may only be
reachable via the LTI tool's own credentials, not a personal API token.
Set this one by hand in the Canvas UI.

### Create/update pages

Define pages in a YAML file (see `examples/pages.example.yaml`, or
`examples/pages.example.json`). Matched by exact title (case-insensitive).
`body` is raw HTML — write it directly or paste in content from elsewhere.

```
python3 -m canvas_tools.cli pages apply --course 10001 --file my_pages.yaml --dry-run
python3 -m canvas_tools.cli pages apply --course 10001 --file my_pages.yaml
```

Create these _before_ referencing them in a `modules apply` file — `Page`
module items link to pages matched by title/`page_url`, they don't create
pages themselves.

### Create/update announcements

Define announcements in a YAML file (see
`examples/announcements.example.yaml`, or
`examples/announcements.example.json`). Matched by exact title
(case-insensitive). Set `delayed_post_at` to schedule one for later
instead of posting immediately.

```
python3 -m canvas_tools.cli announcements apply --course 10001 --file my_announcements.yaml --dry-run
python3 -m canvas_tools.cli announcements apply --course 10001 --file my_announcements.yaml
```

### Post one announcement to multiple courses

For the common case of the same announcement going out to several
sections at once — `canvas_tools/announcement.py`, a standalone tool (not
YAML-driven). Skips a course if a same-titled announcement already exists
there, so it's safe to re-run; pass `--force` to update it instead.

```
python3 -m canvas_tools.announcement --courses 10001 10002 10003 \
  --title "Midterm reminder" \
  --message "<p>The midterm opens Monday and closes Friday at 11:59 PM.</p>"

# or from a file:
python3 -m canvas_tools.announcement --courses 10001 10002 --title "..." --file message.html --dry-run
```

### Build modules and link items

**Unlike every other** **`apply`** **command in this toolkit, this one deletes.**
`modules apply` treats the file as the exact, complete set of modules and
(per module) items — not just a list of things to ensure exist. A module
that exists in Canvas but isn't in the file gets deleted outright. An item
that's in a kept module's Canvas item list but isn't listed under that
module in the file gets removed from the module. This is deliberate: a
module item is just a pointer into a module, not the underlying content —
removing one only unlinks it, it doesn't touch the page/assignment/etc.
itself, and deleting a whole module doesn't delete its former items'
content either. Still, **always run** **`--dry-run`** **first** — a stale or
incomplete file will delete real course structure, not leave it alone.
This is the opposite of `assignments`/`pages`/`announcements` apply, which
never delete anything regardless of what's left out of the file.

Define modules and their items in a YAML file (see
`examples/modules.example.yaml`, or `examples/modules.example.json`).
Modules are matched/created by name;
items are matched by title — already-present ones are left alone, missing
ones are added, and (per the above) extra ones not listed are removed.
Supported item types: `ExternalUrl`, `SubHeader`, `Page`, `Assignment`,
`Quiz`, `Discussion`, `File`, `ExternalTool`. Assignment/Quiz/Discussion
items link to _existing_ content matched by title — create that content
first (e.g. via the assignments command) if it doesn't exist yet. `Page`
and `File` items likewise link existing pages/files (by `page_url` /
title). `ExternalTool` needs a `url` (the LTI launch URL).

Modules themselves support `unlock_at`, `require_sequential_progress`,
and `prerequisites` (a list of _other module names_ in this same file —
resolved to Canvas's internal module ids automatically, including forward
references to modules later in the file). These are applied in a second
pass after every module in the file exists, so prerequisite chains always
resolve regardless of order:

```yaml
modules:
  - name: "Week 1"
    unlock_at: "2026-08-17T05:00:00Z"
  - name: "Week 2"
    unlock_at: "2026-08-24T05:00:00Z"
    prerequisites: ["Week 1"]
```

```
python3 -m canvas_tools.cli modules apply --course 10001 --file my_modules.yaml --dry-run
python3 -m canvas_tools.cli modules apply --course 10001 --file my_modules.yaml
```

### Copy content between courses

Uses Canvas's native Content Migration (course copy) — the same mechanism
as "Copy this Course" in the UI. Good for reusing a template/previous-term
course setup.

```
python3 -m canvas_tools.cli copy --source 10001 --dest 10002 --wait
```

Omit `--wait` to kick off the migration and return immediately (check
progress later in the Canvas UI under Course Settings > Content Migrations,
or by re-querying the API).

**This changes real course content — there's no dry-run for it.** Double
check `--source` and `--dest` course IDs (from `courses list`) before
running.

### Rubrics: create, export, and update (CSV)

Uses Canvas's own rubric CSV format — the same one behind "Import Rubric"
in the UI (`GET /rubrics/upload_template` for a blank template). A row is
one criterion; rows sharing a "Rubric Name" become one rubric; ratings are
repeating `Rating Name,Rating Description,Rating Points` column-triples,
as many as the widest criterion needs.

**One CSV file per rubric, not one file for the whole course.** `rubrics
export` writes a directory of `<rubric title>.csv` files rather than a
single combined file — editing one rubric means editing one small,
unambiguous file. `rubrics import` and `rubrics update` (below) each work
on one such file at a time and error out if a file has more than one
rubric in it.

```
python3 -m canvas_tools.cli rubrics export --course 10001 --out rubrics/
python3 -m canvas_tools.cli rubrics import --course 10001 --file "rubrics/New Rubric.csv" --dry-run
python3 -m canvas_tools.cli rubrics import --course 10001 --file "rubrics/New Rubric.csv"
```

Things confirmed by testing directly against a live course, not from docs
(which don't cover this well):

- **`rubrics import`** **always creates a new rubric — it never updates an
  existing one by title.** Re-importing the same file creates a second
  rubric with the same name, or — see next point — a name Canvas
  auto-generates to avoid an exact collision. Use `rubrics update`
  (below) to actually edit an existing rubric's criteria.

- **Canvas auto-renames on an exact title collision.** Every rubric has a
  `before_save` check against every other non-deleted rubric in the same
  course; if the title you're saving already exists, it silently appends
  `" (1)"`, `" (2)"`, etc. until unique — confirmed against Canvas's own
  source, and it's universal (any create or rename goes through this, not
  just CSV import). So re-importing an edited "Essay Rubric" doesn't
  produce two rubrics both named "Essay Rubric" — it produces
  "Essay Rubric" (the original, untouched) and "Essay Rubric (1)"
  (the new one), which is easy to miss if your `assignments.yaml` still
  says plain `rubric: Essay Rubric` — it'll keep matching the
  original, not the edit you just imported. Use `rubrics update` instead
  when you want to actually replace an existing rubric's content.

- **Imported rubrics land as drafts**, invisible everywhere in Canvas
  (the UI's rubric list, the UI's rubric picker when hand-editing another
  assignment, `rubrics export`) until activated — this is true of rubrics
  imported through the Canvas UI too, not just this tool; the UI's own
  "Import Rubric" flow requires a separate manual "Save Rubric" click
  (kebab menu > Edit > Save Rubric) to activate one. `rubrics import`
  **does this activation automatically** — traced the UI's own network
  request for it and replicated the same update. (Attaching a still-draft
  rubric directly to an assignment via the `rubric:` field on
  `assignments apply` does also work without activating first, for what
  it's worth — draft rubrics grade correctly once associated, they're
  just invisible until activated.)

- Do **not** try to flip a rubric to "active" via a bare workflow_state
  update yourself — confirmed destructive, it silently wipes the title
  and criteria unless the full rubric content is resent in the same call.
  (`activate_rubric()` in `canvas_tools/rubrics.py` does this correctly if
  you need it directly.)

**To actually edit an existing rubric's criteria** — the real use case
being "I need to change wording/points on a rubric that's already
attached to assignments" — use `rubrics update` instead of re-importing:

```
python3 -m canvas_tools.cli rubrics update --course 10001 --file "rubrics/Essay Rubric.csv" --dry-run
python3 -m canvas_tools.cli rubrics update --course 10001 --file "rubrics/Essay Rubric.csv"
```

Canvas has its own protection against silently rewriting grading criteria
out from under existing work: updating a rubric that's attached to _more
than one_ assignment doesn't update it — it forks a new rubric with your
edits and leaves the original (and everything attached to it) completely
untouched (confirmed against a `rubrics_controller.rb` comment, and
against a user report of the live UI behavior). A rubric attached to zero
or one assignment updates in place freely.

So `rubrics update` does the same thing you'd have to do by hand for a
multi-use rubric: detach the rubric from every assignment currently using
it (via `used_locations`), update its criteria (now safe — zero usages),
then reattach it to every assignment it came from, restoring each one's
`use_rubric_for_grading` setting exactly. Same rubric id and title
throughout — no fork, no duplicate, no `assignments.yaml` changes needed
at all. Verified live: detach → edit → reattach across three assignments
with different `use_for_grading` values, all three came back exactly as
they started except the rubric content itself.

## Notes

- `pages apply` and `assignments apply` both run HTML content (`body` /
  `description`) through `canvas_tools/html_clean.py` before sending it,
  which strips markup cruft from pasting AI chat output straight into
  Canvas's editor (chat-UI utility classes, `data-sourcepos`, redundant
  `dir="ltr"`) without touching actual content. `export_course.py` cleans
  on the way out too, so exported YAML is already clean.

- Any `apply`/`export`/`import`/`update` command that makes one Canvas API
  call per item (`assignments`/`pages`/`announcements`/`modules apply`,
  `pages export`, `rubrics export`/`import`/`update`,
  `canvas_tools.export_course`, `canvas_tools.announcement`) shows a live
  progress bar by default, so a long-running run doesn't look hung while
  it's making dozens of individual requests. Pass `--verbose` to print each
  item as it's processed instead of drawing the bar. (Export commands with
  no per-item calls — `assignments`/`announcements`/`modules export` — do
  a single bulk fetch, so there's nothing for a bar or `--verbose` to show.)

- All commands page through Canvas API pagination automatically.

- Errors from Canvas (bad IDs, permission issues, validation errors) print
  the HTTP status and response body and exit non-zero.

- `canvas_tools/client.py` has a small reusable `CanvasClient` if you want
  to script something ad hoc — `get()` auto-paginates and returns a plain
  list/dict; `post()`/`put()`/`delete()` are thin wrappers.
