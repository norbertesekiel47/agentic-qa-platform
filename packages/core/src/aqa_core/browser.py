"""Browser settings: the time zone, locale, viewport, scale and color scheme a
run's pages see (ADR-0025)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class BrowserSettings(BaseModel):
    """The settings a run's browser runs under. The defaults are the settings
    pinned for every run. The project config and a spec may override them
    (DATA_MODEL §9), and a compiled script records the settings it was
    explored under, which its replays use (DATA_MODEL §7)."""

    model_config = ConfigDict(frozen=True)

    timezone: str = "UTC"
    locale: str = "en-US"
    viewport: tuple[int, int] = (1280, 800)
    device_scale_factor: float = 1
    color_scheme: Literal["light", "dark"] = "light"
