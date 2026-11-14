"""Evidence (ADR-0026, Evidence, and its #50 B amendment): what a run keeps
of each step, under `evidence/<seq>/` in the record it is given, and of a heal's
first look, under `heals/<key>/`. Replay (`aqa_runner.executor`), exploring
(#53) and heal (`aqa_runner.heal`) all save it here.

- `a11y_snapshot.yaml`: the caller's own scanned snapshot of the page after
  the step, the one a model is shown, so saving it takes no look of its own
  and changes no ref. Left out when the caller has none.
- `console_log.json` and `network_log.json`: the step's window's console
  messages and request metadata as they stand when this runs, after the
  step settled and before the next action. A request or message later in
  the window is in the result's window, not here.
- `screenshot.png`, only when its caller asks: the page as `take_screenshot`
  captures it, masked, and OCR-checked in a run that binds a secret
  (ADR-0034, ADR-0035). Replay asks for one at a step that drifted or
  failed in `aqa replay`, `aqa run` and `aqa heal <spec>`, and heal at its
  first look; explore, the bench pilot and a completed step never do
  (ADR-0033's #251 amendment).

In a run that binds any test secret, the logs keep no URL, method or
console text: a page can split a value across them in pieces no scan finds
(ADR-0026's #47 amendment). Capture serializes no exception or result
object. A file its record refuses for what it holds is left out, and a
screenshot the capture withholds is too, so no page makes evidence decide a
run; a write the disk or a link refuses raises, as a steps line's does."""

from contextlib import suppress
from dataclasses import dataclass
from typing import Final

from aqa_runner.browser_session import BrowserSession
from aqa_runner.document_origins import (
    DocumentChangedError,
    PolicyEventError,
    Records,
)
from aqa_runner.redaction import Redacted
from aqa_runner.run_record import RefusedWriteError, RunRecord
from aqa_runner.screenshots import ScreenshotWithheldError, take_screenshot
from aqa_runner.settling import ConsoleEntry, NetworkEntry, Window

# Why a screenshot wasn't taken, in fixed words, when the capture's page
# checks refused it: a policy event's own words name the page's URL.
_OFF_ORIGIN: Final = "the page was off the run's allowed origins"
_FRAMES_CHANGED: Final = "the page's frames changed during the capture"


@dataclass(frozen=True)
class Shot:
    """What became of a screenshot evidence was asked for: the file it was
    saved as, by its name in the record, or why it was withheld, in words of
    ours that never quote the page."""

    path: str | None = None
    withheld: str | None = None


async def capture_evidence(
    record: RunRecord,
    session: BrowserSession,
    folder: str,
    window: Window | None,
    snapshot: Redacted | None,
    *,
    screenshot: bool = False,
) -> Shot | None:
    """Save evidence in `folder` of `record`, whose scan must be `session`'s
    (`ValueError` before anything is written). `window` is the step's
    settle window, None when it has none (a drifted step, a failed one
    whose result keeps none, or a heal's look); `snapshot` is the caller's
    first snapshot after the step settled. With `screenshot`, the page's
    screenshot is saved last, and what became of it is returned; its
    refusal records no policy event. Without it, None."""
    if record.redactor is not session.redactor:
        raise ValueError(
            "evidence goes only to a record that scans with its session's redactor"
        )
    withheld = not record.redactor.empty
    if snapshot is not None:
        with suppress(RefusedWriteError):
            record.write_text(f"{folder}/a11y_snapshot.yaml", snapshot)
    console: Records[ConsoleEntry] = Records()
    network: Records[NetworkEntry] = Records()
    if window is not None:
        console, network = (
            session.console_log(window),
            await session.network_log(window),
        )
    documents = {
        "console_log.json": {
            "entries": [
                _console_entry(entry, withheld=withheld) for entry in console.kept
            ],
            "total": console.total,
        },
        "network_log.json": {
            "entries": [
                _network_entry(entry, withheld=withheld) for entry in network.kept
            ],
            "total": network.total,
        },
    }
    for name, document in documents.items():
        with suppress(RefusedWriteError):
            record.write(f"{folder}/{name}", document)
    return await _screenshot(record, session, folder) if screenshot else None


async def _screenshot(record: RunRecord, session: BrowserSession, folder: str) -> Shot:
    try:
        shot = await take_screenshot(session, record_refusal=False)
    except ScreenshotWithheldError as withheld:
        return Shot(withheld=str(withheld))
    except PolicyEventError:
        return Shot(withheld=_OFF_ORIGIN)
    except DocumentChangedError:
        return Shot(withheld=_FRAMES_CHANGED)
    name = f"{folder}/screenshot.png"
    record.write_screenshot(name, shot)
    return Shot(name)


def _console_entry(entry: ConsoleEntry, *, withheld: bool) -> dict[str, object]:
    return (
        {"type": entry.type} if withheld else {"type": entry.type, "text": entry.text}
    )


def _network_entry(entry: NetworkEntry, *, withheld: bool) -> dict[str, object]:
    kept: dict[str, object] = {
        "status": entry.status,
        "timing": {"start": entry.start, "duration_ms": entry.duration_ms},
        "size": entry.size,
    }
    return kept if withheld else {"method": entry.method, "url": entry.url, **kept}
