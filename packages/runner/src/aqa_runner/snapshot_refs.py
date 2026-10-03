"""Pure parsing and rewriting of Playwright AI snapshot refs."""

import re
from collections.abc import Set as AbstractSet

# One line of Playwright's AI snapshot: `- ` and a key, then `:` and a value or
# children, or nothing. Playwright single-quotes a key YAML would misread, so an
# unquoted key ends at the first colon that a space or the line's end follows.
LINE = re.compile(
    r"^(?P<head> *- )?(?P<key>'(?:[^'\n]|'')*'|(?:[^:\n]|:(?=\S))*)(?P<rest>.*)$",
    re.MULTILINE,
)

# An element's own ref ends its key, followed by nothing but `[cursor=pointer]`.
# Page text can't end a key so: a name is JSON-quoted or written /like this/.
ELEMENT_REF = re.compile(r"\[ref=((?:f[0-9]+)?e[0-9]+)\]((?: \[cursor=pointer\])?)\Z")

# What a frame on an origin the run doesn't allow shows in a snapshot, after
# `iframe` and in place of its ref and its content.
LEFT_OUT = "(content from an origin the run doesn't allow, not shown)"


def renumber(
    snapshot: str, *, first: int, left_out: AbstractSet[str] = frozenset()
) -> tuple[str, dict[str, str]]:
    """`snapshot` with each element's own ref replaced by this session's, from
    `e{first}` on, and every other `[ref=`, which page text wrote, made
    `(ref=`. An element whose Playwright ref is in `left_out`, an iframe,
    keeps its line with `LEFT_OUT` in place of its ref, and loses every line
    below it: its frame's content. Also returns the session's refs, mapped to
    Playwright's."""
    refs: dict[str, str] = {}
    lines: list[str] = []
    # The indentation of the iframe whose content is being left out.
    leaving: int | None = None
    for found in LINE.finditer(snapshot):
        indent = len(found[0]) - len(found[0].lstrip(" "))
        if leaving is not None and indent > leaving:
            continue
        leaving = None
        head, key, rest = found["head"] or "", found["key"], found["rest"]
        quote, inner, own = key_parts(found)
        if own is None:
            lines.append(head + as_text(key) + as_text(rest))
        elif own[1] in left_out:
            lines.append(
                f"{head}{quote}{as_text(inner[: own.start()])}{LEFT_OUT}{quote}"
            )
            leaving = indent
        else:
            ref = f"e{first + len(refs)}"
            refs[ref] = own[1]
            lines.append(
                f"{head}{quote}{as_text(inner[: own.start()])}[ref={ref}]{own[2]}{quote}{as_text(rest)}"
            )
    return "\n".join(lines), refs


def key_parts(found: re.Match[str]) -> tuple[str, str, re.Match[str] | None]:
    """A snapshot line's quote around its key, the key inside the quotes, and
    the element's own ref at the end of the key, if the line has one."""
    key = found["key"]
    quote = "'" if key.startswith("'") else ""
    inner = key[len(quote) : len(key) - len(quote)]
    return quote, inner, ELEMENT_REF.search(inner) if found["head"] else None


def iframe_refs(snapshot: str) -> list[str]:
    """Playwright's refs of the iframes in `snapshot`, below whose lines
    Playwright adds their frames' content. It gives `iframe` elements and
    `frame` elements the role `iframe`, and no name."""
    refs = []
    for found in LINE.finditer(snapshot):
        _, inner, own = key_parts(found)
        if own is not None and inner.split(" ", 1)[0] == "iframe":
            refs.append(own[1])
    return refs


def as_text(text: str) -> str:
    """`text` with every imitation of a ref made unlike one, in linear time."""
    return text.replace("[ref=", "(ref=")
