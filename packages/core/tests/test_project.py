"""A project: its config and specs loaded together, and the values a run
derives from them before its browser starts (DATA_MODEL §6, §7, §9;
ADR-0025; ADR-0026; #39)."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from aqa_core.browser import BrowserSettings
from aqa_core.config import ProjectConfig, RoleField
from aqa_core.project import (
    SecretDestination,
    SpecError,
    allowed_origins,
    effective_browser,
    load_config,
    load_project,
    load_spec,
    secret_destinations,
    start_origin,
)
from aqa_core.spec import Spec

PILOT = Path(__file__).resolve().parents[3] / "bench" / "apps" / "conduit" / "qa"
PILOT_SPECS = [
    "favorite-article",
    "login",
    "post-comment",
    "publish-article",
    "read-article",
]

SPEC = """\
---
id: {id}
goal: A reader signs in.
preconditions:
  start_url: /login
  account: {{ email: reader@example.test, password: {{ secret: TEST_PASSWORD }} }}
expect:
  - The home page is shown
{extra}---
"""

BOUND = "secrets: { TEST_PASSWORD: { origins: [start], field: password } }\n"


def write_spec(path: Path, extra: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SPEC.format(id=path.name.removesuffix(".spec.md"), extra=extra))
    return path


def write_project(root: Path, config: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(config)
    return root


def load_one(
    tmp_path: Path, config: str, extra: str = ""
) -> tuple[ProjectConfig, Spec]:
    """A project config and one spec, `login`, loaded with it."""
    root = write_project(tmp_path / "qa", config)
    loaded = load_config(root / "config.yaml")
    return loaded, load_spec(write_spec(root / "login.spec.md", extra), loaded)


def test_the_pilot_specs_and_config_load() -> None:
    project = load_project(PILOT)

    assert sorted(project.specs) == PILOT_SPECS
    assert project.config.base_url == "http://127.0.0.1:4100"
    assert project.config.secrets["TEST_PASSWORD"].field == "password"


def test_the_pilot_spec_hashes_are_stable_across_runs() -> None:
    # Fresh interpreters with different hash seeds, so nothing depends on the
    # order of a set or a dict within one process.
    script = (
        "import json, sys; from pathlib import Path; from aqa_core.project import load_project; "
        "print(json.dumps({i: s.spec_hash for i, s in load_project(Path(sys.argv[1])).specs.items()}))"
    )
    runs = [
        json.loads(
            subprocess.run(
                [sys.executable, "-c", script, str(PILOT)],
                env={**os.environ, "PYTHONHASHSEED": seed},
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
        for seed in ("1", "2")
    ]

    in_process = {i: s.spec_hash for i, s in load_project(PILOT).specs.items()}
    assert runs[0] == runs[1] == in_process
    assert len(set(in_process.values())) == len(PILOT_SPECS)


def test_a_pilot_spec_hash_ignores_edits_to_tags_and_the_body(tmp_path: Path) -> None:
    root = shutil.copytree(PILOT, tmp_path / "qa")
    before = load_project(root).specs["login"].spec_hash
    spec = root / "login.spec.md"
    edited = spec.read_text().replace("tags: [auth, navigation]", "tags: [auth, smoke]")
    assert edited != spec.read_text()
    spec.write_text(edited + "\nMore notes for humans.\n")

    assert load_project(root).specs["login"].spec_hash == before


def test_specs_in_subdirectories_belong_to_the_project(tmp_path: Path) -> None:
    root = write_project(tmp_path / "qa", BOUND)
    write_spec(root / "login.spec.md")
    write_spec(root / "checkout" / "pay.spec.md")

    assert sorted(load_project(root).specs) == ["login", "pay"]


def test_a_spec_id_used_twice_in_one_project_is_an_error(tmp_path: Path) -> None:
    root = write_project(tmp_path / "qa", BOUND)
    first = write_spec(root / "auth" / "login.spec.md")
    second = write_spec(root / "login.spec.md")

    with pytest.raises(SpecError) as raised:
        load_project(root)

    assert raised.value.problems == (
        f"{second}: id: 'login' is already the id of {first}: spec ids are unique in a project",
    )


def test_every_spec_problem_in_the_project_is_reported_together(tmp_path: Path) -> None:
    root = write_project(tmp_path / "qa", BOUND)
    login = write_spec(root / "login.spec.md", extra="owner: qa\n")
    pay = write_spec(root / "pay.spec.md", extra="tags: [1]\n")

    with pytest.raises(SpecError) as raised:
        load_project(root)

    assert [line.split(": ")[:2] for line in raised.value.problems] == [
        [str(login), "owner"],
        [str(pay), "tags[0]"],
    ]


def test_a_project_without_a_config_is_an_error(tmp_path: Path) -> None:
    write_spec(tmp_path / "qa" / "login.spec.md")

    with pytest.raises(SpecError) as raised:
        load_project(tmp_path / "qa")

    assert raised.value.problems[0].startswith(
        f"{tmp_path / 'qa' / 'config.yaml'}: no such file"
    )


# Effective browser settings (ADR-0025).


def test_without_overrides_the_pinned_settings_apply(tmp_path: Path) -> None:
    config, spec = load_one(tmp_path, BOUND)

    assert effective_browser(config, spec) == BrowserSettings()


def test_the_spec_overrides_the_project_which_overrides_the_pins(
    tmp_path: Path,
) -> None:
    config, spec = load_one(
        tmp_path,
        BOUND + "browser: { timezone: Asia/Tokyo, locale: de-DE }\n",
        extra="browser: { timezone: Europe/Paris, viewport: [1440, 900] }\n",
    )

    assert effective_browser(config, spec) == BrowserSettings(
        timezone="Europe/Paris",  # the spec's, over the project's
        locale="de-DE",  # the project's
        viewport=(1440, 900),  # the spec's
        device_scale_factor=1,  # pinned
        color_scheme="light",  # pinned
    )


# Start origin and allowed origins (ADR-0026).


def test_url_wins_over_base_url() -> None:
    config = ProjectConfig.model_validate({"base_url": "http://127.0.0.1:4100"})

    assert (
        start_origin("HTTPS://Preview.Example.test/", config)
        == "https://preview.example.test"
    )


def test_base_url_is_the_start_origin_when_no_url_is_given() -> None:
    config = ProjectConfig.model_validate({"base_url": "http://127.0.0.1:4100"})

    assert start_origin(None, config) == "http://127.0.0.1:4100"


def test_with_neither_url_nor_base_url_there_is_no_start_origin() -> None:
    with pytest.raises(SpecError) as raised:
        start_origin(None, ProjectConfig())

    assert raised.value.problems == (
        "no start origin: pass --url, or set base_url in the project config",
    )


@pytest.mark.parametrize(
    "url", ["https://preview.example.test/app/", "preview.example.test", ""]
)
def test_url_must_be_an_origin(url: str) -> None:
    with pytest.raises(SpecError) as raised:
        start_origin(url, ProjectConfig())

    assert raised.value.problems[0].startswith(f"--url: '{url}' is not an origin")


def test_allowed_origins_are_the_start_origin_then_the_specs(tmp_path: Path) -> None:
    _, spec = load_one(
        tmp_path,
        BOUND,
        extra="allowed_origins: ['https://pay.example.test', 'http://127.0.0.1:4100']\n",
    )

    assert allowed_origins("http://127.0.0.1:4100", spec) == (
        "http://127.0.0.1:4100",
        "https://pay.example.test",
    )


# Secret destinations (ADR-0026).


def test_start_in_a_binding_is_the_runs_start_origin() -> None:
    project = load_project(PILOT)

    destinations = secret_destinations(
        project.specs["login"], project.config, "http://localhost:4100"
    )

    assert destinations == {
        "TEST_PASSWORD": SecretDestination(
            origins=("http://localhost:4100",), field="password"
        )
    }


def test_a_binding_keeps_the_allowed_origins_it_names(tmp_path: Path) -> None:
    config, spec = load_one(
        tmp_path,
        "secrets: { TEST_PASSWORD: { origins: [start, 'https://pay.example.test'], "
        "field: { role: textbox, name: Password } } }\n",
        extra="allowed_origins: ['https://pay.example.test']\n",
    )

    assert secret_destinations(spec, config, "http://127.0.0.1:4100") == {
        "TEST_PASSWORD": SecretDestination(
            origins=("http://127.0.0.1:4100", "https://pay.example.test"),
            field=RoleField(role="textbox", name="Password"),
        )
    }


def test_a_binding_to_an_origin_the_run_does_not_allow_is_rejected(
    tmp_path: Path,
) -> None:
    # Bound to the base URL by name, but this run starts elsewhere (--url).
    config, spec = load_one(
        tmp_path,
        "secrets: { TEST_PASSWORD: { origins: ['http://127.0.0.1:4100'], field: password } }\n",
    )

    with pytest.raises(SpecError) as raised:
        secret_destinations(spec, config, "http://localhost:4100")

    assert raised.value.problems == (
        (
            f"{spec.path}: preconditions.account.password: secret TEST_PASSWORD is bound to "
            "http://127.0.0.1:4100, which is not one of this run's allowed origins: "
            "http://localhost:4100"
        ),
    )


def test_a_binding_to_the_start_origin_by_name_is_allowed(tmp_path: Path) -> None:
    config, spec = load_one(
        tmp_path,
        "secrets: { TEST_PASSWORD: { origins: ['http://127.0.0.1:4100'], field: password } }\n",
    )

    destinations = secret_destinations(spec, config, "http://127.0.0.1:4100")

    assert destinations["TEST_PASSWORD"].origins == ("http://127.0.0.1:4100",)


def test_only_the_secrets_the_spec_references_get_destinations(tmp_path: Path) -> None:
    # API_TOKEN's origin isn't allowed in this run, but the spec never uses it.
    config, spec = load_one(
        tmp_path,
        "secrets:\n"
        "  TEST_PASSWORD: { origins: [start], field: password }\n"
        "  API_TOKEN: { origins: ['https://admin.example.test'], field: password }\n",
    )

    assert list(secret_destinations(spec, config, "http://127.0.0.1:4100")) == [
        "TEST_PASSWORD"
    ]
