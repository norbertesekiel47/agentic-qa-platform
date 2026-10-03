"""The child process `test_executor`'s crash test kills mid-run: it replays a
script the test wrote, recording into a run record the test made. Run as
`python -m packages.runner.tests.executor_crash <arguments file>`, from the
repository's root."""

import asyncio
import json
import sys
from pathlib import Path

from aqa_core.compiled import CompiledScript
from aqa_core.config import ProjectConfig
from aqa_core.project import load_spec
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.executor import RunSetup, replay
from aqa_runner.run_record import RunRecord
from playwright.async_api import async_playwright

from packages.runner.tests.egress_fixtures import gate


def main(arguments: Path) -> None:
    """Replay the run `arguments` describes: its script, spec, config,
    start origin and record."""
    given = json.loads(arguments.read_text())
    config = ProjectConfig.model_validate(given["config"])
    script = CompiledScript.model_validate_json(given["script"])
    setup = RunSetup(
        load_spec(Path(given["spec"]), config),
        config,
        given["origin"],
        RunRecord(given["run_id"], Path(given["record"])),
    )
    run_gate = gate(allowed=(setup.start,))

    async def scenario() -> None:
        async with async_playwright() as playwright, EgressProxy(run_gate) as proxy:
            await replay(
                script, setup, chromium=playwright.chromium, proxy=proxy, gate=run_gate
            )

    asyncio.run(scenario())


if __name__ == "__main__":
    main(Path(sys.argv[1]))
