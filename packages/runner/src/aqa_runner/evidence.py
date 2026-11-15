"""Evidence (ADR-0026, Evidence, and its #50 B amendment): what a run keeps
of each step, under `evidence/<seq>/` in the record it is given, of a heal's
first look, under `heals/<key>/`, and of a verified replay's look for each
`model_verify` check, under `verified/assertion-<index>/`. Replay
(`aqa_runner.executor`), exploring (#53), heal (`aqa_runner.heal`) and the
verifier (`aqa_runner.verification`) all save it here.

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
  failed in `aqa replay`, `aqa run` and `aqa heal <spec>`, heal at its
  first look, and the verifier for each check it would ask about, whose
  image, the `Shot`'s, is the one sent; explore, the bench pilot and a
  completed step never do (ADR-0033's #251 and #256 amendments).
- `before_screenshot.png`, only when its caller passes one in: the page as
  `held_screenshot` captured it ahead of the step, in the same way, held in
  memory until the step's outcome was known. With `--before-screenshots`,
  replay takes one ahead of each compiled step and passes it in only for a
  step that failed; any other step's is dropped (ADR-0033's #314 amendment).

In a run that binds any test secret, the logs keep no URL, method or
console text: a page can split a value across them in pieces no scan finds
(ADR-0026's #47 amendment). Capture serializes no exception or result
object. A file its record refuses for what it holds is left out, and a
screenshot the capture withholds is too, so no page makes evidence decide a
run; a write the disk or a link refuses raises, as a steps line's does."""

from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import Final

from aqa_runner.browser_session import BrowserSession
from aqa_runner.document_origins import (
    DocumentChangedError,
    PolicyEventError,
    Records,
)
from aqa_runner.redaction import Redacted
from aqa_runner.run_record import RefusedWriteError, RunRecord
from aqa_runner.screenshots import (
    Screenshot,
    ScreenshotWithheldError,
    take_screenshot,
)
from aqa_runner.settling import ConsoleEntry, NetworkEntry, Window

# Why a screenshot wasn't taken, in fixed words, when the capture's page
# checks refused it: a policy event's own words name the page's URL.
_OFF_ORIGIN: Final = "the page was off the run's allowed origins"
_FRAMES_CHANGED: Final = "the page's frames changed during the capture"

# A screenshot held in memory until evidence saves or drops it: the image, or
# why it was withheld (`held_screenshot`).
type Held = Screenshot | str


@dataclass(frozen=True)
class Shot:
    """What became of a screenshot evidence was asked for: the file it was
    saved as, by its name in the record, or why it was withheld, in words of
    ours that never quote the page. Only the verifier's, asked for with
    `Asked.sent`, keeps the `Screenshot` its file was written from, the one
    image a verifier is sent (ADR-0024's #256 amendment), out of every repr,
    comparison and document; every other `Shot` keeps no image."""

    path: str | None = None
    withheld: str | None = None
    image: Screenshot | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class Asked:
    """The screenshots a call of `capture_evidence` saves: `screenshot.png`,
    the page after the step, when `after` asks, and `before`, one
    `held_screenshot` took ahead of the step, when given; and with `sent`,
    which only the verifier asks for, the after's `Shot` keeps the
    `Screenshot` it saved, to be sent. No field is named `screenshot`:
    `tests/test_image_exits.py` reads any attribute so spelled in the
    packages' source as an image capture (ADR-0035)."""

    after: bool = False
    before: Held | None = None
    sent: bool = False


@dataclass(frozen=True)
class Shots:
    """What became of the screenshots a call of `capture_evidence` was asked
    for (`Asked`): `screenshot.png`'s and `before_screenshot.png`'s, each
    None when it wasn't."""

    after: Shot | None = None
    before: Shot | None = None


_UNASKED: Final = Asked()


async def capture_evidence(
    record: RunRecord,
    session: BrowserSession,
    folder: str,
    window: Window | None,
    snapshot: Redacted | None,
    *,
    asked: Asked = _UNASKED,
) -> Shots:
    """Save evidence in `folder` of `record`, whose scan must be `session`'s
    (`ValueError` before anything is written). `window` is the step's
    settle window, None when it has none (a drifted step, a failed one
    whose result keeps none, or a heal's look); `snapshot` is the caller's
    first snapshot after the step settled. The screenshot `asked.before`
    holds, which `held_screenshot` took in `session` ahead of the step, is
    saved as `before_screenshot.png`, or, withheld, saves nothing. With
    `asked.after`, the page's screenshot is saved last; its refusal records
    no policy event. What became of each is returned."""
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
    before = asked.before
    ahead = (
        None if before is None else _saved(record, folder, "before_screenshot", before)
    )
    after = await held_screenshot(session) if asked.after else None
    saved = None if after is None else _saved(record, folder, "screenshot", after)
    if saved is not None and asked.sent and not isinstance(after, str):
        saved = replace(saved, image=after)
    return Shots(saved, ahead)


async def held_screenshot(session: BrowserSession) -> Held:
    """The page's screenshot, as `take_screenshot` captures it, or why it was
    withheld, in words of ours: what evidence saves, held until it does.
    Its refusal records no policy event."""
    try:
        return await take_screenshot(session, record_refusal=False)
    except ScreenshotWithheldError as withheld:
        return str(withheld)
    except PolicyEventError:
        return _OFF_ORIGIN
    except DocumentChangedError:
        return _FRAMES_CHANGED


def _saved(record: RunRecord, folder: str, name: str, held: Held) -> Shot:
    """Save `held` in `folder` as `<name>.png`, or nothing when it was
    withheld; what became of it."""
    if isinstance(held, str):
        return Shot(withheld=held)
    path = f"{folder}/{name}.png"
    record.write_screenshot(path, held)
    return Shot(path)


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
