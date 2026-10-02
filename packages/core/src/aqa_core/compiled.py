"""A compiled script (DATA_MODEL §7, ADR-0025): what each element means and
how to find it (targets), the steps that reach the goal, and the assertions
that establish each expectation. Read strictly, like a spec: unknown fields
are errors and nothing is coerced.

`CompiledScript.model_validate_json` checks a script's text, but a file is
read through the loader (#46), which first refuses repeated keys:
`model_validate_json` keeps the last, so `"side_effect": true, …,
"side_effect": false` would read as false. The loader checks the text, then
validates the same text in JSON mode, where `compiled_at` may be a string."""

import re
from collections.abc import Container
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
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
    AtLeastOne,
    DistinctListOf,
    ListOf,
    NonEmpty,
    SecretName,
    StrictModel,
)
from aqa_core.spec import StartPath
from aqa_core.text import normalize

# The roles Playwright 1.63's get_by_role accepts, which are WAI-ARIA's, as
# its signature lists them
# (https://playwright.dev/python/docs/api/class-page#page-get-by-role).
AriaRole = Literal[
    "alert",
    "alertdialog",
    "application",
    "article",
    "banner",
    "blockquote",
    "button",
    "caption",
    "cell",
    "checkbox",
    "code",
    "columnheader",
    "combobox",
    "complementary",
    "contentinfo",
    "definition",
    "deletion",
    "dialog",
    "directory",
    "document",
    "emphasis",
    "feed",
    "figure",
    "form",
    "generic",
    "grid",
    "gridcell",
    "group",
    "heading",
    "img",
    "insertion",
    "link",
    "list",
    "listbox",
    "listitem",
    "log",
    "main",
    "marquee",
    "math",
    "menu",
    "menubar",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "meter",
    "navigation",
    "none",
    "note",
    "option",
    "paragraph",
    "presentation",
    "progressbar",
    "radio",
    "radiogroup",
    "region",
    "row",
    "rowgroup",
    "rowheader",
    "scrollbar",
    "search",
    "searchbox",
    "separator",
    "slider",
    "spinbutton",
    "status",
    "strong",
    "subscript",
    "superscript",
    "switch",
    "tab",
    "table",
    "tablist",
    "tabpanel",
    "term",
    "textbox",
    "time",
    "timer",
    "toolbar",
    "tooltip",
    "tree",
    "treegrid",
    "treeitem",
]

_Sha256 = Annotated[StrictStr, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
_Index = Annotated[StrictInt, Field(ge=0)]
_Positive = Annotated[StrictInt, Field(ge=1)]


def _normalized(text: str) -> str:
    normalized = normalize(text)
    if not normalized:
        raise ValueError(
            f"{text!r} compares as empty: it holds only private-use glyphs and "
            "whitespace, which comparing strips"
        )
    if text != normalized:
        raise ValueError(
            f"{text!r} is not normalized: it is compared with private-use glyphs "
            f"stripped and whitespace collapsed, so write {normalized!r}"
        )
    return text


# Text written as it is compared (ADR-0025), so it can match.
_Normalized = Annotated[NonEmpty, AfterValidator(_normalized)]


def _regex(pattern: str) -> str:
    try:
        re.compile(pattern)
    # Beyond re.error, re.compile raises OverflowError for a huge repeat count
    # (a{4294967296}) and RecursionError for deep nesting; pydantic would let
    # either escape as a raw exception instead of a validation error.
    except (re.error, OverflowError, RecursionError) as error:
        raise ValueError(f"{pattern!r} is not a Python regex: {error}") from None
    return pattern


# A Python regex, used with re.search and only the flags it writes inline,
# such as (?i) (ADR-0025).
_Regex = Annotated[NonEmpty, AfterValidator(_regex)]


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
    name: _Normalized | None = None


class ByLabel(_Locator):
    label: NonEmpty


class ByPlaceholder(_Locator):
    placeholder: NonEmpty


class ByTestId(_Locator):
    testid: NonEmpty


def _one_selector(css: str) -> str:
    # Playwright chains selectors at >>
    # (https://playwright.dev/python/docs/other-locators#chaining-selectors),
    # even after css=: on 1.63, css= chained into xpath= and into an engine
    # that enters frames. CSS itself never uses >>.
    if ">>" in css:
        raise ValueError(
            f"{css!r} isn't one CSS selector: Playwright reads >> as a chain "
            "into another selector engine"
        )
    return css


class ByCss(_Locator):
    css: Annotated[NonEmpty, AfterValidator(_one_selector)]


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

    text: _Normalized | None = None
    pattern: _Regex | None = None

    @model_validator(mode="after")
    def _text_or_pattern(self) -> Self:
        if (self.text is None) == (self.pattern is None):
            raise ValueError("a text check takes text or pattern, exactly one")
        return self


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
    pattern: _Regex


class _NetworkCheck(_Assertion):
    """A request in the browser's own traffic, by method, URL and status."""

    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    url_pattern: NonEmpty
    status_class: Literal["1xx", "2xx", "3xx", "4xx", "5xx"]


class NetworkNone(_NetworkCheck):
    check: Literal["network_none"]


class NetworkSeen(_NetworkCheck):
    check: Literal["network_seen"]


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
    json_path: NonEmpty


def _every_setting(settings: BrowserSettings) -> BrowserSettings:
    # Replay uses the settings the script was explored under (ADR-0025), so a
    # missing one must not quietly become the pinned default. Read from the
    # model, so a setting added later is required here too.
    missing = [
        name
        for name in BrowserSettings.model_fields
        if name not in settings.model_fields_set
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
    browser: Annotated[BrowserSettings, AfterValidator(_every_setting)]
    coverage: Coverage
    targets: dict[NonEmpty, Target]
    probe_baselines: dict[NonEmpty, ProbeBaseline]
    steps: ListOf[Step]
    assertions: Annotated[ListOf[Assertion], AtLeastOne]
