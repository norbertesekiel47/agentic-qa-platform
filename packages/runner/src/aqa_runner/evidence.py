"""Evidence (ADR-0026, Evidence, and its #50 B amendment): what a run keeps
of each step, under `evidence/<seq>/` in the record it is given. Replay
(`aqa_runner.executor`) and exploring (#53) both save it here.

- `a11y_snapshot.yaml`: the caller's own scanned snapshot of the page after
  the step, the one a model is shown, so saving it takes no look of its own
  and changes no ref. Left out when the caller has none.
- `console_log.json` and `network_log.json`: the step's window's console
  messages and request metadata as they stand when this runs, after the
  step settled and before the next action. A request or message later in
  the window is in the result's window, not here.

In a run that binds any test secret, the logs keep no URL, method or
console text: a page can split a value across them in pieces no scan finds
(ADR-0026's #47 amendment). Capture serializes no exception or result
object. A file its record refuses for what it holds is left out, so no page
makes evidence decide a run; a write the disk or a link refuses raises, as
a steps line's does."""

from contextlib import suppress

from aqa_runner.browser_session import BrowserSession
from aqa_runner.document_origins import Records
from aqa_runner.redaction import Redacted
from aqa_runner.run_record import RefusedWriteError, RunRecord
from aqa_runner.settling import ConsoleEntry, NetworkEntry, Window


async def capture_evidence(
    record: RunRecord,
    session: BrowserSession,
    seq: int,
    window: Window | None,
    snapshot: Redacted | None,
) -> None:
    """Save step `seq`'s evidence in `record`, whose scan must be `session`'s
    (`ValueError` before anything is written). `window` is the step's
    settle window, None when it has none (a drifted step, or a failed one
    whose result keeps none); `snapshot` is the caller's first snapshot
    after the step settled."""
    if record.redactor is not session.redactor:
        raise ValueError(
            "evidence goes only to a record that scans with its session's redactor"
        )
    folder = f"evidence/{seq}"
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
