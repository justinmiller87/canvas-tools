"""Thin wrappers around `canvas_tools.cli.main()`, one per short command
name — this is what `pyproject.toml`'s `[project.scripts]` points at, so
`pip install -e .` turns each into a real executable (e.g. `agroups`)
runnable bare from anywhere, without a personal shell alias or typing out
`python3 -m canvas_tools.cli assignment_groups apply` every time. Each one
just prepends its subcommand's fixed tokens onto whatever args were
actually given and hands off to the normal CLI entry point — the exact
same argument parsing, dry-run behavior, and everything else as running
that full command directly. `exco`/`annpost` don't need a wrapper here;
they map straight to `export_course.main`/`announcement.main` in
`pyproject.toml` since neither takes a fixed subcommand prefix.
"""
import sys

from canvas_tools.cli import main as _cli_main


def _run(*prefix):
    _cli_main(list(prefix) + sys.argv[1:])


def canvas():
    _run()


def agroups():
    _run("assignment_groups", "apply")


def agroupsx():
    _run("assignment_groups", "export")


def assignments():
    _run("assignments", "apply")


def assignmentsx():
    _run("assignments", "export")


def pages():
    _run("pages", "apply")


def pagesx():
    _run("pages", "export")


def announcements():
    _run("announcements", "apply")


def announcementsx():
    _run("announcements", "export")


def modules():
    _run("modules", "apply")


def modulesx():
    _run("modules", "export")


def rubex():
    _run("rubrics", "export")


def rubimp():
    _run("rubrics", "import")


def rubupd():
    _run("rubrics", "update")


def courses():
    _run("courses", "list")


def copy():
    _run("copy")


def acleanup():
    _run("archive", "cleanup")
