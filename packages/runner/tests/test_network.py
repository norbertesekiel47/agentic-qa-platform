import asyncio
from typing import Literal

import pytest
from aqa_core.compiled import NetworkNone, NetworkSeen
from aqa_runner import network
from aqa_runner.settling import Exchange, Window


def check(
    kind: Literal["network_seen", "network_none"] = "network_seen",
) -> NetworkSeen | NetworkNone:
    model = NetworkSeen if kind == "network_seen" else NetworkNone
    return model.model_validate(
        {
            "id": "a1",
            "expect_index": 0,
            "check": kind,
            "method": "POST",
            "url_pattern": r"^https://app\.test/api/orders(?:\?|$)",
            "status_class": "2xx",
        }
    )


def window(*responses: Exchange) -> Window:
    made = Window()
    for response in responses:
        made.responses.add(response)
    return made


@pytest.mark.parametrize(
    ("method", "url", "status", "seen"),
    [
        ("POST", "https://app.test/api/orders?attempt=1", 201, True),
        ("POST", "https://app.test/api/orders", 200, True),
        ("POST", "https://app.test/api/orders", 299, True),
        ("GET", "https://app.test/api/orders", 201, False),
        ("post", "https://app.test/api/orders", 201, False),
        ("POST", "https://app.test/api/orders", 199, False),
        ("POST", "https://app.test/api/orders", 300, False),
        ("POST", "https://app.test/api/orders", 500, False),
        ("POST", "https://elsewhere.test/api/orders", 201, False),
        ("POST", "https://app.test/api/orders/count", 201, False),
    ],
)
def test_network_seen_matches_method_url_pattern_and_status_class(
    method: str,
    url: str,
    status: int,
    seen: bool,
) -> None:
    windows = (
        window(Exchange("GET", "https://app.test/", 200)),
        window(Exchange(method, url, status)),
    )

    assert asyncio.run(network.held(check(), windows)) is seen


def test_network_none_fails_on_a_matching_response() -> None:
    windows = (window(Exchange("POST", "https://app.test/api/orders", 201)),)

    assert asyncio.run(network.held(check("network_none"), windows)) is False


@pytest.mark.parametrize(
    ("kind", "held"), [("network_seen", False), ("network_none", True)]
)
def test_no_browser_responses_establishes_absence(
    kind: Literal["network_seen", "network_none"],
    held: bool,
) -> None:
    assert asyncio.run(network.held(check(kind), (Window(),))) is held


@pytest.mark.parametrize("kind", ["network_seen", "network_none"])
def test_a_check_the_kept_responses_dont_decide_raises_when_a_window_kept_too_few(
    kind: Literal["network_seen", "network_none"],
) -> None:
    overflowed = window()
    for _ in range(101):
        overflowed.responses.add(Exchange("GET", "https://app.test/cart", 200))

    with pytest.raises(
        network.WindowsOverflowError, match="window 1 kept 100 of 101 responses"
    ):
        asyncio.run(network.held(check(kind), (Window(), overflowed)))


@pytest.mark.parametrize(
    ("kind", "held"), [("network_seen", True), ("network_none", False)]
)
def test_a_kept_match_decides_even_when_an_earlier_window_overflowed(
    kind: Literal["network_seen", "network_none"],
    held: bool,
) -> None:
    overflowed = window()
    for _ in range(101):
        overflowed.responses.add(Exchange("GET", "https://app.test/cart", 200))
    matching = window(Exchange("POST", "https://app.test/api/orders", 201))

    assert asyncio.run(network.held(check(kind), (overflowed, matching))) is held
