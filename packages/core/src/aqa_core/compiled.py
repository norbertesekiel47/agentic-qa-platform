"""A compiled script (DATA_MODEL §7, ADR-0025): what each element means and
how to find it (targets), the steps that reach the goal, and the assertions
that establish each expectation. Read strictly, like a spec: unknown fields
are errors and nothing is coerced.

`CompiledScript.model_validate_json` checks a script's text, but a file is
read through `aqa_core.project.load_compiled`, which first refuses repeated
keys: `model_validate_json` keeps the last, so `"side_effect": true, …,
"side_effect": false` would read as false. The loader checks the text,
validates the same text in JSON mode, where `compiled_at` may be a string,
then checks the parts against each other.

A text check's `matches` searches page text with no time limit; a run
evaluates checks through `aqa_runner.text_search`, which bounds each search."""

import re
import threading
import warnings
from collections.abc import Container, Iterator
from typing import Annotated, Literal, Self, cast

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BeforeValidator,
    Discriminator,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    Tag,
    model_validator,
)

from aqa_core.browser import BrowserSettings
from aqa_core.config import ModelRoleName
from aqa_core.schema import (
    AriaRole,
    AtLeastOne,
    DistinctListOf,
    ListOf,
    NonEmpty,
    SecretName,
    StartPath,
    StrictModel,
)
from aqa_core.text import has_pattern, has_text, normalize

_Sha256 = Annotated[StrictStr, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
_Index = Annotated[StrictInt, Field(ge=0)]
_Positive = Annotated[StrictInt, Field(ge=1)]


def _normalized(text: str) -> str:
    normalized = normalize(text)
    if not normalized:
        raise ValueError(
            f"{text!r} compares as empty: comparing strips private-use glyphs, "
            "soft hyphens and zero-width spaces, and collapses whitespace"
        )
    if text != normalized:
        raise ValueError(
            f"{text!r} is not normalized: comparing strips private-use glyphs, "
            "soft hyphens and zero-width spaces, and collapses whitespace, so "
            f"write {normalized!r}"
        )
    return text


# Text written as it is compared (ADR-0025), so it can match.
NormalizedText = Annotated[NonEmpty, AfterValidator(_normalized)]


# warnings.catch_warnings swaps the process-wide warning filters, so checks
# take turns rather than restore each other's filters out of order.
_REGEX_CHECK = threading.Lock()


def _regex(pattern: str) -> str:
    # Beyond re.error, re.compile raises OverflowError for a huge repeat count
    # (a{4294967296}) and RecursionError for deep nesting, and only warns
    # (FutureWarning) for a pattern whose meaning Python will change, such as
    # [[:digit:]]. Pydantic would let any of them escape raw. The filters are
    # global, so for this short check another thread's FutureWarning raises too.
    with _REGEX_CHECK, warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        # A cached pattern comes back without the warning.
        re.purge()
        try:
            re.compile(pattern)
        except (re.error, OverflowError, RecursionError, FutureWarning) as error:
            raise ValueError(f"{pattern!r} is not a Python regex: {error}") from None
    return pattern


# A probe's JSON path (#48): $, then a step for each level, .name for an
# object's key (ASCII letters, digits, _ or -) or [index] for an array's
# item, of at most nine digits. Nothing else, so no path reads two ways.
_JSON_STEP = re.compile(r"\.([A-Za-z0-9_-]+)|\[(0|[1-9][0-9]{0,8})\]")
_JSON_PATH = re.compile(rf"\$(?:{_JSON_STEP.pattern})*")


def _json_path(path: str) -> str:
    if not _JSON_PATH.fullmatch(path):
        raise ValueError(
            f"{path!r} is not a JSON path this format reads: write $, then .name "
            "(ASCII letters, digits, _ or -) or [index] for each step, such as "
            "$.count or $.orders[0].total"
        )
    return path


JsonPath = Annotated[NonEmpty, AfterValidator(_json_path)]


def json_path_steps(path: str) -> tuple[str | int, ...]:
    """The keys (str) and indexes (int) a `JsonPath` steps through, in
    order; ValueError for any other text."""
    _json_path(path)
    return tuple(name or int(index) for name, index in _JSON_STEP.findall(path, pos=1))


# A Python regex, used with re.search and only the flags it writes inline,
# such as (?i) (ADR-0025).
PythonRegex = Annotated[NonEmpty, AfterValidator(_regex)]


def _left_open(css: str) -> bool:
    """Whether Playwright's selector splitter ends `css` inside a quote or an
    escape, as its parseSelectorString scans (Playwright 1.63): a backslash
    takes the next character, and ", ' or ` opens a quote only the same
    character closes. Whatever Playwright chains after such a value, such as
    a scoped locator, would be read as part of it."""
    quote = None
    index = 0
    while index < len(css):
        char = css[index]
        if char == "\\":
            if index + 1 == len(css):
                return True
            index += 2
            continue
        if char == quote:
            quote = None
        elif quote is None and char in "\"'`":
            quote = char
        index += 1
    return quote is not None


# The pseudo-classes Playwright's css engine evaluates itself, not the
# browser: Playwright 1.63's customCSSNames (driver/package/lib/coreBundle.js)
# less the standard not, is, where, has and scope. They match by text,
# position or layout (https://playwright.dev/python/docs/other-locators), so a
# compiled css value refuses them; test_locators.py pins the list.
PLAYWRIGHT_PSEUDO_CLASSES = frozenset(
    {
        "above",
        "below",
        "has-text",
        "left-of",
        "light",
        "near",
        "nth-match",
        "right-of",
        "text",
        "text-is",
        "text-matches",
        "visible",
    }
)


# A CSS name character (https://www.w3.org/TR/css-syntax-3/#ident-code-point)
# and a hex escape's digits (#consume-escaped-code-point).
_NAME_CHAR = re.compile(r"[A-Za-z0-9_\-\u0080-\U0010ffff]")
_HEX = re.compile(r"[0-9A-Fa-f]{1,6}")


def _escape(css: str, index: int) -> tuple[str, int]:
    """The character the escape whose backslash is at `index` stands for,
    and where the escape ends: up to six hex digits and one whitespace
    character after them, or the next character as itself."""
    start = index + 1
    digits = _HEX.match(css, start)
    if digits is None:
        return (css[start], start + 1) if start < len(css) else ("\ufffd", start)
    end = digits.end()
    if css[end : end + 1] in (" ", "\t", "\n"):
        end += 1
    value = int(digits[0], 16)
    usable = 0 < value <= 0x10FFFF and not 0xD800 <= value <= 0xDFFF
    return (chr(value) if usable else "\ufffd"), end


def _starts_escape(css: str, index: int) -> bool:
    """Whether a backslash at `index` starts an escape: not before a newline."""
    return css[index] == "\\" and css[index + 1 : index + 2] != "\n"


def _after_comments(css: str, index: int) -> int:
    while css.startswith("/*", index):
        end = css.find("*/", index + 2)
        index = len(css) if end == -1 else end + 2
    return index


def _after_string(css: str, index: int) -> int:
    """Where the string opened by the quote at `index` ends: at the same
    quote, or before a newline, which ends a string unclosed."""
    quote, index = css[index], index + 1
    while index < len(css) and css[index] not in (quote, "\n"):
        index = _escape(css, index)[1] if css[index] == "\\" else index + 1
    if css[index : index + 1] == quote:
        index += 1
    return index


def _name(css: str, index: int) -> tuple[str, int]:
    """The name starting at `index`, with its escapes decoded, and its end."""
    chars: list[str] = []
    while index < len(css):
        if _NAME_CHAR.match(css, index):
            chars.append(css[index])
            index += 1
        elif _starts_escape(css, index):
            char, index = _escape(css, index)
            chars.append(char)
        else:
            break
    return "".join(chars), index


def _pseudo_classes(css: str) -> Iterator[str]:
    """Each pseudo-class name in `css`, lowercased, as Playwright reads a css
    value: its CSS tokenizer (cssTokenizer.ts, a port of CSS Syntax Level 3,
    https://www.w3.org/TR/css-syntax-3/#tokenization) drops comments, reads
    strings whole and decodes escapes, and its parser lowercases the name
    after a colon. A backslash outside a string escapes a colon or a quote
    too, so neither is read as one. That is a later reading than
    `_left_open`'s: Playwright's splitter, before it, counts quotes even in
    comments and takes a backtick for one, which CSS doesn't."""
    css = re.sub(r"\r\n?|\f", "\n", css)
    index = 0
    while index < len(css):
        if css.startswith("/*", index):
            index = _after_comments(css, index)
        elif css[index] in "\"'":
            index = _after_string(css, index)
        elif _starts_escape(css, index):
            index = _escape(css, index)[1]
        elif css[index] == ":":
            name, index = _name(css, _after_comments(css, index + 1))
            yield name.lower()
        else:
            index += 1


# What JavaScript's String.prototype.trim() removes, white space and line
# terminators (https://tc39.es/ecma262/#sec-string.prototype.trim).
# Playwright's splitter trims each selector part before its CSS tokenizer
# reads it, and CSS reads the non-ASCII ones as part of a name.
_JS_TRIMMED = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def _one_selector(css: str) -> str:
    # Playwright chains selectors at >>
    # (https://playwright.dev/python/docs/other-locators#chaining-selectors),
    # even after css=: on 1.63, css= chained into xpath= and into an engine
    # that enters frames. CSS itself never uses >>, and a quote CSS ignores,
    # inside a comment, still counts for Playwright's splitter.
    if not css.strip():
        raise ValueError("a css value is a selector, not only whitespace")
    if ">>" in css:
        raise ValueError(
            f"{css!r} isn't one CSS selector: Playwright reads >> as a chain "
            r"into another selector engine; inside an attribute value, write \>\>"
        )
    if _left_open(css):
        raise ValueError(
            f"{css!r} leaves a quote or escape open, so Playwright would read "
            "what follows it as part of it: close every quote, even in a comment"
        )
    if css.strip(_JS_TRIMMED) != css:
        # Trimmed, `button:visible` followed by a no-break space would read
        # as :visible to Playwright and as :visible\xa0 to the scan below.
        raise ValueError(
            f"{css!r} starts or ends with white space: write it trimmed "
            "(Playwright trims the end before CSS reads the value)"
        )
    for name in _pseudo_classes(css):
        if name in PLAYWRIGHT_PSEUDO_CLASSES:
            raise ValueError(
                f"{css!r} uses Playwright's own pseudo-class :{name}, which "
                "Playwright evaluates instead of CSS: a css value is CSS only"
            )
    return css


# One CSS selector, which resolution must send to Playwright as css=<value>.
_OneSelector = Annotated[NonEmpty, AfterValidator(_one_selector)]


class _Locator(StrictModel):
    """What every kind of locator has: an optional `scope`, itself a
    locator, inside which the match must be unique."""

    scope: Locator | None = None

    def __str__(self) -> str:
        # As the script writes it, so a message names it readably.
        return self.model_dump_json(exclude_none=True)


class ByRole(_Locator):
    """A role, and optionally the accessible name, compared normalized."""

    role: AriaRole
    name: NormalizedText | None = None


class ByLabel(_Locator):
    label: NonEmpty


class ByPlaceholder(_Locator):
    placeholder: NonEmpty


class ByTestId(_Locator):
    testid: NonEmpty


class ByCss(_Locator):
    css: _OneSelector


_KINDS = ("role", "label", "placeholder", "testid", "css")


def _kind(value: object) -> str | None:
    """The one kind a locator names: the key in a JSON object, or the kind
    field of a locator built in Python. None for no kind or several."""
    keys: Container[object]
    if isinstance(value, dict):
        keys = value
    elif isinstance(value, _Locator):
        keys = type(value).model_fields
    else:
        return None
    named = [kind for kind in _KINDS if kind in keys]
    return named[0] if len(named) == 1 else None


# One way of finding a target. A JSON object names exactly one kind, which
# picks the class, so `name` exists only beside `role`.
type Locator = Annotated[
    Annotated[ByRole, Tag("role")]
    | Annotated[ByLabel, Tag("label")]
    | Annotated[ByPlaceholder, Tag("placeholder")]
    | Annotated[ByTestId, Tag("testid")]
    | Annotated[ByCss, Tag("css")],
    Discriminator(
        _kind,
        custom_error_type="locator_kind",
        custom_error_message=(
            "a locator names exactly one kind: role, label, placeholder, testid or css"
        ),
    ),
]


class Target(StrictModel):
    """An element's meaning, and the locators tried in order to find it."""

    semantic: NonEmpty
    locators: Annotated[DistinctListOf[Locator], AtLeastOne]


class _Step(StrictModel):
    """What every step records. `side_effect` is required and never
    defaulted: a default of false would let a continuation repeat a purchase
    (ADR-0006 amendment)."""

    seq: _Positive
    side_effect: StrictBool
    # Why the flag is true (ADR-0025): `false` needs positive evidence, and
    # anything else is `true` with its reason here.
    side_effect_basis: NonEmpty | None = None
    satisfies: DistinctListOf[NonEmpty] = ()

    @model_validator(mode="after")
    def _basis_goes_with_a_true_flag(self) -> Self:
        if self.side_effect and self.side_effect_basis is None:
            raise ValueError(
                "a step whose side_effect is true says why in side_effect_basis"
            )
        if not self.side_effect and self.side_effect_basis is not None:
            raise ValueError(
                "side_effect_basis says why a step's side_effect is true; "
                "this step's is false, so remove it"
            )
        return self


class Navigate(_Step):
    action: Literal["navigate"]
    # A path on the start origin, held to start_url's rules (DATA_MODEL §6).
    # The executor joins it to the start origin (#46, #89).
    url: StartPath


class Reload(_Step):
    action: Literal["reload"]


class Click(_Step):
    action: Literal["click"]
    target: NonEmpty


class Fill(_Step):
    action: Literal["fill"]
    target: NonEmpty
    value: StrictStr


class FillSecret(_Step):
    action: Literal["fill_secret"]
    target: NonEmpty
    secret: SecretName


class Select(_Step):
    action: Literal["select"]
    target: NonEmpty
    option: NonEmpty


class Press(_Step):
    action: Literal["press"]
    key: NonEmpty


Step = Annotated[
    Navigate | Reload | Click | Fill | FillSecret | Select | Press,
    Field(discriminator="action"),
]


class _Assertion(StrictModel):
    id: NonEmpty
    expect_index: _Index


class _TextCheck(_Assertion):
    """A check of rendered text, by a literal `text` or a regex `pattern`
    (ADR-0025)."""

    text: NormalizedText | None = None
    pattern: PythonRegex | None = None

    @model_validator(mode="after")
    def _text_or_pattern(self) -> Self:
        if (self.text is None) == (self.pattern is None):
            raise ValueError("a text check takes text or pattern, exactly one")
        return self

    def matches(self, rendered: str) -> bool:
        """Whether an element's rendered text meets this check: the literal
        as whole words, ignoring case, or the pattern found anywhere, both
        against the normalized text (DATA_MODEL §7)."""
        if self.text is not None:
            return has_text(rendered, self.text)
        # _text_or_pattern guarantees a pattern when there is no text.
        return has_pattern(rendered, cast(str, self.pattern))


class TextVisible(_TextCheck):
    check: Literal["text_visible"]


class TextInTarget(_TextCheck):
    check: Literal["text_in_target"]
    target: NonEmpty


class NotVisible(_Assertion):
    check: Literal["not_visible"]
    target: NonEmpty


class UrlMatches(_Assertion):
    check: Literal["url_matches"]
    pattern: PythonRegex


HttpMethod = Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
StatusClass = Literal["1xx", "2xx", "3xx", "4xx", "5xx"]


class _NetworkCheck(_Assertion):
    """A request in the browser's own traffic, by method, URL and status."""

    method: HttpMethod
    url_pattern: NonEmpty
    status_class: StatusClass


class NetworkNone(_NetworkCheck):
    check: Literal["network_none"]


class NetworkSeen(_NetworkCheck):
    check: Literal["network_seen"]


# A probe_equals check's value, planned or compiled: never true, 2.0 or an
# empty string. A plain union, so the plan's response schema names no alias.
ProbeValue = StrictInt | NonEmpty


class ProbeEquals(_Assertion):
    """A probe the spec declares reads `value` at `json_path` (#48)."""

    check: Literal["probe_equals"]
    probe: NonEmpty
    json_path: JsonPath
    value: ProbeValue


class ProbeEqualsBaseline(_Assertion):
    check: Literal["probe_equals_baseline"]
    probe: NonEmpty


class VisibleUnoccluded(_Assertion):
    check: Literal["visible_unoccluded"]
    target: NonEmpty
    # Width and height, in CSS pixels. JSON mode reads an array as the
    # tuple; not strict on the outside, so Python mode reads a list too.
    min_size_px: Annotated[tuple[_Positive, _Positive], Field(strict=False)]
    in_viewport: StrictBool


Assertion = Annotated[
    TextVisible
    | TextInTarget
    | NotVisible
    | UrlMatches
    | NetworkNone
    | NetworkSeen
    | ProbeEquals
    | ProbeEqualsBaseline
    | VisibleUnoccluded,
    Field(discriminator="check"),
]


class ExpectationCoverage(StrictModel):
    """One expectation in the coverage plan: what it is about, what it
    claims, and the assertions that establish the claim."""

    expect_index: _Index
    subject: NonEmpty
    claim: NonEmpty
    assertions: Annotated[DistinctListOf[NonEmpty], AtLeastOne]


class RequiredCondition(StrictModel):
    """A condition the goal or an expectation needs, such as a reload."""

    id: NonEmpty
    condition: NonEmpty


class Coverage(StrictModel):
    """The coverage plan explore froze before the browser opened
    (ADR-0024)."""

    plan_hash: _Sha256
    expectations: Annotated[ListOf[ExpectationCoverage], AtLeastOne]
    requires: ListOf[RequiredCondition]


class ProbeBaseline(StrictModel):
    """When a probe's baseline is read, and which value of its JSON."""

    capture_before_seq: _Positive
    json_path: JsonPath


def _every_setting(settings: object) -> object:
    # Replay uses the settings the script was explored under (ADR-0025), so a
    # setting a file leaves out must not quietly become the pinned default.
    # Checked on the file's object, not on a BrowserSettings built in Python,
    # which always holds every setting. Read from the model, so a setting added
    # later is required here too.
    if isinstance(settings, dict):
        missing = [
            name for name in BrowserSettings.model_fields if name not in settings
        ]
        if missing:
            raise ValueError(
                f"records no {', '.join(missing)}: a compiled script records every "
                "browser setting it was explored under"
            )
    return settings


class CompiledBy(StrictModel):
    mode: Literal["explore"]
    models: dict[ModelRoleName, NonEmpty]
    price_map: NonEmpty


def _version_1(version: int) -> int:
    if version != 1:
        raise ValueError(f"schema_version {version} is unknown: this reader reads 1")
    return version


class CompiledScript(StrictModel):
    """`<spec root>/.compiled/<spec id>.json`, schema version 1."""

    # An integer, not Literal[1], which pydantic matches by equality, so it
    # would take true and 1.0.
    schema_version: Annotated[StrictInt, AfterValidator(_version_1)]
    spec_id: NonEmpty
    spec_hash: _Sha256
    compiled_at: AwareDatetime
    compiled_by: CompiledBy
    confirmed: StrictBool
    browser: Annotated[BrowserSettings, BeforeValidator(_every_setting)]
    coverage: Coverage
    targets: dict[NonEmpty, Target]
    probe_baselines: dict[NonEmpty, ProbeBaseline]
    steps: ListOf[Step]
    assertions: Annotated[ListOf[Assertion], AtLeastOne]
