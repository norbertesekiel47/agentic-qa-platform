"""A step a repair inserted shows, as it runs, that it changed nothing (#246;
ADR-0024's #246 amendment): its own settle window ended `idle`, the egress
proxy heard every write and read none, no tunnel was open, and the page's main
frame didn't navigate. Otherwise the step fails and the run stops,
`inconclusive`, with no proposal. The rule alone, on hand-made windows."""

import pytest
from aqa_runner.redaction import NO_SECRETS, Redactor
from aqa_runner.repairs import inserted_failure
from aqa_runner.settling import Settled, Window

from packages.runner.tests.result_redaction_fixtures import fake_secret

# The reason's opening, and each fault's phrase, in the order a reason lists them.
UNSHOWN = "a repair inserted this step, and it didn't show it changed nothing: "
BUSY = "settle: the page was still busy when settling's wait ran out"
UNHEARD = (
    "network: the egress proxy didn't hear every write from the step's dispatch on"
)
WROTE = "network: a write request"
TUNNEL = (
    "network: an encrypted or WebSocket connection was open, which the egress "
    "proxy can't read"
)
NAVIGATED = "page: the page's main frame navigated"


@pytest.mark.parametrize(
    ("window", "settled", "phrase"),
    [
        pytest.param(Window(heard=True), "timeout", BUSY, id="busy"),
        pytest.param(Window(heard=False), "idle", UNHEARD, id="unheard"),
        pytest.param(Window(heard=True, wrote=True), "idle", WROTE, id="wrote"),
        pytest.param(
            Window(heard=True, tunnelled=True), "idle", TUNNEL, id="tunnelled"
        ),
        pytest.param(
            Window(heard=True, navigated=True), "idle", NAVIGATED, id="navigated"
        ),
    ],
)
def test_each_fault_alone_fails_an_inserted_step_with_its_phrase(
    window: Window, settled: Settled, phrase: str
) -> None:
    assert inserted_failure(window, settled, NO_SECRETS) == UNSHOWN + phrase


def test_an_idle_window_that_heard_everything_and_saw_nothing_passes() -> None:
    assert inserted_failure(Window(heard=True), "idle", NO_SECRETS) is None


def test_several_faults_are_listed_in_a_fixed_order() -> None:
    window = Window(heard=False, wrote=True, tunnelled=True, navigated=True)

    assert inserted_failure(window, "timeout", NO_SECRETS) == (
        UNSHOWN + f"{BUSY}; {UNHEARD}; {WROTE}; {TUNNEL}; {NAVIGATED}"
    )


def test_the_reason_is_scanned_with_the_sessions_redactor() -> None:
    redactor = Redactor([fake_secret("TEST_PASSWORD", "request", "http://app.test")])

    assert inserted_failure(Window(heard=True, wrote=True), "idle", redactor) == (
        UNSHOWN + "network: a write [SECRET:TEST_PASSWORD]"
    )
