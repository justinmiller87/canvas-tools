import re

from bs4 import BeautifulSoup

_JUNK_TOKEN_RE = re.compile(
    r"^(font-claude-response-body|flex(-\w+)?|gap-\d+|list-(disc|decimal)|"
    r"whitespace-\w+|break-\w+|[mp][lrtbxy]?-\d+)$"
)


def _is_junk_class(token):
    return ":" in token or token.startswith("[") or bool(_JUNK_TOKEN_RE.match(token))


def clean_html(html):
    """Strip markup cruft left over from pasting AI chat responses straight into
    Canvas's rich text editor: chat-UI utility classes (e.g. font-claude-response-body,
    Tailwind-style tokens), data-sourcepos (markdown source-map attribute), and
    redundant dir="ltr". Leaves real content and any other attributes untouched.
    """
    if not html:
        return html

    soup = BeautifulSoup(html, "html.parser")
    changed = False

    for tag in soup.find_all(True):
        if tag.has_attr("data-sourcepos"):
            del tag["data-sourcepos"]
            changed = True
        if tag.get("dir") == "ltr":
            del tag["dir"]
            changed = True
        if tag.has_attr("class"):
            kept = [c for c in tag["class"] if not _is_junk_class(c)]
            if kept != tag["class"]:
                changed = True
            if kept:
                tag["class"] = kept
            else:
                del tag["class"]

    if not changed:
        return html
    return str(soup)
