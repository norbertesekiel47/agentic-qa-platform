"""A compiled script (DATA_MODEL §7, ADR-0025): what each element means and
how to find it (targets), the steps that reach the goal, and the assertions
that establish each expectation. Read strictly, like a spec: unknown fields
are errors and nothing is coerced. A file is read with
`CompiledScript.model_validate_json`."""

import re
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

from aqa_core.browser import (
    BrowserSettings,
    ColorScheme,
    Locale,
    ScaleFactor,
    TimeZone,
    Viewport,
)
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
    except re.error as error:
        raise ValueError(f"{pattern!r} is not a Python regex: {error}") from None
    return pattern


# A Python regex, used with re.search and only the flags it writes inline,
# such as (?i) (ADR-0025).
_Regex = Annotated[NonEmpty, AfterValidator(_regex)]

_KINDS = ("role", "label", "placeholder", "testid", "css")


class Locator(StrictModel):
    """One way of finding a target: a role and an optional accessible name,
    a label, a placeholder, a test ID or a CSS selector, optionally inside a
    `scope`, which is itself a locator."""

    role: AriaRole | None = None
    name: _Normalized | None = None
    label: NonEmpty | None = None
    placeholder: NonEmpty | None = None
    testid: NonEmpty | None = None
    css: NonEmpty | None = None
    scope: Locator | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> Self:
        if self.name is not None and self.role is None:
            raise ValueError("a name goes with a role: write role and name together")
        kinds = [kind for kind in _KINDS if getattr(self, kind) is not None]
        if not kinds:
            raise ValueError(
                "names no kind: write one of role, label, placeholder, testid or css"
            )
        if len(kinds) > 1:
            raise ValueError(
                f"names {len(kinds)} kinds: {', '.join(kinds)}; a locator has one"
            )
        return self


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


class NetworkCheck(_Assertion):
    """A request the browser itself made, or didn't."""

    check: Literal["network_none", "network_seen"]
    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    url_pattern: NonEmpty
    status_class: Literal["1xx", "2xx", "3xx", "4xx", "5xx"]


class ProbeEqualsBaseline(_Assertion):
    check: Literal["probe_equals_baseline"]
    probe: NonEmpty


class VisibleUnoccluded(_Assertion):
    check: Literal["visible_unoccluded"]
    target: NonEmpty
    # Width and height, in CSS pixels. Not strict on the outside, so a JSON
    # list becomes the tuple; each side stays strict.
    min_size_px: Annotated[tuple[_Positive, _Positive], Field(strict=False)]
    in_viewport: StrictBool


Assertion = Annotated[
    TextVisible
    | TextInTarget
    | NotVisible
    | UrlMatches
    | NetworkCheck
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


class _RecordedSettings(BrowserSettings):
    """The settings a script was explored under, which its replays use
    (ADR-0025). Every one is written, so a missing one can't quietly become
    the pinned default."""

    timezone: TimeZone
    locale: Locale
    viewport: Viewport
    device_scale_factor: ScaleFactor
    color_scheme: ColorScheme


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
    browser: _RecordedSettings
    coverage: Coverage
    targets: dict[NonEmpty, Target]
    probe_baselines: dict[NonEmpty, ProbeBaseline]
    steps: ListOf[Step]
    assertions: Annotated[ListOf[Assertion], AtLeastOne]
