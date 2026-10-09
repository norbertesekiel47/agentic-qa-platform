"""`aqa_runner.reasons.reason`, the text a failed step's or an unevaluated
assertion's result carries (ADR-0026's #53 P9+P10 amendment): in a run that
binds a test secret, every reason the runner owns comes from a closed
vocabulary that names no origin, host or port; outside one, each keeps the
message it had before. Every expected value is a literal."""

import pytest
from aqa_runner import network, probes
from aqa_runner.browser_session import UnsupportedVisualFrameError
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
from aqa_runner.reasons import Raised, UnansweredError, reason
from aqa_runner.redaction import NO_SECRETS, Redactor
from aqa_runner.secret_fields import SecretNotFilledError, SecretRefusedError
from playwright.async_api import Error

from packages.runner.tests.result_redaction_fixtures import fake_secret

EVIL = "http://evil.example.test:31337"
APP = "http://app.example.test:8080"
BOUND = Redactor([fake_secret("TEST_PASSWORD", "fake-bound-value", APP)])
WITHHELD = "(withheld: the run has a test secret)"


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
    shown = reason(error, BOUND, withheld=True)

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
    assert reason(error, NO_SECRETS, withheld=False) == expected


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
        (
            Error("Page.evaluate: Target crashed\n    at the page's own line"),
            "Error: Page.evaluate: Target crashed",
        ),
    ],
)
def test_outside_a_withheld_run_every_reason_is_presented_as_before(
    error: Raised, expected: str
) -> None:
    assert reason(error, NO_SECRETS, withheld=False) == expected
