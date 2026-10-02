"""Browser settings are validated before a browser starts (ADR-0025, #39, #90)."""

import sys
import zoneinfo
from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from aqa_core.browser import BrowserOverrides, BrowserSettings
from pydantic import ValidationError

# A name the host lists and tzdata doesn't, as Ubuntu lists `localtime`.
HOST_ONLY = "Host/Only"
# Each probe, and whether the check accepts it: the answer on every host.
TIME_ZONE_PROBES = {
    "UTC": True,
    "Asia/Kolkata": True,
    "Asia/Calcutta": True,  # an alias Ubuntu leaves out and macOS has
    "America/Buenos_Aires": True,
    "US/Pacific": True,
    "utc": False,
    "localtime": False,
    HOST_ONLY: False,
    "Mars/Phobos": False,
}


def is_time_zone(name: str) -> bool:
    try:
        BrowserSettings(timezone=name)
    except ValidationError:
        return False
    return True


@pytest.fixture
def host_zone_directory(tmp_path: Path) -> Iterator[Path]:
    """A directory that stands in for the host's zone files, empty until a test
    adds some: zoneinfo's search path for the length of the test."""
    original = zoneinfo.TZPATH
    zoneinfo.reset_tzpath(to=[str(tmp_path)])
    yield tmp_path
    zoneinfo.reset_tzpath(to=list(original))


def test_the_pinned_defaults_are_valid() -> None:
    settings = BrowserSettings()

    assert (
        settings.timezone,
        settings.locale,
        settings.viewport,
        settings.device_scale_factor,
        settings.color_scheme,
    ) == ("UTC", "en-US", (1280, 800), 1, "light")


def test_settings_read_from_yaml_are_accepted() -> None:
    # YAML gives a list where the model keeps a tuple.
    settings = BrowserSettings.model_validate(
        {
            "timezone": "Asia/Tokyo",
            "locale": "zh-Hant-TW",
            "viewport": [1440, 900],
            "device_scale_factor": 2,
            "color_scheme": "dark",
        }
    )

    assert settings.viewport == (1440, 900)
    assert settings.locale == "zh-Hant-TW"


@pytest.mark.parametrize(
    ("field", "value", "problem"),
    [
        ("timezon", "UTC", "Extra inputs are not permitted"),
        ("viewport", [0, 0], "greater than or equal to 1"),
        ("viewport", [-1280, 800], "greater than or equal to 1"),
        ("viewport", [1280, 10_001], "less than or equal to 10000"),
        ("viewport", [True, "800"], "valid integer"),
        ("viewport", [1280, 800, 2], "at most 2 items"),
        ("viewport", "1280x800", "valid tuple"),
        ("device_scale_factor", 0, "greater than 0"),
        ("device_scale_factor", -1, "greater than 0"),
        ("device_scale_factor", float("nan"), "finite number"),
        ("device_scale_factor", float("inf"), "finite number"),
        ("device_scale_factor", True, "valid number"),
        ("timezone", "Mars/Phobos", "not an IANA time zone"),
        # Chromium refuses these only when the page opens (LAB_NOTES).
        ("timezone", "utc", "not an IANA time zone"),
        ("timezone", "localtime", "not an IANA time zone"),
        # A name is matched whole, as the IANA database spells it.
        ("timezone", "", "not an IANA time zone"),
        ("timezone", "asia/kolkata", "not an IANA time zone"),
        ("timezone", "Asia/Kolkata\n", "not an IANA time zone"),
        # Chromium accepts every one of these without a word (LAB_NOTES).
        ("locale", "en_US", "not a BCP 47 language tag"),
        ("locale", "english", "not a BCP 47 language tag"),
        ("locale", "", "not a BCP 47 language tag"),
        ("locale", "x", "not a BCP 47 language tag"),
        ("locale", "123", "not a BCP 47 language tag"),
        ("locale", "en-", "not a BCP 47 language tag"),
        ("locale", "en-US\n", "not a BCP 47 language tag"),
        ("color_scheme", "sepia", "'light' or 'dark'"),
    ],
)
def test_an_invalid_setting_is_rejected(field: str, value: Any, problem: str) -> None:
    for model in (BrowserSettings, BrowserOverrides):
        with pytest.raises(ValidationError) as raised:
            model.model_validate({field: value})

        assert {error["loc"][0] for error in raised.value.errors()} == {field}
        assert problem in str(raised.value)


def test_a_refused_time_zone_names_the_value_and_an_example() -> None:
    with pytest.raises(ValidationError) as raised:
        BrowserSettings(timezone="Mars/Phobos")

    [error] = raised.value.errors()
    assert "'Mars/Phobos'" in error["msg"]
    assert "such as Asia/Kolkata" in error["msg"]


@pytest.mark.parametrize(
    "locale",
    ["de", "en-US", "EN-us", "sr-Latn-RS", "es-419", "de-CH-1996", "en-US-x-qa"],
)
def test_a_well_formed_language_tag_is_a_locale(locale: str) -> None:
    assert BrowserSettings(locale=locale).locale == locale


def test_overrides_name_only_the_settings_they_change() -> None:
    overrides = BrowserOverrides.model_validate({"viewport": [1440, 900]})

    assert overrides.model_dump(exclude_none=True) == {"viewport": (1440, 900)}


@pytest.mark.parametrize(
    "host_files",
    [[], ["localtime", HOST_ONLY]],
    ids=[
        "a host with no zone files",
        "a host that lists localtime and a zone of its own",
    ],
)
def test_the_answer_is_the_same_whatever_the_host_lists(
    host_zone_directory: Path, host_files: list[str]
) -> None:
    for name in host_files:
        file = host_zone_directory / name
        file.parent.mkdir(exist_ok=True)
        file.write_bytes(b"TZif" + bytes(40))  # zoneinfo lists a file by this magic

    assert {name: is_time_zone(name) for name in TIME_ZONE_PROBES} == TIME_ZONE_PROBES


def test_a_missing_tzdata_package_is_an_error_not_a_fallback_to_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A first answer with the package present, so a list kept from it can't hide
    # the failure; then an import of None in sys.modules fails as an uninstalled
    # package does.
    BrowserSettings(timezone="UTC")
    monkeypatch.setitem(sys.modules, "tzdata", None)

    with pytest.raises(ModuleNotFoundError, match="tzdata"):
        BrowserSettings(timezone="UTC")


def test_every_name_in_the_tzdata_package_is_a_time_zone() -> None:
    # The package's own file is the reference here. The probes above carry the
    # literals, and the two tests above the cases where the source is wrong.
    names = (
        resources.files("tzdata")
        .joinpath("zones")
        .read_text(encoding="utf-8")
        .splitlines()
    )

    # #90 measured 598 names; the floor only keeps the loop from passing empty.
    assert len(names) > 500
    assert [name for name in names if not is_time_zone(name)] == []
