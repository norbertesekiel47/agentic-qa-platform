"""A run's test secrets (ADR-0026, Test secrets; SECURITY §5): each secret
its spec references, with its value from `AQA_SECRET_<NAME>` and where the
browser may fill it in this run. Explore and replay read them before the
browser starts, and hold the values in memory only, behind pydantic's
`SecretStr`, which prints as asterisks. The browser gets a value only through
`BrowserSession.fill_secret`, never through its environment."""

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from aqa_core.project import SecretDestination, secret_destinations
from aqa_core.spec import Spec, secret_references
from pydantic import SecretStr

# Where a test secret's value comes from, locally and in CI. The prefix keeps
# a spec from pulling an unrelated variable, such as a cloud key, into a field.
PREFIX = "AQA_SECRET_"

# What separates the names in `DEBUG`: commas, and what JavaScript's `\s`
# matches, U+FEFF included and U+001C to U+001F and U+0085 not, as the
# driver's `debug` package splits them.
_DEBUG_SEPARATORS = re.compile(
    "[,\t\n\x0b\x0c\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+"
)


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


class SecretLoggedError(Exception):
    """The environment turns on Playwright's protocol logging, which would
    print a test secret's value as the fill sends it: an infrastructure
    error, as a missing value is (exit code 12, API.md §7)."""

    exit_code = 12


def bound_secrets(spec: Spec, start: str) -> dict[str, BoundSecret]:
    """Each test secret `spec` references, bound for a run whose start origin
    is `start`, by name.

    Its destination comes from `aqa_core.project.secret_destinations`, so a
    binding the run doesn't allow is a `SpecError`, raised before any value
    is read. Its value comes from `AQA_SECRET_<NAME>`, and every secret the
    spec references needs one, whichever the run fills. A variable that is
    unset or empty has none: GitHub Actions gives a secret that isn't set as
    an empty string. Every missing value is reported at once, in a
    `MissingSecretError`.

    Nor are secrets bound where Playwright would log the messages that carry
    a value (`SecretLoggedError`): `DEBUGP` makes its Python client print
    every protocol message, and a `DEBUG` that turns on `pw:protocol` makes
    its driver, which inherits the runner's environment, log them."""
    destinations = secret_destinations(spec, start)
    if destinations and (logged := _protocol_logging(os.environ)) is not None:
        raise SecretLoggedError(
            f"{logged}: Playwright would log every protocol message, a test "
            "secret's value among them; unset it to run with test secrets"
        )
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


def _protocol_logging(environ: Mapping[str, str]) -> str | None:
    """What in `environ` makes Playwright log a value, as a refusal says it;
    None when nothing does. `DEBUGP`, set to anything, makes its Python
    client print every protocol message (`playwright/_impl/_transport.py`).
    `DEBUG` turning on `pw:protocol` makes its driver log them, and turning
    on `pw:browser` makes it print the browser's stderr, where the headless
    shell writes the page's console messages (#49's security re-review).
    `DEBUG` is read as the driver's `debug` package reads it: names split at
    commas and JavaScript's whitespace, `*` matching anything, a name
    starting `-` turning one off, and an off winning (Playwright 1.63's
    `utilsBundle.js`, `debugLogger.isEnabled`)."""
    if "DEBUGP" in environ:
        return "DEBUGP is set"
    names = [name for name in _DEBUG_SEPARATORS.split(environ.get("DEBUG", "")) if name]
    for logged in ("pw:protocol", "pw:browser"):
        off = any(_names(name[1:], logged) for name in names if name.startswith("-"))
        on = any(_names(name, logged) for name in names if not name.startswith("-"))
        if on and not off:
            return f"DEBUG turns on {logged}"
    return None


def _names(name: str, logged: str) -> bool:
    """Whether `name`, one of `DEBUG`'s, names the debug name `logged`."""
    return re.fullmatch(re.escape(name).replace(r"\*", ".*?"), logged) is not None
