import sys

_BAR_WIDTH = 30


class Progress:
    """A simple overwriting progress bar for a loop of known length.

    Default (verbose=False): draws `[#####-----] 6/16 label: detail`,
    redrawn in place via \\r, so a long-running command (rubric updates
    across many assignments, a full module sync, etc.) doesn't look hung
    with no output for the API-latency duration of each step.

    verbose=True: draws nothing — the caller is expected to print its own
    per-item detail lines instead, same as before this existed.

    Non-interactive stdout (piped/redirected) falls back to occasional
    plain-text progress lines instead of \\r-redraws, since carriage
    returns just clutter a log file or captured output.
    """

    def __init__(self, total, label, verbose=False, file=None):
        self.total = total
        self.label = label
        self.verbose = verbose
        self.count = 0
        self.file = file or sys.stdout
        self.interactive = self.file.isatty()
        self._last_plain_pct = -1

    def step(self, detail=""):
        self.count += 1
        if self.verbose or self.total == 0:
            return
        if self.interactive:
            self._draw(detail)
        else:
            self._draw_plain()

    def _draw(self, detail):
        filled = int(_BAR_WIDTH * self.count / self.total)
        bar = "#" * filled + "-" * (_BAR_WIDTH - filled)
        text = f"\r[{bar}] {self.count}/{self.total} {self.label}"
        if detail:
            text += f": {detail}"
        pad = 100 - len(text)
        if pad > 0:
            text += " " * pad
        else:
            text = text[:100]
        self.file.write(text)
        self.file.flush()

    def _draw_plain(self):
        pct = int(100 * self.count / self.total)
        # only print every 10%, plus the final step, so a redirected log
        # doesn't get a line per item
        if pct // 10 == self._last_plain_pct // 10 and self.count != self.total:
            return
        self._last_plain_pct = pct
        self.file.write(f"{self.label}: {self.count}/{self.total} ({pct}%)\n")
        self.file.flush()

    def done(self):
        if not self.verbose and self.total and self.interactive:
            self.file.write("\n")
            self.file.flush()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.done()
        return False
