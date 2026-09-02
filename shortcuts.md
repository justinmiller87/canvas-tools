# Shortcut commands

`pip install -e .` from the project root (see the main `README.md`) registers
each of these as a real command on your `PATH` — no `python3 -m
canvas_tools.cli`, no per-command prefix, runnable from any directory.

Pattern: bare resource name = `apply` (the common case), `x` suffix =
`export`.

| Shortcut              | Replaces                                                                                          |
| --------------------- | ------------------------------------------------------------------------------------------------- |
| `acleanup`            | `python3 -m canvas_tools.cli archive cleanup`                                                     |
| `agroups`             | `python3 -m canvas_tools.cli assignment_groups apply`                                             |
| `agroupsx`            | `python3 -m canvas_tools.cli assignment_groups export`                                            |
| `assignments`         | `python3 -m canvas_tools.cli assignments apply`                                                   |
| `assignmentsx`        | `python3 -m canvas_tools.cli assignments export`                                                  |
| `announcements`       | `python3 -m canvas_tools.cli announcements apply`                                                 |
| `announcementsx`      | `python3 -m canvas_tools.cli announcements export`                                                |
| `annpost`             | `python3 -m canvas_tools.announcement` (multi-course announcement poster)                         |
| `canvas <subcommand>` | `python3 -m canvas_tools.cli <subcommand>` — fallback for anything without its own shortcut above |
| `copy`                | `python3 -m canvas_tools.cli copy`                                                                |
| `courses`             | `python3 -m canvas_tools.cli courses list`                                                        |
| `exco`                | `python3 -m canvas_tools.export_course` (multi/all-course export)                                 |
| `modules`             | `python3 -m canvas_tools.cli modules apply`                                                       |
| `modulesx`            | `python3 -m canvas_tools.cli modules export`                                                      |
| `pages`               | `python3 -m canvas_tools.cli pages apply`                                                         |
| `pagesx`              | `python3 -m canvas_tools.cli pages export`                                                        |
| `rubex`               | `python3 -m canvas_tools.cli rubrics export`                                                      |
| `rubimp`              | `python3 -m canvas_tools.cli rubrics import`                                                      |
| `rubupd`              | `python3 -m canvas_tools.cli rubrics update`                                                      |
| `subdl`               | `python3 -m canvas_tools.cli submissions download`                                                |
| `subx`                | `python3 -m canvas_tools.cli submissions export`                                                  |
| `subpull`             | `python3 -m canvas_tools.cli submissions pull` (subdl + subx combined into one --out directory)   |
| `subapply`            | `python3 -m canvas_tools.cli submissions apply`                                                   |

Each one is a thin wrapper defined in `canvas_tools/shortcuts.py` and
registered via `[project.scripts]` in `pyproject.toml` — it just prepends
its fixed subcommand tokens onto whatever args you give it and hands off to
the normal CLI, so it takes exactly the same flags (`--course`, `--file`,
`--dry-run`, etc.) as running the full command directly.

## Examples

```
modules --course 10001 --file my_modules.yaml --dry-run
modules --course 10001 --file my_modules.yaml

agroups --course 10001 --file my_groups.yaml --dry-run

assignmentsx --course 10001 --out "exports/course_10001_.../assignments.yaml"

rubimp --course 10001 --file "rubrics/New Rubric.csv" --dry-run

exco --course 10001 10002 10003 --out exports
exco --all --match "26/FA" --out exports

acleanup --dry-run
acleanup

annpost --courses 10001 10002 --title "Midterm reminder" --message "<p>...</p>"

courses
courses --state unpublished

subdl --course 10001 --assignment "Essay 1" --out submissions/essay1_files/
subx --course 10001 --assignment "Essay 1" --out submissions/essay1.yaml
subpull --course 10001 --assignment "Essay 1" --out submissions/essay1/
subapply --course 10001 --file submissions/essay1.yaml --dry-run
```
