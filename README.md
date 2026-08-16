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

### Install into a virtual environment

The default, recommended way to set this up — keeps this project's
dependencies isolated from everything else on your system, whether that's
your own machine or anyone else's who picks this up:

```
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
```

That last command reads `pyproject.toml` and installs everything this
project needs (`requests`, `pyyaml`, `python-dotenv`, `beautifulsoup4`) —
and also registers every shortcut command (`agroups`, `acleanup`, `exco`,
etc.) on your `PATH`, runnable bare from any directory without the
`python3 -m canvas_tools.cli` prefix. See `shortcuts.md` for the full list.

The venv only needs creating once; after that, `source .venv/bin/activate`
(run from the project root, or give it the full path from anywhere) is
what you need at the start of each new shell session before using any of
these commands. You'll know it's active when your prompt shows `(.venv)`.

## How to Use

With the virtual environment active (see Install, above), every command in
this doc can be run either as its shortcut (`agroups ...`, `acleanup ...`,
see `shortcuts.md`) or spelled out in full — both work from any directory,
not just the project root, since `pip install -e .` is what makes
`canvas_tools` importable in the first place:

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

### Export an existing course from Canvas to local files

Pulls a live course's rubrics, assignment groups, assignments, pages,
announcements, and modules and writes them out as `rubrics/<title>.csv`
(one file per rubric) plus, for every other resource, **both**
`assignment_groups.yaml`/`.json`, `assignments.yaml`/`.json`,
`pages.yaml`/`.json`, `announcements.yaml`/`.json`, and
`modules.yaml`/`.json` — in the same schema their respective
`apply`/`import`/`update` commands expect. Useful for seeing how a real
built course maps to these tools, or as a starting template for a new
course. The files round-trip: applying either format back to their source
course is a no-op. The rubric CSVs round-trip through
`rubrics update`, not `rubrics import` — re-importing one would create a
new rubric (or an auto-renamed one, see below) rather than updating the
original; they're exported for reference, for editing via `rubrics
update`, or to import fresh into a _different_ course.

The no-blind-overwrites `[Y]es/[N]o/[A]rchive` prompt (see below) applies
to every file this writes, rubric CSVs included — one prompt per file,
same as `rubrics export`.

```
python3 -m canvas_tools.export_course --course 10001 --out exports
python3 -m canvas_tools.export_course --course 10001 10002 10003 --out exports
python3 -m canvas_tools.export_course --all --out exports
python3 -m canvas_tools.export_course --all --match "26/FA" --out exports
python3 -m canvas_tools.export_course --course 10001 --out exports --format json
python3 -m canvas_tools.export_course --course 10001 --out exports --format yaml
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

`--out` is the _parent_ directory — each course's own subfolder is created
underneath it automatically, named `course_<id>_<course code>` (e.g.
`course_10001_26-FA_XXX-100-OL01`), not just the bare id. A course code
alone isn't memorable months later across a dozen sections; the sanitized
code (Canvas course codes look like `26/FA XXX-100-OL01` — `/` isn't valid
in a directory name, so `/` becomes `-` and spaces become `_`) makes it
obvious which folder is which course without having to look it up.

Left out, `--out` defaults to _this project's own_ `exports/` folder —
resolved from where the code itself lives on disk, not your current
directory, so running this from inside a course's own exports subfolder
still lands in the right place instead of creating a stray nested
`exports/exports/...` right there (confirmed the hard way). Give `--out`
explicitly and it resolves normally, relative to wherever you actually
are, same as `--file` always has.

**Left out, `--format` writes both `.yaml` and `.json` for every
resource** — one fetch from Canvas, two files each, so you always have
both on disk without re-running the export. Give `--format yaml` or
`--format json` to write only that one instead (rubrics stay CSV either
way regardless of `--format` — that format is dictated by Canvas's own
rubric import endpoint, not a style choice). Every `apply` command already
reads either format with no changes needed — YAML is a syntactic superset
of JSON, so `yaml.safe_load()` parses a plain JSON file correctly
(confirmed directly, not assumed) — so a hand-written `.json` file works
today even without ever running `--format json`. The one thing JSON
loses: no comments, and multi-line HTML shows up as one line with escaped
`\n`s instead of YAML's readable block-literal style, which matters more
the more you're hand-editing descriptions/bodies. `assignments
export`/`pages export`/etc. (below) don't have a `--format` flag at all —
they always write exactly one file, in whatever format its `--out` path's
extension says.

**This overwrites every local file for the course with whatever is
currently live in Canvas** — assignment_groups.yaml/.json,
assignments.yaml/.json, pages.yaml/.json, announcements.yaml/.json,
modules.yaml/.json, and the rubric CSVs all get replaced in one pass. If
you have local edits in any of those files that
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

**Large modules are always fetched correctly, not trusted blindly.**
Canvas's module-list API silently omits a module's items once it has
enough of them (confirmed live against a 223-item module — the response
had `items_count: 223` but no `items` field at all, not even a partial
list) instead of erroring or truncating visibly. `modules export`
cross-checks every module's item count against what actually came back
and transparently re-fetches from that module's own items endpoint
whenever they don't match, so a big module never exports as falsely
empty. This matters beyond the export file being wrong: `modules apply`
deletes any item not listed under its module, so a falsely-empty export
would have deleted everything in that module the next time it was
applied.

Each overwrites only the one file it targets.

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

### Keeping local files in sync after apply/delete

Every `apply` and `delete` command — for every resource type — ends by
offering to immediately re-export Canvas's live state back to the
course's conventional file(s) for that resource:

```
Re-export course 10001's current state to 'exports/course_10001_.../assignments.yaml' and 'exports/course_10001_.../assignments.json'? [Y]es/[N]o/[A]rchive:
```

The target is always the file(s) `{kind} export` would normally write —
`assignments.yaml`/`.json`, `assignment_groups.yaml`/`.json`,
`pages.yaml`/`.json`, `announcements.yaml`/`.json`, or
`modules.yaml`/`.json` — in whatever directory `--file` lives in. It's
deliberately **not** just whatever `--file` happened to be named: applying
from a scratch/edit copy like `assignments_new.yaml` still resyncs the
course's real `assignments.yaml`, exactly as if you'd run `assignments
export` right after — not the scratch file itself.

If **both** the `.yaml` and `.json` copy already exist alongside `--file`,
one shared decision resyncs both together — editing one format by hand and
letting the other silently drift is exactly the inconsistency this exists
to prevent. If only one of the two exists, only that one is touched; this
never starts a second format that was never in use. If neither exists yet,
it falls back to a plain `[y/N]` prompt and creates a fresh `.yaml`, same
as every other export in this tool.

`--dry-run` never asks at all, since nothing changed to sync. Otherwise,
answering the prompt means:

- **Y**es — overwrite the file(s) in place.
- **N**o (or anything else) — leave every target untouched.
- **A**rchive — move the existing file(s) into an `archive/` subfolder next
  to them first (created if it doesn't exist yet), renamed with a
  timestamp reflecting your own locale's date order (e.g.
  `modules_2026-08-16_14-30-05.yaml`, or day-before-month for a locale that
  conventionally writes dates that way) — then write the fresh export in
  their place.

This exists because that file can silently drift out of sync with
anything changed outside it — directly in the Canvas UI, or by a separate
`apply`/`delete` run — and a later `apply` against the stale copy can undo
those changes, e.g. by recreating something that was just deleted.
Answering `y` (or `a`) closes that gap immediately instead of relying on a
manual `export` afterward, without silently clobbering whatever was there
before if you'd rather keep it.

This same **no-blind-overwrites** protection — the `[Y]es/[N]o/[A]rchive`
prompt, only asked when the target file already exists — applies to every
`export` command (`assignments export --out ...`, `modules export`, etc.)
and to a full `export_course` run as well, one prompt per file it's about
to write over.

A full `export_course` run can hit that prompt a dozen-plus times in a row
(five resources × two formats, plus one per rubric) if you're re-exporting
a course that's already fully exported — answering the same question that
many times gets old fast. So the first time it asks, it asks a follow-up:
`Apply that choice to every other file in this run too? [y/N]`. Say yes and
whichever you picked (overwrite/skip/archive) is applied silently to every
remaining file for the rest of that run — across every course too, if
you're exporting more than one in the same `--all`/`--course A B C` call.
Say no (or anything else) and it keeps asking per file, same as before.
This follow-up only appears in `export_course`; a single-file `export`
command never asks it, since there'd be nothing else in that run to apply
it to.

**The "Archive" choice never deletes anything on its own** — the old
copies just pile up in `archive/` subfolders over time. To actually clear
those out:

```
python3 -m canvas_tools.cli archive cleanup --dry-run
python3 -m canvas_tools.cli archive cleanup
```

Each course has exactly one `archive/` folder, directly inside its own
export directory — everything gets archived there, rubric CSVs included
(their `rubrics/` subfolder doesn't get its own separate archive). `--path`
(default: the current directory) is searched recursively for every
directory named `archive` — run it from inside one course's export folder
and only that course's archive is in scope; run it from the top of
`exports/` and every course's is. If more than one turns up, an interactive
checklist opens before anything else happens:

```
Found 2 archive folders (↑/↓ move, space toggle, a=all, n=none, enter=confirm):
> [ ] course_10001_26-FA_XXX-100-OL01/archive (10 file(s))
  [*] course_10002_26-FA_XXX-200-OL01/archive (21 file(s))
  [A]ll
  [N]one
```

↑/↓ moves the highlighted row, space toggles it, `a`/`n` check/uncheck
everything at once, and Enter confirms whatever's checked — Escape cancels
outright. Running the default `--path` for real should never accidentally
wipe out a different course's archives than the one you meant, and this is
what stands between that and a single unchecked "yes." (This checklist
needs a real terminal; piped/non-interactive input falls back to a plain
numbered prompt instead.)

**If you checked more than one folder**, it asks one more thing first:
apply the same cleanup choice to every folder you picked, or choose
separately for each one (`For course_10002_..., what would you like to
clean up?`, once per folder, before anything is deleted). This is skipped
entirely — straight to the menu below — when only one folder was selected,
either because there was only one to begin with or because you checked
just one from the list. It then asks how to clean up what's in the
folder(s) in question:

- **[A]ll** — delete every archived file.
- **[M]ost recent** — for each original file, keep only its newest
  archived copy and delete the rest (so you always have one fallback per
  file, not the whole history).
- **[T]ime** — delete archived files older than a chosen cutoff: last day,
  week, month, or year. ("Older than" — a week means keep the last week,
  delete anything archived before that.)
- **[C]ancel** — leave everything alone.

Either way, it prints the full list of files that would be deleted and
requires typing `yes` before actually removing anything (`--dry-run` stops
right after printing that list). This is a plain local file cleanup — it
never touches Canvas.

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

### Delete assignment groups

`apply`, above, never deletes anything — leaving a group out of the file
just leaves it alone in Canvas. To actually remove a group, use `delete`
with its own file (see `examples/assignment_groups_delete.example.yaml`):

```yaml
assignment_groups:
  - name: "Old Homework"
    move_assignments_to: "Homework"   # relocate its assignments first

  - name: "Empty Leftover Group"      # nothing in it — nothing else needed

  - name: "Scrapped Unit"
    delete_assignments: true          # delete its assignments too, permanently
```

```
python3 -m canvas_tools.cli assignment_groups delete --course 10001 --file my_groups_delete.yaml --dry-run
python3 -m canvas_tools.cli assignment_groups delete --course 10001 --file my_groups_delete.yaml
```

Every group named in the file is checked against Canvas itself — freshly
fetched, not a local export — before anything happens, so a stale file or a
typo'd name fails loudly instead of silently doing nothing. If a group
still has assignments in it, something has to say what happens to them —
Canvas itself deletes a group's assignments if you don't tell it to move
them elsewhere, so this tool refuses to guess. Setting both
`move_assignments_to` and `delete_assignments: true` on the same entry is
always a file error, regardless of `--dry-run`.

Leaving **both** out for a non-empty group isn't a file error, though —
it's resolved differently depending on how you're running it:
- **Real run:** asked right there at the terminal — move to another group
  (picked from a numbered list of the course's other groups, not typed by
  hand), delete the assignments along with the group, or skip this one
  group and leave it alone.
- **`--dry-run`:** nothing to ask, since dry-run never touches the
  keyboard — it prints an `UNRESOLVED` note for that group and leaves it
  out of the previewed plan, rather than failing the whole preview.

**Two separate confirmations, not --dry-run gated.** Even without
`--dry-run`, the real run always prints the full plan and requires typing
`yes` before touching anything. If any group is set to `delete_assignments:
true` and actually has assignments in it, there's a second, separate `yes`
prompt afterward naming every assignment that's about to be permanently
deleted — a group-only "yes" can't accidentally take assignments with it.

**A third gate if the file happens to name every group that currently
exists.** This is the same `delete` command for every resource type in
this tool (assignment groups, assignments, pages, announcements — see
below): if what's being deleted matches the *entire* live set — easy to
do by accident if a full export gets pointed at `delete` instead of a
curated subset — typing `yes` isn't enough. It prints an explicit warning
that nothing of that type would be left, and requires typing the course
ID itself to proceed, so it can't be clicked through on autopilot the way
a habitual "yes" can.

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

### Delete announcements

`apply`, above, never deletes anything — leaving an announcement out of
the file just leaves it alone in Canvas. To actually remove one, use
`delete` with its own file (see
`examples/announcements_delete.example.yaml`) — a flat list of titles:

```yaml
announcements:
  - "Old Reminder"
  - "Outdated Notice"
```

```
python3 -m canvas_tools.cli announcements delete --course 10001 --file my_announcements_delete.yaml --dry-run
python3 -m canvas_tools.cli announcements delete --course 10001 --file my_announcements_delete.yaml
```

Every title is checked against Canvas itself — freshly fetched, not a
local export — before anything happens, so a stale file or a typo'd title
fails loudly instead of silently doing nothing. Even without `--dry-run`,
the real run always prints the full plan and requires typing `yes` before
deleting anything. If the file happens to name _every_ announcement
currently in the course, there's a further gate requiring you to type the
course ID itself — see the same escalation under **Delete assignment
groups**, above.

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
import one first with `rubrics import`, see above) to the assignment for
grading; pair with `use_rubric_for_grading: true` if the rubric's score
should set the grade rather than being purely for feedback.

`remove_rubric: true` detaches whatever rubric is currently on the
assignment — a distinct field rather than blanking `rubric:`, since blank
means "don't touch" everywhere in this tool and detaching needs to be an
explicit choice. **Don't use this to edit an existing rubric's content —
use** **`rubrics update`** **instead, with the instructions above.** `remove_rubric` here is for the narrower case of
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

To spell out the rule those examples are demonstrating: within one
assignment entry, `name` is the only field that's always required (it's
what matches the existing assignment) — every other field is optional on
update, and simply **leaving a field out of the entry entirely** is what
tells this tool to not touch it on Canvas, not to clear it. That's the
"blank means don't touch" convention this whole tool follows, and it's why
a two-line entry like the one above is enough to change just one field on
an assignment with 20+ fields set — nothing else in the file gets sent to
Canvas at all, so nothing else can be affected.

**Not supported: plagiarism review (e.g. Turnitin/Copyleaks/VeriCite).**
Verified directly against a live assignment — this setting isn't in the
Assignments API response at all, isn't in any form field, and doesn't
appear even after saving it through the Canvas UI and inspecting the
resulting network traffic. It's Canvas's newer LTI-based Plagiarism
Platform, stored outside the Assignment model entirely; it may only be
reachable via the LTI tool's own credentials, not a personal API token.
Set this one by hand in the Canvas UI.

### Delete assignments

`apply`, above, never deletes anything — leaving an assignment out of the
file just leaves it alone in Canvas. To actually remove one, use `delete`
with its own file (see `examples/assignments_delete.example.yaml`) — a
flat list of names:

```yaml
assignments:
  - "Old Homework 1"
  - "Scrapped Quiz"
```

```
python3 -m canvas_tools.cli assignments delete --course 10001 --file my_assignments_delete.yaml --dry-run
python3 -m canvas_tools.cli assignments delete --course 10001 --file my_assignments_delete.yaml
```

Every name is checked against Canvas itself — freshly fetched, not a local
export — before anything happens, so a stale file or a typo'd name fails
loudly instead of silently doing nothing. Even without `--dry-run`, the
real run always prints the full plan and requires typing `yes` before
deleting anything. If the file happens to name _every_ assignment
currently in the course, there's a further gate requiring you to type the
course ID itself — see the same escalation under **Delete assignment
groups**, above.

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

### Delete pages

`apply`, above, never deletes anything — leaving a page out of the file
just leaves it alone in Canvas. To actually remove one, use `delete` with
its own file (see `examples/pages_delete.example.yaml`) — a flat list of
titles:

```yaml
pages:
  - "Old Syllabus Draft"
  - "Scrapped Page"
```

```
python3 -m canvas_tools.cli pages delete --course 10001 --file my_pages_delete.yaml --dry-run
python3 -m canvas_tools.cli pages delete --course 10001 --file my_pages_delete.yaml
```

Every title is checked against Canvas itself — freshly fetched, not a
local export — before anything happens, so a stale file or a typo'd title
fails loudly instead of silently doing nothing. Even without `--dry-run`,
the real run always prints the full plan and requires typing `yes` before
deleting anything. If the file happens to name _every_ page currently in
the course, there's a further gate requiring you to type the course ID
itself — see the same escalation under **Delete assignment groups**,
above.

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

A routine partial edit (dropping a module or two while restructuring)
stays frictionless — no confirmation prompt, same as it's always worked.
But if the file happens to share **no** module names with what's
currently in the course at all — wrong file, wrong `--course`, or a
near-empty file applied by mistake — every existing module would be
deleted in one shot. That case gets the same course-id escalation as the
dedicated `delete` commands (see **Delete assignment groups**, above)
before anything happens, including creating the file's own modules.

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

The order modules appear in the file is the order they end up in on
Canvas — every apply sends each module's position as its row number in
the file, so reordering rows and re-applying reorders the course.
(Without this, any settings update to a module that didn't also restate
its position could cause Canvas to silently drop it to the bottom of the
list — confirmed live.)

Renaming a module in the file (just editing its `name:`) is safe: add a
`rename_from:` with the module's _old_ name and the apply matches it to the
existing Canvas module and renames it in place via a single update, instead
of deleting the old name and creating a new one under the new name. Without
`rename_from`, a plain rename looks like "delete + create" to `modules
apply` — the module gets recreated with a new id, all its items get
recreated fresh from the file, and (per the position note above) it can only
land wherever the create endpoint puts it before the next apply's position
pass corrects it:

```yaml
modules:
  - name: "Week 7 - Working with Microsoft File Systems, the Registry, and More"
    rename_from: "Week 7 - Working with Microsoft File Systems and the Windows Registry"
```

`rename_from` is only read on the apply that performs the rename — drop it
from the file afterward (the next export won't include it anyway).

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
