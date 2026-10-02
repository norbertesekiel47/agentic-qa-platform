"""Reading a project's config and specs strictly, before a browser or a model
is involved (DATA_MODEL §6, §9; ADR-0030). Every problem is reported at once,
each naming the file, the key and what is wrong."""

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from aqa_core import strict_yaml
from aqa_core.browser import BrowserSettings
from aqa_core.config import ProjectConfig, RoleField
from aqa_core.schema import parse_origin
from aqa_core.spec import (
    Spec,
    SpecContext,
    SpecFrontmatter,
    secret_references,
    spec_hash,
)


class SpecError(Exception):
    """A spec or the project config is invalid: ADR-0024's `spec_error`. Each
    problem reads `<file>: <key>: <problem>`, or `<file>: line N: <problem>`
    for YAML that doesn't parse."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = tuple(problems)


def _read_text(path: Path) -> str:
    try:
        # utf-8-sig: a byte order mark an editor wrote is not part of the text.
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise SpecError([f"{path}: not UTF-8 text"]) from None


def _read_yaml(text: str, path: Path, first_line: int) -> object:
    """`text` parsed; `first_line` is its first line's number in `path`."""
    try:
        return strict_yaml.parse(text)
    except strict_yaml.StrictYAMLError as error:
        line = "" if error.line is None else f"line {error.line + first_line}: "
        raise SpecError([f"{path}: {line}{error.problem}"]) from None


def _key(loc: tuple[int | str, ...]) -> str:
    key = ""
    for part in loc:
        if isinstance(part, int):
            key += f"[{part}]"
        elif part != "[key]":  # Pydantic's marker for an invalid mapping key
            key += f".{part}" if key else part
    return key


def _problems(error: ValidationError, path: Path) -> Iterator[str]:
    for detail in error.errors():
        match detail["type"]:
            case "extra_forbidden":
                problem = "unknown key"
            case "missing":
                problem = "missing key"
            case "value_error":
                problem = str(detail["ctx"]["error"])
            # Pydantic names Python's types; the file is YAML.
            case "model_type" | "dict_type":
                problem = "must be a mapping of keys"
            case "tuple_type":
                problem = "must be a list"
            case _:
                problem = detail["msg"]
        yield f"{path}: {_key(detail['loc'])}: {problem}"


def _validate[M: BaseModel](
    model: type[M], data: object, path: Path, context: SpecContext | None = None
) -> M:
    try:
        return model.model_validate(data, context=context)
    except ValidationError as error:
        raise SpecError(list(_problems(error, path))) from None


def load_config(path: Path) -> ProjectConfig:
    """The project config at `path`. The directory that holds it is the
    project's spec root (DATA_MODEL §9)."""
    try:
        text = _read_text(path)
    except FileNotFoundError:
        raise SpecError(
            [
                (
                    f"{path}: no such file: a project's spec root is the directory "
                    "that holds its config.yaml"
                )
            ]
        ) from None
    data = _read_yaml(text, path, first_line=1)
    if data is None:  # an empty file, or comments only
        data = {}
    if not isinstance(data, dict):
        raise SpecError([f"{path}: the project config must be a mapping of keys"])
    return _validate(ProjectConfig, data, path)


# A spec file opens with its frontmatter, between two lines of three dashes.
_FRONTMATTER = re.compile(r"---\n(.*?)^---[ \t]*$", re.DOTALL | re.MULTILINE)


def load_spec(path: Path, config: ProjectConfig) -> Spec:
    """The spec at `path`, carrying the bindings in `config` of exactly the
    test secrets it references. Its secret references must name secrets
    `config` declares, and its id must be its file name without `.spec.md`."""
    frontmatter, hashed = _read_spec(path, frozenset(config.secrets))
    return _spec(path, frontmatter, hashed, config)


def _read_spec(
    path: Path, declared_secrets: frozenset[str] | None
) -> tuple[SpecFrontmatter, str]:
    """The frontmatter of the spec at `path`, and its spec_hash."""
    match = _FRONTMATTER.match(_read_text(path))
    if match is None:
        raise SpecError(
            [f"{path}: a spec starts with its frontmatter between two --- lines"]
        )
    data = _read_yaml(match[1], path, first_line=2)
    if not isinstance(data, dict):
        raise SpecError([f"{path}: the frontmatter must be a mapping of keys"])
    context = SpecContext(
        file_id=path.name.removesuffix(".spec.md"), declared_secrets=declared_secrets
    )
    return _validate(SpecFrontmatter, data, path, context), spec_hash(data)


def _spec(
    path: Path, frontmatter: SpecFrontmatter, hashed: str, config: ProjectConfig
) -> Spec:
    """The spec, with the bindings in `config` of the secrets `frontmatter`
    references, which _read_spec checked `config` declares."""
    referenced = {name for _, name in secret_references(frontmatter)}
    bindings = {name: b for name, b in config.secrets.items() if name in referenced}
    return Spec(path, frontmatter, hashed, bindings)


@dataclass(frozen=True)
class Project:
    """A spec root: its project config, and every spec below it by id."""

    root: Path
    config: ProjectConfig
    specs: Mapping[str, Spec]


def load_project(spec_root: Path) -> Project:
    """The project whose config is `spec_root/config.yaml`, with every
    `*.spec.md` below it, in subdirectories too, each carrying the config's
    bindings of exactly the test secrets it references. Spec ids are unique in a
    project (DATA_MODEL §6). An invalid config doesn't stop the specs being
    read: every problem in every file is reported together."""
    problems: list[str] = []
    try:
        config: ProjectConfig | None = load_config(spec_root / "config.yaml")
    except SpecError as error:
        config = None
        problems.extend(error.problems)
    # Unknown when the config is invalid, so references aren't checked then.
    declared = None if config is None else frozenset(config.secrets)
    read: dict[str, tuple[Path, SpecFrontmatter, str]] = {}
    for path in sorted(p for p in spec_root.rglob("*.spec.md") if p.is_file()):
        try:
            frontmatter, hashed = _read_spec(path, declared)
        except SpecError as error:
            problems.extend(error.problems)
            continue
        first, _, _ = read.setdefault(frontmatter.id, (path, frontmatter, hashed))
        if first != path:
            problems.append(
                f"{path}: id: '{frontmatter.id}' is already the id of {first}: "
                "spec ids are unique in a project"
            )
    if config is None or problems:
        raise SpecError(problems)
    # Built only now: an invalid config has no bindings to give them.
    specs = {spec_id: _spec(*entry, config) for spec_id, entry in read.items()}
    return Project(spec_root, config, specs)


def effective_browser(spec: Spec, config: ProjectConfig) -> BrowserSettings:
    """The pinned settings, overridden by the project's, then by the spec's
    (ADR-0025)."""
    settings = BrowserSettings().model_dump()
    for overrides in (config.browser, spec.frontmatter.browser):
        settings |= overrides.model_dump(exclude_none=True)
    return BrowserSettings.model_validate(settings)


def start_origin(url: str | None, config: ProjectConfig) -> str:
    """The run's start origin: the invocation's `--url` when it gives one,
    otherwise the project's base_url (DATA_MODEL §9). Never the spec's."""
    if url is None:
        if config.base_url is None:
            raise SpecError(
                ["no start origin: pass --url, or set base_url in the project config"]
            )
        return config.base_url
    try:
        return parse_origin(url)
    except ValueError as error:
        raise SpecError([f"--url: {error}"]) from None


def allowed_origins(spec: Spec, start: str) -> tuple[str, ...]:
    """The origins the run may navigate to and act on: `start`, the run's
    start origin, then the spec's (ADR-0026)."""
    return tuple(dict.fromkeys((start, *spec.frontmatter.allowed_origins)))


def start_url(spec: Spec, start: str) -> str:
    """Where the run's first navigation goes: `start`, the run's start origin
    as start_origin gives it, followed by the spec's start_url as written.
    Joined as text, never percent-decoded or resolved (ADR-0026's start URL
    amendment)."""
    return start + spec.frontmatter.preconditions.start_url


@dataclass(frozen=True)
class SecretDestination:
    """Where the browser may fill one test secret in this run."""

    origins: tuple[str, ...]
    field: Literal["password"] | RoleField


def secret_destinations(spec: Spec, start: str) -> dict[str, SecretDestination]:
    """For each test secret `spec` references, its binding, which `spec`
    carries from the config it was loaded with, intersected with the run's
    allowed origins (ADR-0026), where `start` is the run's start origin. A
    bound origin the run doesn't allow is an error, never dropped: the secret
    could otherwise be left with nowhere to go. fill_secret takes each
    secret's origins and field from here, never from `spec.secret_bindings`."""
    allowed = allowed_origins(spec, start)
    destinations: dict[str, SecretDestination] = {}
    problems: list[str] = []
    for key, name in secret_references(spec.frontmatter):
        if name in destinations:
            continue
        binding = spec.secret_bindings[name]
        origins = tuple(
            dict.fromkeys(start if o == "start" else o for o in binding.origins)
        )
        problems.extend(
            f"{spec.path}: {key}: secret {name} is bound to {origin}, which is not one of "
            f"this run's allowed origins: {', '.join(allowed)}"
            for origin in origins
            if origin not in allowed
        )
        destinations[name] = SecretDestination(origins, binding.field)
    if problems:
        raise SpecError(problems)
    return destinations
