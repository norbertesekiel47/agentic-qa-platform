"""Browser settings: the time zone, locale, viewport, scale and color scheme a
run's pages see (ADR-0025)."""

import re
from importlib import resources
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, StrictInt

from aqa_core.schema import PositiveNumber, StrictModel

# A well-formed RFC 5646 language tag whose language is an ISO 639 code (two
# or three letters), so "english" and "x" fail. Playwright 1.63's Chromium accepts any
# locale string without an error (LAB_NOTES, 2026-10-01), so this is the only
# check a locale gets.
_LANGUAGE_TAG = re.compile(
    r"""
    [a-z]{2,3}(?:-[a-z]{3}){0,3}                 # language, extended language
    (?:-[a-z]{4})?                               # script
    (?:-(?:[a-z]{2}|[0-9]{3}))?                  # region
    (?:-(?:[a-z0-9]{5,8}|[0-9][a-z0-9]{3}))*     # variants
    (?:-[0-9a-wy-z](?:-[a-z0-9]{2,8})+)*         # extensions
    (?:-x(?:-[a-z0-9]{1,8})+)?                   # private use
    """,
    re.IGNORECASE | re.VERBOSE,
)


def time_zones() -> frozenset[str]:
    """The time zones a setting may name: the names in the tzdata package's own
    `zones` file, one per line, aliases such as Asia/Calcutta included, so every
    machine gives the same answer (ADR-0025, 2026-10-02 amendment).

    Never `zoneinfo.available_timezones()`, which adds the host's zone files
    (LAB_NOTES, 2026-10-02). A missing package raises, with no fallback to the
    host's list."""
    zones = resources.files("tzdata").joinpath("zones").read_text(encoding="utf-8")
    return frozenset(zones.splitlines())


def _time_zone(name: str) -> str:
    # By name, never zoneinfo.ZoneInfo(name): that also opens files such as
    # "localtime", and on a case-insensitive file system it finds "utc".
    # Chromium refuses both, but only once a page opens.
    if name not in time_zones():
        raise ValueError(
            f"'{name}' is not an IANA time zone in the tzdata package's list: write a "
            "name as the IANA database spells it, such as Asia/Kolkata"
        )
    return name


def _locale(tag: str) -> str:
    if not _LANGUAGE_TAG.fullmatch(tag):
        raise ValueError(f"'{tag}' is not a BCP 47 language tag such as en-US")
    return tag


TimeZone = Annotated[str, AfterValidator(_time_zone)]
Locale = Annotated[str, AfterValidator(_locale)]
# 10,000 CSS pixels is above an 8K display (7,680), so a larger side is a typo.
# CDP itself accepts up to 10,000,000.
_Side = Annotated[StrictInt, Field(ge=1, le=10_000)]
# Not strict on the outside, so the list YAML gives becomes the tuple; each side
# stays strict, so True and "800" are refused.
Viewport = Annotated[tuple[_Side, _Side], Field(strict=False)]
ScaleFactor = PositiveNumber
ColorScheme = Literal["light", "dark"]


class BrowserSettings(StrictModel):
    """The settings a run's browser runs under. The defaults are the settings
    pinned for every run. The project config and a spec may override them
    (DATA_MODEL §9), and a compiled script records the settings it was
    explored under, which its replays use (DATA_MODEL §7)."""

    timezone: TimeZone = "UTC"
    locale: Locale = "en-US"
    viewport: Viewport = (1280, 800)
    device_scale_factor: ScaleFactor = 1
    color_scheme: ColorScheme = "light"


class BrowserOverrides(StrictModel):
    """A project's or a spec's `browser:`: the settings it changes, each
    validated as in BrowserSettings. None leaves a setting as it was."""

    timezone: TimeZone | None = None
    locale: Locale | None = None
    viewport: Viewport | None = None
    device_scale_factor: ScaleFactor | None = None
    color_scheme: ColorScheme | None = None
