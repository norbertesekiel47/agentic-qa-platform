"""Reading a project's config and specs strictly, before a browser or a model
is involved (DATA_MODEL §6, §9; ADR-0030). Every problem is reported at once,
each naming the file, the key and what is wrong."""

import re
from collections.abc import Callable, Hashable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ValidationError
from yaml.constructor import ConstructorError

from aqa_core.browser import BrowserSettings
from aqa_core.config import ProjectConfig, RoleField
from aqa_core.schema import parse_origin
from aqa_core.spec import Spec, SpecFrontmatter, secret_references, spec_hash


class SpecError(Exception):
    """A spec or the project config is invalid: ADR-0024's `spec_error`. Each
    problem reads `<file>: <key>: <problem>`, or `<file>: line N: <problem>`
    for YAML that doesn't parse."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = tuple(problems)


_CORE = "tag:yaml.org,2002:"
# YAML 1.2's core schema (ADR-0030): each implicit type, its pattern, the
# characters a match can start with, and its value. Integers come before
# floats: the float pattern matches them too, and the first match wins.
_SCALARS: list[tuple[str, re.Pattern[str], list[str], Callable[[str], object]]] = [
    (
        "null",
        re.compile(r"(?:~|null|Null|NULL|)\Z"),
        ["~", "n", "N", ""],
        lambda _: None,
    ),
    (
        "bool",
        re.compile(r"(?:true|True|TRUE|false|False|FALSE)\Z"),
        list("tTfF"),
        lambda value: value.lower() == "true",
    ),
    (
        "int",
        re.compile(r"[-+]?[0-9]+\Z"),
        list("-+0123456789"),
        lambda value: int(value, 10),
    ),
    (
        "float",
        re.compile(r"[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?\Z"),
        list("-+.0123456789"),
        float,
    ),
]


def _implicit_tag(node: yaml.ScalarNode) -> str:
    """The tag a scalar gets with no tag written: a quoted scalar is a string."""
    if node.style is None:
        for name, pattern, _, _ in _SCALARS:
            if pattern.match(node.value):
                return _CORE + name
    return _CORE + "str"


class _Loader(yaml.SafeLoader):
    """YAML 1.2's core schema, narrowed (ADR-0030): strings, decimal integers,
    floats, `true`/`false` and null, in mappings with string keys, each key
    once. No aliases and no tags. PyYAML's own resolvers are YAML 1.1's, which
    read `no` as false, `017` as 15 and `2026-01-04` as a date."""

    def construct_object(self, node: yaml.Node, deep: bool = False) -> Any:
        # Every constructor below is deep, so a node met a second time was
        # reached through an alias. Its mark is the anchor's.
        if node in self.constructed_objects:
            raise ConstructorError(
                None, None, "aliases (*name) are not allowed", node.start_mark
            )
        if isinstance(node, yaml.ScalarNode) and node.tag != _implicit_tag(node):
            raise ConstructorError(
                None, None, "tags (!name) are not allowed", node.start_mark
            )
        return super().construct_object(node, deep)

    def construct_mapping(
        self, node: yaml.MappingNode, deep: bool = False
    ) -> dict[Hashable, Any]:
        mapping: dict[Hashable, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ConstructorError(
                    None,
                    None,
                    f"keys must be strings, not {key!r}",
                    key_node.start_mark,
                )
            if key in mapping:
                raise ConstructorError(
                    None, None, f"duplicate key '{key}'", key_node.start_mark
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


# Replaced, not extended: SafeLoader's resolvers and constructors are YAML 1.1's.
# A tag left without a constructor, such as !!set or !!timestamp, is refused.
def _scalar(
    convert: Callable[[str], object],
) -> Callable[[_Loader, yaml.ScalarNode], object]:
    def construct(_loader: _Loader, node: yaml.ScalarNode) -> object:
        return convert(node.value)

    return construct


def _str(loader: _Loader, node: yaml.ScalarNode) -> str:
    return loader.construct_scalar(node)


def _seq(loader: _Loader, node: yaml.SequenceNode) -> list[Any]:
    return loader.construct_sequence(node, deep=True)


def _map(loader: _Loader, node: yaml.MappingNode) -> dict[Hashable, Any]:
    return loader.construct_mapping(node, deep=True)


_Loader.yaml_implicit_resolvers = {}
_Loader.yaml_constructors = {}
for _name, _pattern, _first, _convert in _SCALARS:
    _Loader.add_implicit_resolver(_CORE + _name, _pattern, _first)
    _Loader.add_constructor(_CORE + _name, _scalar(_convert))
_Loader.add_constructor(_CORE + "str", _str)
_Loader.add_constructor(_CORE + "seq", _seq)
_Loader.add_constructor(_CORE + "map", _map)


def _read_yaml(text: str, path: Path, first_line: int) -> object:
    """`text` parsed; `first_line` is its first line's number in `path`."""
    loader = _Loader(text)
    try:
        return loader.get_single_data()
    except yaml.MarkedYAMLError as error:
        mark = error.problem_mark
        line = "" if mark is None else f"line {mark.line + first_line}: "
        raise SpecError([f"{path}: {line}{error.problem}"]) from None
    finally:
        loader.dispose()


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
            case _:
                problem = detail["msg"]
        yield f"{path}: {_key(detail['loc'])}: {problem}"


def _validate[M: BaseModel](model: type[M], data: object, path: Path) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise SpecError(list(_problems(error, path))) from None


def load_config(path: Path) -> ProjectConfig:
    """The project config at `path`. The directory that holds it is the
    project's spec root (DATA_MODEL §9)."""
    try:
        text = path.read_text(encoding="utf-8")
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
    """The spec at `path`. Its secret references must name secrets `config`
    declares, and its id must be its file name without `.spec.md`."""
    match = _FRONTMATTER.match(path.read_text(encoding="utf-8"))
    if match is None:
        raise SpecError(
            [f"{path}: a spec starts with its frontmatter between two --- lines"]
        )
    data = _read_yaml(match[1], path, first_line=2)
    if not isinstance(data, dict):
        raise SpecError([f"{path}: the frontmatter must be a mapping of keys"])
    problems = []
    name = path.name.removesuffix(".spec.md")
    if isinstance(data.get("id"), str) and data["id"] != name:
        problems.append(
            f"{path}: id: '{data['id']}' doesn't match the file name: a spec's id is its "
            f"file name without .spec.md, here '{name}'"
        )
    try:
        frontmatter = _validate(SpecFrontmatter, data, path)
    except SpecError as error:
        raise SpecError([*problems, *error.problems]) from None
    problems.extend(
        f"{path}: {key}: secret {secret} is not declared in the project config's secrets"
        for key, secret in secret_references(frontmatter)
        if secret not in config.secrets
    )
    if problems:
        raise SpecError(problems)
    return Spec(path, frontmatter, spec_hash(data))


@dataclass(frozen=True)
class Project:
    """A spec root: its project config, and every spec below it by id."""

    root: Path
    config: ProjectConfig
    specs: Mapping[str, Spec]


def load_project(spec_root: Path) -> Project:
    """The project whose config is `spec_root/config.yaml`, with every
    `*.spec.md` below it, in subdirectories too. Spec ids are unique in a
    project (DATA_MODEL §6)."""
    config = load_config(spec_root / "config.yaml")
    specs: dict[str, Spec] = {}
    problems: list[str] = []
    for path in sorted(spec_root.rglob("*.spec.md")):
        try:
            spec = load_spec(path, config)
        except SpecError as error:
            problems.extend(error.problems)
            continue
        first = specs.setdefault(spec.frontmatter.id, spec)
        if first is not spec:
            problems.append(
                f"{path}: id: '{spec.frontmatter.id}' is already the id of {first.path}: "
                "spec ids are unique in a project"
            )
    if problems:
        raise SpecError(problems)
    return Project(spec_root, config, specs)


def effective_browser(config: ProjectConfig, spec: Spec) -> BrowserSettings:
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


def allowed_origins(start: str, spec: Spec) -> tuple[str, ...]:
    """The origins the run may navigate to and act on: `start`, the run's
    start origin, then the spec's (ADR-0026)."""
    return tuple(dict.fromkeys((start, *spec.frontmatter.allowed_origins)))


@dataclass(frozen=True)
class SecretDestination:
    """Where the browser may fill one test secret in this run."""

    origins: tuple[str, ...]
    field: Literal["password"] | RoleField


def secret_destinations(
    spec: Spec, config: ProjectConfig, start: str
) -> dict[str, SecretDestination]:
    """For each test secret `spec` references, its binding in `config`
    intersected with the run's allowed origins (ADR-0026), where `start` is
    the run's start origin. A bound origin the run doesn't allow is an error,
    never dropped: the secret could otherwise be left with nowhere to go."""
    allowed = allowed_origins(start, spec)
    destinations: dict[str, SecretDestination] = {}
    problems: list[str] = []
    for key, name in secret_references(spec.frontmatter):
        if name in destinations:
            continue
        binding = config.secrets[name]
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
