"""A bound value a page splits with gaps (#53 ZW, #195; ADR-0026's #53 ZW
amendment): Unicode's default-ignorable code points, which show nothing, and
its private-use code points, which mean nothing of their own; the set
Chromium's `[\\p{Default_Ignorable_Code_Point}\\p{Co}]` matches. The scan
finds the value with any run of them between any two of its characters, each
in any spelling the scan finds a value's own characters in, and replaces only
the value's span. So only the marker reaches the model: in a snapshot, a
field the page reflects, a part's line, a request and a tool's refusal."""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from urllib.parse import quote

import pytest
from aqa_core.project import SecretDestination
from aqa_runner import reasons
from aqa_runner.bound_secrets import GAP, BoundSecret
from aqa_runner.browser_session import BrowserSession, RefError, open_browser_session
from aqa_runner.navigator import Click, Fill, Prefix, Turn, navigator_request
from aqa_runner.navigator_tools import ActionResult, Attempt
from aqa_runner.redaction import Redactor
from aqa_runner.run_record import RefusedWriteError, RunRecord, require_clean
from playwright.async_api import async_playwright
from pydantic import SecretStr

from packages.runner.tests.egress_fixtures import egress_proxy
from packages.runner.tests.executor_fixtures import App, read_steps, serving_app
from packages.runner.tests.navigator_fixtures import NAME, SAVE, bound, on_app, ref
from packages.runner.tests.pilot_pages import ORIGIN, put

VALUE = "fake-secret-49"
MARKER = "[SECRET:FAKE]"
ZWJ = "\N{ZERO WIDTH JOINER}"
ZWSP = "\N{ZERO WIDTH SPACE}"
SHY = "\N{SOFT HYPHEN}"
BOM = "\N{ZERO WIDTH NO-BREAK SPACE}"
# Private-use characters: one of the BMP's, one of plane 15's.
PUA = chr(0xE001)
PLANE15 = chr(0xF0041)
UTF8 = '<meta charset="utf-8">'


def split(between: str) -> str:
    return f"fake-se{between}cret-49"


def scan(value: str = VALUE) -> Redactor:
    return Redactor(
        [
            BoundSecret(
                "FAKE", SecretStr(value), SecretDestination((ORIGIN,), "password")
            )
        ]
    )


def exactly(text: str) -> str:
    return f"^{re.escape(text)}$"


# The first and last code point of each range of Unicode 16's
# Default_Ignorable_Code_Point (DerivedCoreProperties.txt) and of its
# private-use areas.
EDGES = [
    0x00AD,
    0x034F,
    0x061C,
    0x115F,
    0x1160,
    0x17B4,
    0x17B5,
    0x180B,
    0x180F,
    0x200B,
    0x200F,
    0x202A,
    0x202E,
    0x2060,
    0x206F,
    0x3164,
    0xE000,
    0xF8FF,
    0xFE00,
    0xFE0F,
    0xFEFF,
    0xFFA0,
    0xFFF0,
    0xFFF8,
    0x1BCA0,
    0x1BCA3,
    0x1D173,
    0x1D17A,
    0xE0000,
    0xE0FFF,
    0xF0000,
    0xFFFFD,
    0x100000,
    0x10FFFD,
]


@pytest.mark.parametrize("point", EDGES, ids=lambda point: f"U+{point:04X}")
def test_a_value_split_by_any_of_them_is_redacted_in_every_spelling(point: int) -> None:
    shown = split(chr(point))
    spellings = [
        shown,
        quote(shown, safe=""),
        json.dumps(shown)[1:-1],
        repr(shown.encode())[2:-1],
        repr(shown)[1:-1],
    ]

    assert [scan().redact(f"<{spelled}>") for spelled in spellings] == [
        f"<{MARKER}>"
    ] * 5


def test_what_shows_nothing_around_a_value_stays_as_it_was() -> None:
    assert (
        scan().redact(f"x{ZWJ}{split(ZWJ)}{ZWJ}y{SHY}") == f"x{ZWJ}{MARKER}{ZWJ}y{SHY}"
    )


def test_a_run_of_them_between_every_two_characters_is_still_the_value() -> None:
    assert [
        scan().redact((ZWJ + ZWSP).join(VALUE)),
        scan().redact(split(ZWJ * 3 + SHY + "%E2%81%A0")),
    ] == [MARKER, MARKER]


def test_a_near_value_or_a_visible_separator_is_left_alone() -> None:
    near = [
        split(ZWJ)[:-1] + "8",
        split("."),
        split(" "),
        split("\N{BRAILLE PATTERN BLANK}"),
    ]

    assert [scan().redact(text) for text in near] == near


def test_a_value_holding_one_is_found_with_or_without_it() -> None:
    joined = scan(f"fake{ZWJ}secret")
    spaced = scan(f"fake{BOM}secret-49")
    glyph = scan(f"fake{PUA}secret")

    assert [
        joined.redact("fakesecret"),
        joined.redact(f"fake{ZWJ}secret"),
        joined.redact("fake\N{WORD JOINER}secret"),
        # U+FEFF is JavaScript whitespace, which Playwright writes as a space,
        # and shows nothing, which a consumer can strip (#53 P19's parts).
        spaced.redact("fake secret-49"),
        spaced.redact(f"fake{BOM}secret-49"),
        spaced.redact("fakesecret-49"),
        # `aqa_core.text.normalize` strips private-use characters.
        glyph.redact("fakesecret"),
        glyph.redact(f"fake{PLANE15}secret"),
    ] == [MARKER] * 8


def test_a_final_sigma_beside_a_left_out_gap_is_found_in_every_spelling() -> None:
    # Without its private-use character, the value's lowercase ends in a
    # final sigma, which percent-encoding and JSON write apart from the
    # other small sigma.
    redactor = scan(
        f"FAKE-\N{GREEK CAPITAL LETTER OMICRON}{PUA}\N{GREEK CAPITAL LETTER SIGMA}"
    )
    shown = "fake-\N{GREEK SMALL LETTER OMICRON}\N{GREEK SMALL LETTER FINAL SIGMA}"
    spellings = [shown, quote(shown, safe=""), json.dumps(shown)[1:-1]]
    turn = Turn(
        log=(),
        notes=quote(shown, safe=""),
        results=(json.dumps(shown)[1:-1],),
        snapshot=redactor.redact(""),
    )

    sent = navigator_request(Prefix(spec="{}", plan="{}"), turn, redactor=redactor)

    assert [redactor.redact(f"<{spelled}>") for spelled in spellings] == [
        f"<{MARKER}>"
    ] * 3
    assert sent[2].content == (
        "<log>\n\n</log>\n\n"
        f"<notes>\n{MARKER}\n</notes>\n\n"
        f"<results>\n{MARKER}\n</results>\n\n"
        "<snapshot>\n\n</snapshot>"
    )
    with pytest.raises(
        RefusedWriteError,
        match=exactly(
            "steps.jsonl: a bound value's spelling is in it, so it isn't written"
        ),
    ):
        require_clean(redactor, "steps.jsonl")(json.dumps({"text": shown}))


TWO_BOMS = f"fake{BOM}secret{BOM}49"


@pytest.mark.parametrize(
    "shown",
    [
        TWO_BOMS,
        f"fakesecret{BOM}49",
        f"fake{BOM}secret49",
        "fakesecret49",
        "fakesecret 49",
        "fake secret49",
    ],
    ids=[
        "both",
        "first-out",
        "second-out",
        "neither",
        "first-out-space",
        "second-out-space",
    ],
)
def test_each_of_a_values_own_bom_can_be_left_out_on_its_own(shown: str) -> None:
    redactor = scan(TWO_BOMS)
    spellings = [shown, quote(shown, safe=""), json.dumps(shown)[1:-1]]
    turn = Turn(
        log=(f"1. fill e2 with {shown}",),
        notes=shown,
        results=(),
        snapshot=redactor.redact(f"- paragraph [ref=e3]: {shown}"),
    )

    sent = navigator_request(Prefix(spec="{}", plan="{}"), turn, redactor=redactor)

    assert [redactor.redact(f"<{spelled}>") for spelled in spellings] == [
        f"<{MARKER}>"
    ] * 3
    assert [redactor.finds(shown), redactor.spells({"text": shown})] == [True, True]
    assert sent[2].content == (
        f"<log>\n1. fill e2 with {MARKER}\n</log>\n\n"
        f"<notes>\n{MARKER}\n</notes>\n\n"
        "<results>\n\n</results>\n\n"
        f"<snapshot>\n- paragraph [ref=e3]: {MARKER}\n</snapshot>"
    )
    with pytest.raises(
        RefusedWriteError,
        match=exactly(
            "steps.jsonl: a bound value's spelling is in it, so it isn't written"
        ),
    ):
        require_clean(redactor, "steps.jsonl")(json.dumps({"text": shown}))


def test_spells_and_the_journal_check_find_a_split_value() -> None:
    redactor = scan()

    assert [
        redactor.spells({"locator": {"css": f"#{split(ZWJ)}"}}),
        redactor.spells([split(SHY)]),
    ] == [True, True]
    with pytest.raises(
        RefusedWriteError,
        match=exactly(
            "steps.jsonl: a bound value's spelling is in it, so it isn't written"
        ),
    ):
        require_clean(redactor, "steps.jsonl")(json.dumps({"text": split(ZWJ)}))


def test_saved_evidence_holds_the_marker(tmp_path: Path) -> None:
    record = RunRecord.create(tmp_path).redacting(scan())

    saved = record.write_text("a11y_snapshot.yaml", f"- paragraph: {split(ZWJ)}\n")

    assert saved.read_text() == f"- paragraph: {MARKER}\n"


@pytest.mark.parametrize(
    "between",
    [ZWJ, ZWSP, SHY, PUA, PLANE15],
    ids=["U+200D", "U+200B", "U+00AD", "U+E001", "U+F0041"],
)
def test_a_tool_error_quoting_a_split_value_says_only_its_marker(between: str) -> None:
    quoted = RefError(f"{split(between)!r} isn't a ref in the current snapshot")

    assert (
        reasons.reason(quoted, scan())
        == f"'{MARKER}' isn't a ref in the current snapshot"
    )


def test_a_split_value_in_any_block_of_a_request_reaches_the_model_as_its_marker() -> (
    None
):
    redactor = scan()
    turn = Turn(
        log=(f"1. fill e2 with {split(ZWJ)}",),
        notes=split(ZWSP),
        results=(f"the page now says {split(SHY)}",),
        snapshot=redactor.redact(f"- paragraph [ref=e3]: {split(ZWJ)}"),
    )

    sent = navigator_request(Prefix(spec="{}", plan="{}"), turn, redactor=redactor)

    assert sent[2].content == (
        f"<log>\n1. fill e2 with {MARKER}\n</log>\n\n"
        f"<notes>\n{MARKER}\n</notes>\n\n"
        f"<results>\nthe page now says {MARKER}\n</results>\n\n"
        f"<snapshot>\n- paragraph [ref=e3]: {MARKER}\n</snapshot>"
    )
    # The check every request passes before it is sent (#53 P15's `unspelled`).
    assert [redactor.finds(message.text) for message in sent] == [False, False, False]


def in_session[T](
    scenario: Callable[[BrowserSession], Awaitable[T]], redactor: Redactor
) -> T:
    async def run() -> T:
        async with (
            async_playwright() as playwright,
            egress_proxy() as egress,
            open_browser_session(
                playwright.chromium, egress=egress, redactor=redactor
            ) as session,
            asyncio.timeout(60),
        ):
            return await scenario(session)

    return asyncio.run(run())


SPLIT_PAGE = UTF8 + "".join(
    f"<p>{name}: {split(f'&#{point};')}</p>"
    f'<button aria-label="{split(f"&#{point};")}">b {name}</button>'
    f'<input aria-label="f {name}" value="{split(f"&#{point};")}">'
    f'<a href="/q?v={split(f"&#{point};")}">l {name}</a>'
    for name, point in (("ZWJ", 0x200D), ("ZWSP", 0x200B), ("SHY", 0x00AD))
)


def test_a_value_split_on_the_page_reaches_the_snapshot_as_its_marker() -> None:
    async def scenario(session: BrowserSession) -> str:
        await put(session, SPLIT_PAGE)
        return await session.snapshot()

    snapshot = in_session(scenario, scan())

    assert snapshot == "\n".join(
        [
            "- generic [active] [ref=e1]:",
            f'  - paragraph [ref=e2]: "ZWJ: {MARKER}"',
            f'  - button "{MARKER}" [ref=e3]: b ZWJ',
            f'  - textbox "f ZWJ" [ref=e4]: {MARKER}',
            '  - link "l ZWJ" [ref=e5] [cursor=pointer]:',
            f"    - /url: /q?v={MARKER}",
            f'  - paragraph [ref=e6]: "ZWSP: {MARKER}"',
            f'  - button "{MARKER}" [ref=e7]: b ZWSP',
            f'  - textbox "f ZWSP" [ref=e8]: {MARKER}',
            '  - link "l ZWSP" [ref=e9] [cursor=pointer]:',
            f"    - /url: /q?v={MARKER}",
            f'  - paragraph [ref=e10]: "SHY: {MARKER}"',
            f'  - button "{MARKER}" [ref=e11]: b SHY',
            f'  - textbox "f SHY" [ref=e12]: {MARKER}',
            '  - link "l SHY" [ref=e13] [cursor=pointer]:',
            f"    - /url: /q?v={MARKER}",
        ]
    )


REFLECTS = (
    UTF8
    + '<label>Password <input id="password" type="password"></label>'
    + '<label>Shown <input id="shown"></label><p id="echo"></p>'
    + "<script>password.addEventListener('input', () => {"
    + " shown.value = [...password.value].join('\\u200d');"
    + " echo.textContent = password.value.replace('-se', '-se\\u00ad')"
    + ".replace('cret', 'c\\u200bret');"
    + "});</script>"
)


def test_a_filled_value_the_page_reflects_split_reaches_the_snapshot_as_its_marker() -> (
    None
):
    secret = BoundSecret(
        "FAKE", SecretStr(VALUE), SecretDestination((ORIGIN,), "password")
    )

    async def scenario(session: BrowserSession) -> tuple[bool, str]:
        await put(session, REFLECTS)
        field = await session.page.query_selector("#password")
        assert field is not None
        await session.fill_secret(field, secret)
        shown = await session.page.eval_on_selector("#shown", "field => field.value")
        return shown == ZWJ.join(VALUE), await session.snapshot()

    split_on_the_page, snapshot = in_session(scenario, Redactor([secret]))

    assert split_on_the_page
    assert snapshot == "\n".join(
        [
            "- generic [ref=e1]:",
            "  - generic [ref=e2]:",
            "    - text: Password",
            f'    - textbox "Password" [active] [ref=e3]: {MARKER}',
            "  - generic [ref=e4]:",
            "    - text: Shown",
            f'    - textbox "Shown" [ref=e5]: {MARKER}',
            f"  - paragraph [ref=e6]: {MARKER}",
        ]
    )


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


def test_an_action_argument_holding_a_split_value_is_refused_before_its_intent(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> list[ActionResult]:
        save, name = ref(attempt, "button", "Save"), ref(attempt, "textbox", "Name")
        return [
            await attempt.act(Click(ref=split(ZWJ), meaning=SAVE)),
            await attempt.act(Fill(ref=name, meaning=NAME, text=split(ZWSP))),
            await attempt.act(Click(ref=save, meaning=f"the {split(SHY)} button")),
        ]

    results = on_app(
        app, tmp_path, scenario, secrets=[bound(app.origin, VALUE, "FAKE")]
    )

    holds = "holds a test secret's value"
    assert [result.text for result in results] == [
        f"click did not run: its ref {holds}",
        f"fill did not run: its text {holds}; fill a secret with fill_secret",
        f"click did not run: its meaning {holds}",
    ]
    assert app.paths() == ["/page/form"]
    assert app.record is not None
    assert read_steps(app.record) == []


def test_the_scans_gaps_are_chromiums_default_ignorable_and_private_use_sets() -> None:
    async def scenario(session: BrowserSession) -> list[list[int]]:
        await put(session, "<p>x</p>")
        ranges: list[list[int]] = await session.page.evaluate(
            """() => {
                const gap = /[\\p{Default_Ignorable_Code_Point}\\p{Co}]/u, ranges = [];
                for (let point = 0; point <= 0x10ffff; point++) {
                    if (point >= 0xd800 && point <= 0xdfff) continue;
                    if (!gap.test(String.fromCodePoint(point))) continue;
                    const last = ranges[ranges.length - 1];
                    if (last && last[1] === point - 1) last[1] = point;
                    else ranges.push([point, point]);
                }
                return ranges;
            }"""
        )
        return ranges

    chromiums = in_session(scenario, scan())
    ours: list[list[int]] = []
    for point in range(0x110000):
        if 0xD800 <= point <= 0xDFFF or not GAP.fullmatch(chr(point)):
            continue
        if ours and ours[-1][1] == point - 1:
            ours[-1][1] = point
        else:
            ours.append([point, point])

    assert ours == chromiums


def test_a_gap_inside_a_whitespace_run_is_still_the_value() -> None:
    shown = f"fake {ZWJ} secret-49"
    spellings = [shown, quote(shown, safe=""), json.dumps(shown)[1:-1]]

    assert [scan("fake  secret-49").redact(f"<{form}>") for form in spellings] == [
        f"<{MARKER}>"
    ] * 3
    assert scan("fake  secret-49").spells([shown]) is True
    # A gap never stands in for whitespace the value holds.
    assert scan("fake secret-49").redact(f"fake{ZWJ}secret-49") == (
        f"fake{ZWJ}secret-49"
    )


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        ("fake-ß-secret-49", f"fake-s{ZWJ}S-secret-49"),
        ("fake-ﬁ-secret-49", f"fake-F{ZWSP}i-secret-49"),
        ("fake-İ-secret-49", f"fake-i{SHY}\N{COMBINING DOT ABOVE}-secret-49"),
    ],
    ids=["sharp-s", "fi-ligature", "dotted-I"],
)
def test_a_gap_inside_a_case_expansion_is_still_the_value(
    value: str, shown: str
) -> None:
    spellings = [shown, quote(shown, safe=""), json.dumps(shown)[1:-1]]

    assert [scan(value).redact(f"<{form}>") for form in spellings] == [
        f"<{MARKER}>"
    ] * 3


def test_split_base64_is_replaced_whole_at_every_alignment() -> None:
    # "fake" after 0, 1 and 2 bytes of "x", base64-encoded, split at every
    # character, with a gap on either side; then a core whose partial
    # character is missing, after a gap.
    redactor = scan("fake")
    shown = [
        f"<{ZWJ}{ZWJ.join(encoded)}{ZWJ}>"
        for encoded in ("ZmFrZQ==", "eGZha2U=", "eHhmYWtl")
    ] + [f"<{ZWJ}{ZWJ.join('Zha2U=')}>"]

    assert [redactor.redact(text) for text in shown] == [
        f"<{ZWJ}{MARKER}{ZWJ}>",
        f"<{ZWJ}e{ZWJ}{MARKER}{ZWJ}>",
        f"<{ZWJ}e{ZWJ}H{ZWJ}{MARKER}{ZWJ}>",
        f"<{ZWJ}{MARKER}>",
    ]


PRIVATE_USE_PAGE = UTF8 + "".join(
    f"<p>{name}: {split(f'&#{point};')}</p>"
    f'<button aria-label="{split(f"&#{point};")}">b {name}</button>'
    f'<input aria-label="f {name}" value="{split(f"&#{point};")}">'
    f'<a href="/q?v={split(f"&#{point};")}">l {name}</a>'
    for name, point in (("PUA", 0xE001), ("PLANE15", 0xF0041))
)


def test_a_value_split_by_a_private_use_character_reaches_the_snapshot_as_its_marker() -> (
    None
):
    async def scenario(session: BrowserSession) -> str:
        await put(session, PRIVATE_USE_PAGE)
        return await session.snapshot()

    snapshot = in_session(scenario, scan())

    assert snapshot == "\n".join(
        [
            "- generic [active] [ref=e1]:",
            f'  - paragraph [ref=e2]: "PUA: {MARKER}"',
            f'  - button "{MARKER}" [ref=e3]: b PUA',
            f'  - textbox "f PUA" [ref=e4]: {MARKER}',
            '  - link "l PUA" [ref=e5] [cursor=pointer]:',
            f"    - /url: /q?v={MARKER}",
            f'  - paragraph [ref=e6]: "PLANE15: {MARKER}"',
            f'  - button "{MARKER}" [ref=e7]: b PLANE15',
            f'  - textbox "f PLANE15" [ref=e8]: {MARKER}',
            '  - link "l PLANE15" [ref=e9] [cursor=pointer]:',
            f"    - /url: /q?v={MARKER}",
        ]
    )


@pytest.mark.parametrize("between", [PUA, PLANE15], ids=["U+E001", "U+F0041"])
def test_a_part_line_holding_a_value_split_by_a_private_use_character_shows_its_marker(
    between: str,
) -> None:
    # A part's line as #53 P19's describe_parts writes it: its tag and
    # classes as the page wrote them, its text JSON-escaped.
    lines = [
        f"- p1.1 span.{split(between)} {json.dumps('your code')}",
        f"- p1.2 span.note {json.dumps(split(between))}",
    ]

    assert [scan().redact(line) for line in lines] == [
        f'- p1.1 span.{MARKER} "your code"',
        f'- p1.2 span.note "{MARKER}"',
    ]


def test_a_value_split_by_a_private_use_character_in_a_request_reaches_the_model_as_its_marker() -> (
    None
):
    redactor = scan()
    turn = Turn(
        log=(f"1. fill e2 with {split(PUA)}",),
        notes=split(PLANE15),
        results=(f"the page now says {split(PUA + PLANE15)}",),
        snapshot=redactor.redact(f"- paragraph [ref=e3]: {split(PLANE15)}"),
    )

    sent = navigator_request(Prefix(spec="{}", plan="{}"), turn, redactor=redactor)

    assert sent[2].content == (
        f"<log>\n1. fill e2 with {MARKER}\n</log>\n\n"
        f"<notes>\n{MARKER}\n</notes>\n\n"
        f"<results>\nthe page now says {MARKER}\n</results>\n\n"
        f"<snapshot>\n- paragraph [ref=e3]: {MARKER}\n</snapshot>"
    )
    assert [redactor.finds(message.text) for message in sent] == [False, False, False]
