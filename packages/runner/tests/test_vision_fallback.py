"""Heal's vision fallback request and its one tool (#252; ADR-0002's #252
amendment): a request holds only a drifted target and one `Screenshot`, the
model's only output is a point, and the send check reads the request's text,
not its image's data. Real Chromium makes each screenshot; the wire request goes
to a local stand-in for the provider, with a fake key."""

import asyncio
import json
from base64 import b64decode, b64encode
from collections.abc import Iterator
from typing import Any, cast

import pytest
from aqa_core.compiled import ByCss, ByRole, Locator
from aqa_core.config import ProjectConfig
from aqa_core.model_roles import resolve_roles
from aqa_core.price_map import vendored
from aqa_runner.anthropic_client import AnthropicClient
from aqa_runner.healer import heal_request
from aqa_runner.model_router import Routed
from aqa_runner.model_turns import UnsentRequestError, unspelled
from aqa_runner.redaction import NO_SECRETS, Redactor
from aqa_runner.screenshots import Screenshot, take_screenshot
from aqa_runner.vision_fallback import (
    VISION_INSTRUCTIONS,
    VisionClick,
    clicked,
    vision_request,
    vision_tools,
)
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from packages.runner.tests.document_fixtures import Sites, browsing, serving_sites
from packages.runner.tests.navigator_fixtures import bound
from packages.runner.tests.provider_stand_in import called, standing_in
from packages.runner.tests.secret_fixtures import FAKE_VALUE

MEANING = "the button that shows the order's details"
LOCATORS: list[Locator] = [
    ByRole(role="button", name="Show details"),
    ByCss(css="#toolbar button"),
]


@pytest.fixture
def sites(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sites]:
    with serving_sites(monkeypatch) as served:
        yield served


def requested(
    sites: Sites, redactor: Redactor | None = None
) -> tuple[Screenshot, list[BaseMessage]]:
    """A screenshot of a page and the vision request built from it, with the
    session's scan unless `redactor` is another."""

    async def scenario() -> tuple[Screenshot, list[BaseMessage]]:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/kept")
            shot = await take_screenshot(session)
            scan = session.redactor if redactor is None else redactor
            messages = vision_request(
                MEANING, LOCATORS, shot, session.settings.viewport, redactor=scan
            )
            return shot, messages

    return asyncio.run(scenario())


def images(message: BaseMessage) -> list[dict[str, Any]]:
    return [
        part
        for part in message.content
        if isinstance(part, dict) and part.get("type") == "image"
    ]


def test_a_vision_request_takes_only_a_screenshot() -> None:
    raw = cast(Screenshot, b"\x89PNG\r\n\x1a\n")

    with pytest.raises(TypeError, match="Screenshot"):
        vision_request(MEANING, LOCATORS, raw, (1280, 800), redactor=NO_SECRETS)


def test_a_vision_request_refuses_a_screenshot_another_scan_made(
    sites: Sites,
) -> None:
    with pytest.raises(ValueError, match="another"):
        requested(sites, Redactor([]))


def test_a_vision_request_holds_the_target_the_images_size_and_one_png(
    sites: Sites,
) -> None:
    shot, [system, human] = requested(sites)

    assert isinstance(system, SystemMessage)
    assert system.content == VISION_INSTRUCTIONS
    [image] = images(human)
    assert image == {
        "type": "image",
        "base64": image["base64"],
        "mime_type": "image/png",
    }
    assert b64decode(image["base64"]) == shot.png
    [_, ours] = heal_request(
        MEANING, LOCATORS, "", NO_SECRETS.redact(""), redactor=NO_SECRETS
    )
    target = ours.text[: ours.text.index("</target>") + len("</target>")]
    assert human.text.startswith(f"{target}\n\n<screenshot>\n")
    assert "1280" in human.text
    assert "800" in human.text
    assert "<snapshot>" not in human.text
    assert "<results>" not in human.text


def test_the_send_check_reads_a_requests_text_and_not_its_images_data() -> None:
    check = unspelled(Redactor([bound("http://127.0.0.1", FAKE_VALUE)]))
    image = {"type": "image", "base64": FAKE_VALUE, "mime_type": "image/png"}

    check([SystemMessage(content="instructions"), HumanMessage(content=[image])])
    for leaked in (
        [{"type": "text", "text": FAKE_VALUE}, image],
        [{**image, "mime_type": FAKE_VALUE}],
    ):
        with pytest.raises(UnsentRequestError):
            check([HumanMessage(content=cast(list[str | dict[str, Any]], leaked))])
    with pytest.raises(UnsentRequestError):
        check([SystemMessage(content=FAKE_VALUE), HumanMessage(content=[image])])


def test_a_vision_request_reaches_the_provider_as_a_png_and_one_strict_tool(
    sites: Sites, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = resolve_roles(ProjectConfig(), vendored())["vision_fallback"].model
    shot, messages = requested(sites)

    with standing_in(monkeypatch) as start:
        stand_in = start([called(1, "vision_click", {"x": 96, "y": 36})])
        client = AnthropicClient(model, None)
        answered = asyncio.run(client.call(messages, vision_tools(), None))

    [sent] = stand_in.sent
    body = json.loads(sent)
    [human] = body["messages"]
    assert [part for part in human["content"] if part["type"] == "image"] == [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": b64encode(shot.png).decode("ascii"),
            },
        }
    ]
    assert [(tool["name"], tool["strict"]) for tool in body["tools"]] == [
        ("vision_click", True)
    ]
    assert body["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    routed = Routed(answered.message, None, "ok", ())
    assert clicked(routed) == VisionClick(x=96, y=36)


def test_vision_falls_back_with_one_tool_that_takes_a_point_only() -> None:
    [tool] = vision_tools()

    assert tool.name == "vision_click"
    assert tool.args_schema is VisionClick
    schema = VisionClick.model_json_schema()
    assert set(schema["properties"]) == {"x", "y"}
    assert schema["additionalProperties"] is False


def reply(message: AIMessage, outcome: Any = "ok") -> Routed:
    return Routed(message, None, outcome, ())


def calling(name: str, args: dict[str, object]) -> Routed:
    return reply(
        AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "c1"}])
    )


def test_a_vision_click_is_the_point_it_names() -> None:
    assert clicked(calling("vision_click", {"x": 96, "y": 36})) == VisionClick(
        x=96, y=36
    )


def test_a_reply_that_calls_no_tool_names_no_point() -> None:
    assert clicked(reply(AIMessage(content="nothing fits"))) is None


CANARY = "canary-only-the-model-wrote"


@pytest.mark.parametrize(
    ("routed", "why"),
    [
        pytest.param(
            calling("vision_click", {"x": 1, "y": 2, "locator": CANARY}),
            "the call to vision_click was not run: its arguments don't fit the tool",
            id="a_locator",
        ),
        pytest.param(
            calling("vision_click", {"x": CANARY, "y": 2}),
            "the call to vision_click was not run: its arguments don't fit the tool",
            id="x_as_a_string",
        ),
        pytest.param(
            calling("vision_click", {"x": True, "y": 2}),
            "the call to vision_click was not run: its arguments don't fit the tool",
            id="x_as_a_bool",
        ),
        pytest.param(
            calling("rebind", {"ref": CANARY}),
            "the reply called a tool other than vision_click, so nothing ran",
            id="another_tool",
        ),
        pytest.param(
            reply(
                AIMessage(
                    content="",
                    invalid_tool_calls=[
                        {"name": CANARY, "args": CANARY, "id": "x", "error": CANARY}
                    ],
                )
            ),
            "a tool call in the reply was malformed, so nothing ran",
            id="malformed",
        ),
        pytest.param(
            reply(AIMessage(content=CANARY), "invalid"),
            "the reply's outcome was invalid, so nothing ran",
            id="invalid",
        ),
    ],
)
def test_a_vision_reply_that_cant_run_says_why_in_its_own_words(
    routed: Routed, why: str
) -> None:
    unfit = clicked(routed)

    assert not isinstance(unfit, VisionClick)
    assert unfit is not None
    assert unfit.why == why
    assert CANARY not in json.dumps(unfit.why)
