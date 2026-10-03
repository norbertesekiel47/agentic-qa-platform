"""A run's test secrets (ADR-0026, Test secrets; SECURITY §5): each secret
its spec references, with its value from `AQA_SECRET_<NAME>` and where the
browser may fill it in this run. Explore and replay read them before the
browser starts, and hold the values in memory only, behind pydantic's
`SecretStr`, which prints as asterisks. The browser gets a value only through
`BrowserSession.fill_secret`, never through its environment."""

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass

from aqa_core.config import SecretField
from aqa_core.project import SecretDestination, secret_destinations
from aqa_core.spec import Spec, secret_references
from aqa_core.text import normalize
from playwright.async_api import ElementHandle, Frame
from pydantic import SecretStr

from aqa_runner.locators import name_pattern

# Where a test secret's value comes from, locally and in CI. The prefix keeps
# a spec from pulling an unrelated variable, such as a cloud key, into a field.
PREFIX = "AQA_SECRET_"

# The fields a `password` binding names: <input type="password">, whose type
# HTML reads case-insensitively, as the css engine's `i` flag does.
CONCEALED_INPUTS = 'css=input[type="password" i]'

# Whether `element` is one of `found`, compared as the same node. It runs in
# the page's own world: nothing there can redefine `===`, but `found`
# arrives through it, so this keeps a secret from the wrong field, not from
# the page, which is on one of its destinations.
AMONG = """(element, found) => {
    for (let i = 0; i < found.length; i++) if (found[i] === element) return true;
    return false;
}"""


@dataclass(frozen=True)
class BoundSecret:
    """A test secret this run may fill: its name, its value, which prints as
    `**********` (https://docs.pydantic.dev/2.13/api/types/#pydantic.types.SecretStr),
    and where it may go."""

    name: str
    value: SecretStr
    destination: SecretDestination


class MissingSecretError(Exception):
    """A test secret the spec references has no value: an infrastructure
    error, the environment's rather than the spec's (exit code 12, API.md
    §7). Each problem names the variable to set."""

    exit_code = 12

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = tuple(problems)


def bound_secrets(spec: Spec, start: str) -> dict[str, BoundSecret]:
    """Each test secret `spec` references, bound for a run whose start origin
    is `start`, by name.

    Its destination comes from `aqa_core.project.secret_destinations`, so a
    binding the run doesn't allow is a `SpecError`, raised before any value
    is read. Its value comes from `AQA_SECRET_<NAME>`, and every secret the
    spec references needs one, whichever the run fills. A variable that is
    unset or empty has none: GitHub Actions gives a secret that isn't set as
    an empty string. Every missing value is reported at once, in a
    `MissingSecretError`."""
    destinations = secret_destinations(spec, start)
    # Where the spec first references each secret, for the message.
    where: dict[str, str] = {}
    for key, name in secret_references(spec.frontmatter):
        where.setdefault(name, key)
    bound: dict[str, BoundSecret] = {}
    problems: list[str] = []
    for name, destination in destinations.items():
        variable = PREFIX + name
        value = os.environ.get(variable)
        if value:
            bound[name] = BoundSecret(name, SecretStr(value), destination)
            continue
        fix = (
            f"set it to the value of test secret {name}, which {spec.path} "
            f"references at {where[name]}"
        )
        problems.append(
            f"{variable} is not set: {fix}"
            if value is None
            else f"{variable} is empty: {fix} (GitHub Actions gives a secret "
            "that isn't set as an empty string)"
        )
    if problems:
        raise MissingSecretError(problems)
    return bound


class SecretRefusedError(Exception):
    """fill_secret refused to fill a test secret where its binding doesn't
    allow it: not a policy event, since the page is on an allowed origin, but
    the wrong page or field for this secret. Nothing was filled, and the
    message names the secret, never its value."""


def described(field: SecretField) -> str:
    """The field a binding names, as a refusal says it."""
    if field == "password":
        return 'an <input type="password">'
    return f'the {field.role} named "{field.name}"'


async def field_matches(
    frame: Frame, element: ElementHandle, field: SecretField
) -> bool:
    """Whether `element`, in `frame`, is the field a binding names: an
    `<input type="password">` (`CONCEALED_INPUTS`), or an element whose role
    and accessible name Playwright computes as the binding's, the name
    compared normalized, as a role locator's is (DATA_MODEL §7).

    The fields are found in Playwright's utility world, which the page's
    scripts can't reach: `element_handles` resolves its selector there,
    unlike `evaluate_all`, which resolves it in the page's own world
    (Playwright 1.63's `FrameSelectors._callOnSelectorInternal` and
    `queryArrayInMainWorld`)."""
    if field == "password":
        candidates = frame.locator(CONCEALED_INPUTS)
    else:
        name = normalize(field.name)
        if not name:
            return False  # a name that compares as empty names no field
        # https://playwright.dev/python/docs/api/class-frame#frame-get-by-role
        candidates = frame.get_by_role(field.role, name=re.compile(name_pattern(name)))
    # https://playwright.dev/python/docs/api/class-locator#locator-element-handles
    found = await candidates.element_handles()
    try:
        return bool(await element.evaluate(AMONG, found))
    finally:
        for handle in found:
            await handle.dispose()
