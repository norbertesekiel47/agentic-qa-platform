"""Browser settings are validated before a browser starts (ADR-0025, #39)."""

from typing import Any

import pytest
from aqa_core.browser import BrowserOverrides, BrowserSettings
from pydantic import ValidationError


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


@pytest.mark.parametrize(
    "locale",
    ["de", "en-US", "EN-us", "sr-Latn-RS", "es-419", "de-CH-1996", "en-US-x-qa"],
)
def test_a_well_formed_language_tag_is_a_locale(locale: str) -> None:
    assert BrowserSettings(locale=locale).locale == locale


def test_overrides_name_only_the_settings_they_change() -> None:
    overrides = BrowserOverrides.model_validate({"viewport": [1440, 900]})

    assert overrides.model_dump(exclude_none=True) == {"viewport": (1440, 900)}
