#!/usr/bin/env python3
"""Standalone WCAG-style lint for the heading/table-header patterns UDOIT
flags most often on this course's pages ("Page Headings", "Styled
Headings", "Page Structure", "Table Headers" in UDOIT's report). Not a
replacement for UDOIT — it only catches these specific, mechanically
fixable patterns locally, before a page ever gets uploaded:

  - a bare bold paragraph used in place of a real heading, either as a
    "titled list" (a <p><strong>Title</strong></p> immediately followed by
    a sub-list, both wrapped in one <li>) or standalone
  - heading levels that skip (e.g. a page starting at <h3> with no <h2>
    above it, or jumping from <h2> straight to <h4>)
  - a table's first row using <td> for what are clearly header cells
    (each cell is just bold text) instead of <th scope="col">

Run directly against an exported pages.yaml: reports issues by default,
or rewrites the file in place with `--fix` (after writing a .bak copy).
"""
import argparse
import sys

import yaml
from bs4 import BeautifulSoup, NavigableString

from canvas_tools.export_course import LiteralStr, dump_data

_HEADING_NAMES = ["h1", "h2", "h3", "h4", "h5", "h6"]
_MAX_STANDALONE_HEADING_LEN = 100


def _significant_children(tag):
    return [c for c in tag.contents if not (isinstance(c, NavigableString) and not c.strip())]


def _is_titled_li(li):
    """A <li> whose entire content is a bold-only title paragraph followed
    by exactly one sub-list — the "titled list" pattern used throughout
    these courses in place of a real heading + list. Returns (title, sublist)
    or None."""
    kids = _significant_children(li)
    if len(kids) != 2:
        return None
    p, sub = kids
    if getattr(p, "name", None) != "p" or getattr(sub, "name", None) not in ("ul", "ol"):
        return None
    p_kids = _significant_children(p)
    if len(p_kids) != 1 or p_kids[0].name != "strong":
        return None
    title = p_kids[0].get_text().strip()
    if not title:
        return None
    return title, p, sub


def _is_titled_list(tag):
    if getattr(tag, "name", None) not in ("ul", "ol"):
        return False
    lis = tag.find_all("li", recursive=False)
    if not lis:
        return False
    return all(_is_titled_li(li) is not None for li in lis)


def _level_after_preceding_heading(tag):
    prev = tag.find_previous(_HEADING_NAMES)
    if prev is None:
        return 2
    return min(6, int(prev.name[1]) + 1)


def _convert_titled_list(soup, ul, base_level, fixes, fix, claimed, mutated):
    for li in ul.find_all("li", recursive=False):
        title, p, sub = _is_titled_li(li)
        claimed.add(id(p))
        level = min(6, base_level)
        fixes.append(f"bold list-item title {title!r} -> h{level}")
        if fix:
            h = soup.new_tag(f"h{level}")
            h.string = title
            li.insert_before(h)
            sub.extract()
            h.insert_after(sub)
            li.decompose()
            mutated.append(True)
            if _is_titled_list(sub):
                _convert_titled_list(soup, sub, base_level + 1, fixes, fix, claimed, mutated)
    if fix:
        ul.unwrap()


def _find_and_convert_titled_lists(soup, fixes, fix, mutated):
    """Returns the set of title <p> tag ids consumed here, so the
    standalone-bold-paragraph pass doesn't also flag them — needed even
    when fix=False, since a report-only run never removes those <p> tags
    from the tree for the later pass to skip naturally."""
    candidates = [ul for ul in soup.find_all(["ul", "ol"]) if _is_titled_list(ul)]
    nested_ids = set()
    for ul in candidates:
        for li in ul.find_all("li", recursive=False):
            _, _, sub = _is_titled_li(li)
            nested_ids.add(id(sub))
    top_level = [ul for ul in candidates if id(ul) not in nested_ids]
    claimed = set()
    for ul in top_level:
        base_level = _level_after_preceding_heading(ul)
        _convert_titled_list(soup, ul, base_level, fixes, fix, claimed, mutated)
    return claimed


def _find_and_convert_standalone_bold_paragraphs(soup, fixes, fix, claimed, mutated):
    candidates = []
    for p in soup.find_all("p"):
        if id(p) in claimed:
            continue
        kids = _significant_children(p)
        if len(kids) == 1 and kids[0].name == "strong":
            text = kids[0].get_text().strip()
            if text:
                candidates.append((p, text))
    for p, text in candidates:
        if len(text) > _MAX_STANDALONE_HEADING_LEN:
            fixes.append(f"bold paragraph {text[:60]!r}... looks heading-like but is long — review manually")
            continue
        level = _level_after_preceding_heading(p)
        fixes.append(f"bold paragraph {text!r} -> h{level}")
        if fix:
            h = soup.new_tag(f"h{level}")
            h.string = text
            p.replace_with(h)
            mutated.append(True)


def _cell_is_bold_only(td):
    kids = _significant_children(td)
    if not kids:
        return False
    for k in kids:
        if k.name == "p":
            pk = _significant_children(k)
            if len(pk) != 1 or pk[0].name != "strong":
                return False
        elif k.name != "strong":
            return False
    return True


def _find_and_convert_table_headers(soup, fixes, fix, mutated):
    for table in soup.find_all("table"):
        first_tr = table.find("tr")
        if first_tr is None:
            continue
        tds = first_tr.find_all("td", recursive=False)
        ths = first_tr.find_all("th", recursive=False)
        if not tds or ths:
            continue
        if not all(_cell_is_bold_only(td) for td in tds):
            continue
        fixes.append(f"table header row ({len(tds)} cells) uses <td> instead of <th>")
        if fix:
            for td in tds:
                td.name = "th"
                td["scope"] = "col"
            mutated.append(True)


def _normalize_heading_skips(soup, fixes, fix, mutated):
    prev = 1
    for h in soup.find_all(_HEADING_NAMES):
        level = int(h.name[1])
        allowed = prev + 1
        label = h.get_text(strip=True)[:50]
        if level > allowed:
            if fix:
                fixes.append(f"h{level} {label!r} skips heading levels -> demoted to h{allowed}")
                h.name = f"h{allowed}"
                mutated.append(True)
            else:
                fixes.append(f"h{level} {label!r} skips heading levels (expected at most h{allowed})")
            # Even in report-only mode, treat this heading as if corrected
            # to `allowed` for the purpose of judging the *next* heading —
            # otherwise a chain of skips gets misreported relative to the
            # original chaotic levels instead of the would-be-fixed ones.
            level = allowed
        prev = level


def check_and_fix_body(html, fix=False):
    """Returns (issues, new_html). `new_html` is None unless `fix` is True
    and an actual structural mutation happened — advisory-only findings
    (e.g. "review manually") never trigger a rewrite, since re-serializing
    an untouched page through BeautifulSoup isn't byte-identical to the
    input (attribute order, entity encoding, etc.) and would otherwise
    disturb pages we didn't actually need to touch."""
    if not html or not html.strip():
        return [], None

    soup = BeautifulSoup(html, "html.parser")
    fixes = []
    mutated = []

    # Order matters: titled lists first (so their title <p> tags don't
    # also get caught by the standalone-bold-paragraph pass), then
    # standalone bold paragraphs, then table headers, then a final
    # heading-skip cleanup pass over whatever headings now exist
    # (original + newly converted).
    claimed = _find_and_convert_titled_lists(soup, fixes, fix, mutated)
    _find_and_convert_standalone_bold_paragraphs(soup, fixes, fix, claimed, mutated)
    _find_and_convert_table_headers(soup, fixes, fix, mutated)
    _normalize_heading_skips(soup, fixes, fix, mutated)

    if not soup.find(_HEADING_NAMES) and len(soup.get_text(strip=True)) > 400:
        fixes.append("page has no headings at all and substantial content — review manually")

    new_html = str(soup) if (fix and mutated) else None
    return fixes, new_html


def check_file(path, fix=False):
    with open(path) as f:
        spec = yaml.safe_load(f)

    items = spec.get("pages", spec if isinstance(spec, list) else [])
    any_issues = False
    needs_manual_review = False
    changed = False

    for item in items:
        body = item.get("body")
        if not body:
            continue
        issues, new_body = check_and_fix_body(body, fix=fix)
        if not issues:
            continue
        any_issues = True
        if any("review manually" in issue for issue in issues):
            needs_manual_review = True
        print(f"\n{item.get('title', '(untitled)')}:")
        for issue in issues:
            print(f"  - {issue}")
        if fix and new_body is not None and new_body != body:
            item["body"] = new_body
            changed = True

    if fix and changed:
        # Every body — changed or not — needs to go back out as a
        # LiteralStr, since yaml.safe_load() always hands back plain str
        # regardless of how it was originally written; dump_data()'s
        # block-style representer only fires for LiteralStr. Skipping the
        # unchanged ones would silently flatten their `|-` block style
        # into a folded quoted string on rewrite.
        for item in items:
            if isinstance(item.get("body"), str):
                item["body"] = LiteralStr(item["body"])

        backup = path + ".bak"
        with open(path) as f:
            original = f.read()
        with open(backup, "w") as f:
            f.write(original)
        dump_data(spec, path)
        print(f"\nFixed. Original backed up to {backup}")
    elif not any_issues:
        print("No issues found.")

    return needs_manual_review if fix else any_issues


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", help="Path to a pages YAML file (matches `canvas pages apply` schema)")
    parser.add_argument("--fix", action="store_true", help="Rewrite the file in place (writes a .bak backup first)")
    args = parser.parse_args(argv)

    # Without --fix: exit 1 if anything was found (report mode). With
    # --fix: exit 1 only if something remains that needs manual review —
    # everything auto-fixable was already fixed, so a clean --fix run
    # exits 0 even though issues were printed along the way.
    needs_attention = check_file(args.file, fix=args.fix)
    if needs_attention:
        sys.exit(1)


if __name__ == "__main__":
    main()
