"""`aqa_runner.reasons.reason`, the text a failed step's or an unevaluated
assertion's result carries (ADR-0026's #53 P9+P10 amendment): in a run that
binds a test secret, every reason the runner owns comes from a closed
vocabulary that names no origin, host or port; outside one, each keeps the
message it had before. Every expected value is a literal."""

from typing import Literal

import pytest
from aqa_core.schema import Contract
from aqa_runner import network, probes
from aqa_runner.binding import Refused
from aqa_runner.browser_session import RefError, UnsupportedVisualFrameError
from aqa_runner.document_origins import (
    DocumentChangedError,
    PolicyEvent,
    PolicyEventError,
    PolicyEventKind,
)
from aqa_runner.egress import (
    EgressRefusedError,
    EgressUpstreamError,
    InfrastructureEvent,
    Refusal,
)
from aqa_runner.locator_generation import BindingRefusedError, LocatorError
from aqa_runner.locators import Use
from aqa_runner.reasons import Raised, UnansweredError, reason
from aqa_runner.redaction import NO_SECRETS, Redactor
from aqa_runner.run_record import RefusedWriteError
from aqa_runner.secret_fields import SecretNotFilledError, SecretRefusedError
from playwright.async_api import Error

from packages.runner.tests.result_redaction_fixtures import fake_secret

EVIL = "http://evil.example.test:31337"
APP = "http://app.example.test:8080"
BOUND = Redactor([fake_secret("TEST_PASSWORD", "fake-bound-value", APP)])
WITHHELD = "(withheld: the run has a test secret)"
UNWRITTEN = "steps.jsonl: a bound value's spelling is in it, so it isn't written"
UNRECORDED = (
    "a request's scan failed, so it went unrecorded and no network check can be "
    "evaluated"
)


def policy(kind: PolicyEventKind, origin: str | None = EVIL) -> PolicyEventError:
    return PolicyEventError(PolicyEvent(kind, f"{origin}/path?q=1", origin))


REFUSED_PAGE = SecretRefusedError(
    "TEST_PASSWORD",
    "destination",
    f"fill_secret refused TEST_PASSWORD: the page is on {EVIL}, which isn't one "
    f"of its destinations: {APP}",
)
REFUSED_DOCUMENT = SecretRefusedError(
    "TEST_PASSWORD",
    "document_origin",
    f"fill_secret refused TEST_PASSWORD: the page's document is on {EVIL}, "
    f"though its URL is on {APP}",
)
REFUSED_FRAME = SecretRefusedError(
    "TEST_PASSWORD",
    "frame_origin",
    f"fill_secret refused TEST_PASSWORD: the field is in a frame on {EVIL}, "
    f"not on the page's origin, {APP}",
)
REFUSED_FIELD = SecretRefusedError(
    "TEST_PASSWORD",
    "field",
    'fill_secret refused TEST_PASSWORD: the field isn\'t an <input type="password">',
)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            policy("document"),
            f"the page is on an origin the run doesn't allow {WITHHELD}",
        ),
        (
            policy("frame"),
            f"the element's frame is on an origin the run doesn't allow {WITHHELD}",
        ),
        (
            policy("popup"),
            f"a popup is on an origin the run doesn't allow {WITHHELD}",
        ),
        (
            policy("navigation"),
            f"the URL to navigate to is on an origin the run doesn't allow {WITHHELD}",
        ),
        (
            policy("document", None),
            f"the page is on an origin the run doesn't allow {WITHHELD}",
        ),
        (
            REFUSED_PAGE,
            (
                "fill_secret refused TEST_PASSWORD: the page is on none of its "
                "destinations"
            ),
        ),
        (
            REFUSED_DOCUMENT,
            (
                "fill_secret refused TEST_PASSWORD: the page's document isn't on its "
                "URL's origin"
            ),
        ),
        (
            REFUSED_FRAME,
            (
                "fill_secret refused TEST_PASSWORD: the field is in a frame off the "
                "page's origin, or one the page can't reach"
            ),
        ),
        # Trusted config: the field the binding names, never the page's text.
        (
            REFUSED_FIELD,
            'fill_secret refused TEST_PASSWORD: the field isn\'t an <input type="password">',
        ),
        (
            DocumentChangedError(),
            (
                "the page changed while the session looked at it, so what it saw was "
                "discarded: take a new snapshot, or look the target up again"
            ),
        ),
        (UnansweredError(2), "the page didn't answer within 2 s"),
        (
            SecretNotFilledError("fill_secret: TEST_PASSWORD wasn't filled"),
            "fill_secret: TEST_PASSWORD wasn't filled",
        ),
        (
            network.WindowsOverflowError("settle window 0 kept 100 of 101 responses"),
            "settle window 0 kept 100 of 101 responses",
        ),
        (network.UnrecordedRequestError(), UNRECORDED),
        (probes.ProbeError(f"{EVIL} answered 500"), "the probe could not be read"),
        (
            probes.ProbeUnstableError(EVIL),
            "the probe value did not stabilize within its read bound",
        ),
        (
            EgressRefusedError(Refusal("evil.example.test", 31337, "host", EVIL)),
            "the probe request was refused by the egress policy",
        ),
        (
            EgressUpstreamError(
                InfrastructureEvent("evil.example.test", 31337, "refused")
            ),
            "the probe request could not reach its allowed origin",
        ),
        (
            UnsupportedVisualFrameError(EVIL),
            "visible_unoccluded does not support elements inside frames",
        ),
        (
            RefusedWriteError(UNWRITTEN),
            UNWRITTEN,
        ),
        (
            Error(f"Page.goto: net::ERR_FAILED at {EVIL}/"),
            (
                "Error: Page.goto: the rest is withheld, since the page was handed a "
                "test secret"
            ),
        ),
    ],
)
def test_every_owned_reason_in_a_withheld_run_is_fixed_text(
    error: Raised, expected: str
) -> None:
    shown = reason(error, BOUND)

    assert shown == expected
    assert "evil.example.test" not in shown
    assert "31337" not in shown


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            policy("document"),
            (
                f"the page is on {EVIL}, which isn't one of the run's allowed origins: "
                "nothing there is observed or acted on; navigate to an allowed origin, "
                "or restart"
            ),
        ),
        (
            policy("navigation"),
            (
                f"the URL to navigate to is on {EVIL}, which isn't one of the run's "
                "allowed origins: nothing there is observed or acted on; navigate to an "
                "allowed origin, or restart"
            ),
        ),
        (
            REFUSED_PAGE,
            (
                f"fill_secret refused TEST_PASSWORD: the page is on {EVIL}, which isn't "
                f"one of its destinations: {APP}"
            ),
        ),
        (
            REFUSED_DOCUMENT,
            (
                f"fill_secret refused TEST_PASSWORD: the page's document is on {EVIL}, "
                f"though its URL is on {APP}"
            ),
        ),
        (
            REFUSED_FRAME,
            (
                f"fill_secret refused TEST_PASSWORD: the field is in a frame on {EVIL}, "
                f"not on the page's origin, {APP}"
            ),
        ),
    ],
)
def test_an_owned_reason_outside_a_withheld_run_keeps_its_origin(
    error: Raised, expected: str
) -> None:
    assert reason(error, NO_SECRETS) == expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            policy("frame", None),
            (
                "the element's frame is on no origin a run could allow: nothing there "
                "is observed or acted on; navigate to an allowed origin, or restart"
            ),
        ),
        (
            REFUSED_FIELD,
            'fill_secret refused TEST_PASSWORD: the field isn\'t an <input type="password">',
        ),
        (
            DocumentChangedError(),
            (
                "the page changed while the session looked at it, so what it saw was "
                "discarded: take a new snapshot, or look the target up again"
            ),
        ),
        (UnansweredError(1.5), "the page didn't answer within 1.5 s"),
        (
            SecretNotFilledError("fill_secret: TEST_PASSWORD wasn't filled"),
            "fill_secret: TEST_PASSWORD wasn't filled",
        ),
        (
            network.WindowsOverflowError("settle window 2 kept 100 of 140 responses"),
            "settle window 2 kept 100 of 140 responses",
        ),
        (network.UnrecordedRequestError(), UNRECORDED),
        (probes.ProbeError("answered 500"), "the probe could not be read"),
        (
            probes.ProbeUnstableError("unstable"),
            "the probe value did not stabilize within its read bound",
        ),
        (
            EgressRefusedError(Refusal("evil.example.test", 31337, "host", EVIL)),
            "the probe request was refused by the egress policy",
        ),
        (
            EgressUpstreamError(
                InfrastructureEvent("evil.example.test", 31337, "refused")
            ),
            "the probe request could not reach its allowed origin",
        ),
        (
            UnsupportedVisualFrameError("framed"),
            "visible_unoccluded does not support elements inside frames",
        ),
        (RefusedWriteError(UNWRITTEN), UNWRITTEN),
        (
            Error("Page.evaluate: Target crashed\n    at the page's own line"),
            "Error: Page.evaluate: Target crashed",
        ),
    ],
)
def test_outside_a_withheld_run_every_reason_is_presented_as_before(
    error: Raised, expected: str
) -> None:
    assert reason(error, NO_SECRETS) == expected


COUNT = Contract(region="div.banner", part="span.counter", leaf=True)
SUBJECT = "this check's subject"


@pytest.mark.parametrize(
    ("refused", "contract", "use", "expected"),
    [
        (
            "region_absent",
            COUNT,
            "assertion",
            f"{SUBJECT} binds inside div.banner, and nothing on this page matches it",
        ),
        (
            "region_ambiguous",
            COUNT,
            "action",
            (
                f"{SUBJECT} binds inside div.banner, and more than one element on "
                "this page matches it"
            ),
        ),
        (
            "outside_region",
            COUNT,
            "action",
            f"{SUBJECT} binds only inside div.banner, and this element is outside it",
        ),
        (
            "part_absent",
            COUNT,
            "negative_check",
            (
                f"{SUBJECT} is the element matching span.counter inside div.banner, "
                "and that region has none"
            ),
        ),
        (
            "part_ambiguous",
            COUNT,
            "assertion",
            (
                f"{SUBJECT} is the one element matching span.counter inside "
                "div.banner, and that region has several"
            ),
        ),
        (
            "not_the_part",
            COUNT,
            "assertion",
            (
                f"{SUBJECT} is the one element matching span.counter inside "
                "div.banner, and this element is another one (describe_parts lists "
                "an element's parts)"
            ),
        ),
        (
            "not_leaf",
            COUNT,
            "assertion",
            (
                f"{SUBJECT} is one value: bind the element that holds it, not one "
                "that contains it"
            ),
        ),
        (
            "unlisted_copies",
            None,
            "assertion",
            (
                "this element is repeated on the page, and no subject contract in the "
                "project config covers this use, so no copy can be bound"
            ),
        ),
        (
            "unlisted_copies",
            None,
            "action",
            (
                "this element is repeated on the page, and no subject contract in the "
                "project config covers this use, so no copy can be bound; act under "
                "the target_meaning of a planned check whose contract covers it"
            ),
        ),
        (
            "too_many",
            None,
            "action",
            "the page repeats this element too often to tell its copies apart",
        ),
    ],
)
def test_a_binding_refusal_is_fixed_text_naming_its_contract(
    refused: Literal[
        "region_absent",
        "region_ambiguous",
        "outside_region",
        "part_absent",
        "part_ambiguous",
        "not_the_part",
        "not_leaf",
        "unlisted_copies",
        "too_many",
    ],
    contract: Contract | None,
    use: Use,
    expected: str,
) -> None:
    error = BindingRefusedError(
        "the favorites count, which the page may name", Refused(refused), contract, use
    )

    assert (reason(error, BOUND), reason(error, NO_SECRETS)) == (expected, expected)
    assert str(error) == f"the favorites count, which the page may name: {refused}"


# What a generator's LocatorError can carry: the meaning the model wrote,
# and the element's tag, which the page chose.
CUSTOM_TAG = LocatorError(
    "the article's save button: no locator of the grammar finds the "
    "x-fake-tag-for-53 element alone for an action"
)


def test_a_stale_ref_keeps_its_words_and_a_locator_error_its_own_only_unbound() -> None:
    stale = RefError(
        "'e1' is from an older snapshot: a ref acts only on the snapshot that gave "
        "it, so take a new snapshot"
    )

    assert [
        reason(stale, BOUND),
        reason(stale, NO_SECRETS),
        reason(CUSTOM_TAG, BOUND),
        reason(CUSTOM_TAG, NO_SECRETS),
    ] == [
        str(stale),
        str(stale),
        (
            "no locator could be generated for this element (withheld: the run has "
            "a test secret)"
        ),
        (
            "the article's save button: no locator of the grammar finds the "
            "x-fake-tag-for-53 element alone for an action"
        ),
    ]
