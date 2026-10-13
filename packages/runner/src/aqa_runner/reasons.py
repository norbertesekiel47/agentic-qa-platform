"""The text a failed step's, an unevaluated assertion's or a navigator tool's
result carries for what raised (ADR-0026's #53 P9+P10 amendment), built on
`redaction.error_text`.

An owned error is one whose message the runner or the session writes; any
other is Playwright's, whose message the page may have chosen, and which a
withheld run cuts to its class and the call it names. A run withholds when it
binds a test secret, filled or not: its redactor isn't empty, the one
predicate replay and explore share, so no caller chooses it.

In a withheld run an owned reason comes from a closed vocabulary that names no
origin, host, port, URL or path: a policy refusal names only its kind, and a
`fill_secret` refusal only the secret and the check that refused it, and a
`LocatorError`, which can name a tag the page chose, nothing of its own. One
exception is trusted config: a `field` refusal keeps its description of the
field the secret's binding names (`secret_fields.describe_field`), and a
refused binding names its subject contract, which the project config writes
and the page never does. Outside a withheld run every owned reason keeps its
own message, origins included."""

from typing import Literal

from playwright.async_api import Error

from aqa_runner import network, probes
from aqa_runner.browser_session import RefError, UnsupportedVisualFrameError
from aqa_runner.document_origins import (
    REFUSED_BY_KIND,
    DocumentChangedError,
    PolicyEventError,
)
from aqa_runner.egress import EgressRefusedError, EgressUpstreamError
from aqa_runner.locator_generation import BindingRefusedError, LocatorError
from aqa_runner.redaction import Redacted, Redactor, error_text
from aqa_runner.run_record import RefusedWriteError
from aqa_runner.secret_fields import SecretNotFilledError, SecretRefusedError


class UnansweredError(Exception):
    """A look at the page that didn't finish within its budget: Playwright's
    reads of a page take no timeout of their own."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"the page didn't answer within {seconds:g} s")


# What a step or an assertion's look can raise that becomes its reason.
type Raised = (
    Error
    | PolicyEventError
    | DocumentChangedError
    | UnansweredError
    | SecretRefusedError
    | probes.ProbeError
    | probes.ProbeUnstableError
    | EgressRefusedError
    | EgressUpstreamError
    | network.WindowsOverflowError
    | UnsupportedVisualFrameError
    | RefusedWriteError
    | RefError
    | LocatorError
)

# Owned errors whose reason is always this fixed text: a probe's message can
# hold what its endpoint answered, and an egress error's names a host.
_FIXED: tuple[tuple[type[Exception], str], ...] = (
    (probes.ProbeError, "the probe could not be read"),
    (
        UnsupportedVisualFrameError,
        "visible_unoccluded does not support elements inside frames",
    ),
    (
        probes.ProbeUnstableError,
        "the probe value did not stabilize within its read bound",
    ),
    (EgressRefusedError, "the probe request was refused by the egress policy"),
    (EgressUpstreamError, "the probe request could not reach its allowed origin"),
)

# What a withheld run says for each `fill_secret` check that names an origin.
_WITHHELD_CHECKS: dict[
    Literal["destination", "document_origin", "frame_origin"], str
] = {
    "destination": "the page is on none of its destinations",
    "document_origin": "the page's document isn't on its URL's origin",
    "frame_origin": (
        "the field is in a frame off the page's origin, or one the page can't reach"
    ),
}

_ORIGIN_WITHHELD = "(withheld: the run has a test secret)"

# A refused binding's reason, by its verdict (ADR-0025's #53 P13 amendment).
_BINDINGS = {
    "region_absent": "this check's subject binds inside {region}, and nothing on "
    "this page matches it",
    "region_ambiguous": "this check's subject binds inside {region}, and more than "
    "one element on this page matches it",
    "outside_region": "this check's subject binds only inside {region}, and this "
    "element is outside it",
    "part_absent": "this check's subject is the element matching {part} inside "
    "{region}, and that region has none",
    "part_ambiguous": "this check's subject is the one element matching {part} "
    "inside {region}, and that region has several",
    "not_the_part": "this check's subject is the one element matching {part} "
    "inside {region}, and this element is another one (describe_parts lists an "
    "element's parts)",
    "not_leaf": "this check's subject is one value: bind the element that holds "
    "it, not one that contains it",
    "unlisted_copies": "this element is repeated on the page, and no subject "
    "contract in the project config covers this use, so no copy can be bound",
    "too_many": "the page repeats this element too often to tell its copies apart",
}
# Kept short: with it, the reason fits `REASON_CHARS`.
_ACTION_ON_COPIES = (
    "; act under the target_meaning of a planned check whose contract covers it"
)


def reason(error: Raised, redactor: Redactor) -> Redacted:
    """`error` as a result's reason: its owned message, or else
    Playwright's, through `error_text`, which scans either for every value
    `redactor`, the run's, binds. When it binds any, the run withholds: an
    owned message comes from the closed vocabulary, and Playwright's keeps
    only its class and call."""
    withheld = not redactor.empty
    # A refused binding first: its text names only config, while any other
    # LocatorError's is withheld.
    ours = (
        _refused_binding(error)
        if isinstance(error, BindingRefusedError)
        else _owned(error, withheld=withheld)
    )
    return error_text(error, redactor, ours=ours, withheld=withheld)


def _owned(error: Raised, *, withheld: bool) -> str | None:
    """The runner's or the session's own message for `error`, or None for
    Playwright's."""
    for kind, text in _FIXED:
        if isinstance(error, kind):
            return text
    if withheld and isinstance(error, LocatorError):
        return f"no locator could be generated for this element {_ORIGIN_WITHHELD}"
    if withheld and isinstance(error, PolicyEventError):
        refused = REFUSED_BY_KIND[error.event.kind]
        return f"{refused} on an origin the run doesn't allow {_ORIGIN_WITHHELD}"
    if withheld and isinstance(error, SecretRefusedError) and error.check != "field":
        return f"fill_secret refused {error.secret}: {_WITHHELD_CHECKS[error.check]}"
    if isinstance(
        error,
        PolicyEventError
        | DocumentChangedError
        | UnansweredError
        | SecretRefusedError
        | SecretNotFilledError
        | network.WindowsOverflowError
        | RefusedWriteError
        | RefError
        | LocatorError,
    ):
        return str(error)
    return None


def _refused_binding(error: BindingRefusedError) -> str:
    """A refused binding's fixed text, naming its contract's region and part
    (config), whether or not the run withholds."""
    text = _BINDINGS[error.reason]
    if error.contract is not None:
        text = text.format(region=error.contract.region, part=error.contract.part)
    if error.reason == "unlisted_copies" and error.use == "action":
        text += _ACTION_ON_COPIES
    return text
