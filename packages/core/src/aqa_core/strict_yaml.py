"""The YAML that specs and the project config are written in: YAML 1.2's core
schema, narrowed (ADR-0030). PyYAML's own resolvers are YAML 1.1's, which read
`no` as false, `017` as 15 and `2026-01-04` as a date, and its loaders keep
the last of two equal keys."""

import re
from collections.abc import Callable, Hashable
from typing import Any

import yaml
from yaml.constructor import ConstructorError


class StrictYAMLError(ValueError):
    """YAML that doesn't parse, or uses what the narrowed schema refuses."""

    def __init__(self, problem: str, line: int | None) -> None:
        super().__init__(problem)
        self.problem = problem
        # 0-based, as PyYAML counts; None when PyYAML gives no position.
        self.line = line


_CORE = "tag:yaml.org,2002:"
# Each implicit type, its pattern, the characters a match can start with, and
# its value. Integers come before floats: the float pattern matches them too,
# and the first match wins.
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


def _implicit_tag(node: yaml.Node) -> str:
    """The tag a node gets with no tag written: a sequence's, a mapping's, or
    the scalar type its text reads as. A quoted scalar is a string."""
    if isinstance(node, yaml.SequenceNode):
        return _CORE + "seq"
    if isinstance(node, yaml.MappingNode):
        return _CORE + "map"
    if isinstance(node, yaml.ScalarNode) and node.style is None:
        for name, pattern, _, _ in _SCALARS:
            if pattern.match(node.value):
                return _CORE + name
    return _CORE + "str"


class _Loader(yaml.SafeLoader):
    """Strings, decimal integers, floats, `true`/`false` and null, in mappings
    with string keys, each key once. No aliases, and no tag other than the
    one a node would have with none written."""

    def construct_object(self, node: yaml.Node, deep: bool = False) -> Any:
        # Every constructor below is deep, so a node met a second time was
        # reached through an alias. Its mark is the anchor's.
        if node in self.constructed_objects:
            raise ConstructorError(
                None, None, "aliases (*name) are not allowed", node.start_mark
            )
        if node.tag != _implicit_tag(node):
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


def _scalar(
    name: str, convert: Callable[[str], object]
) -> Callable[[_Loader, yaml.ScalarNode], object]:
    def construct(_loader: _Loader, node: yaml.ScalarNode) -> object:
        try:
            return convert(node.value)
        except ValueError as error:  # int() refuses more than 4,300 digits
            raise ConstructorError(
                None, None, f"can't read this {name}: {error}", node.start_mark
            ) from None

    return construct


def _str(loader: _Loader, node: yaml.ScalarNode) -> str:
    return loader.construct_scalar(node)


def _seq(loader: _Loader, node: yaml.SequenceNode) -> list[Any]:
    return loader.construct_sequence(node, deep=True)


def _map(loader: _Loader, node: yaml.MappingNode) -> dict[Hashable, Any]:
    return loader.construct_mapping(node, deep=True)


# Replaced, not extended: SafeLoader's resolvers and constructors are YAML 1.1's.
_Loader.yaml_implicit_resolvers = {}
_Loader.yaml_constructors = {}
for _name, _pattern, _first, _convert in _SCALARS:
    _Loader.add_implicit_resolver(_CORE + _name, _pattern, _first)
    _Loader.add_constructor(_CORE + _name, _scalar(_name, _convert))
_Loader.add_constructor(_CORE + "str", _str)
_Loader.add_constructor(_CORE + "seq", _seq)
_Loader.add_constructor(_CORE + "map", _map)


def parse(text: str) -> object:
    """`text`'s one YAML document, or StrictYAMLError."""
    try:
        # The reader checks every character as the loader is made.
        loader = _Loader(text)
    except yaml.reader.ReaderError as error:
        problem = f"unacceptable character #x{error.character:04x}: {error.reason}"
        raise StrictYAMLError(problem, text.count("\n", 0, error.position)) from None
    try:
        return loader.get_single_data()
    except yaml.MarkedYAMLError as error:
        mark = error.problem_mark
        line = None if mark is None else mark.line
        raise StrictYAMLError(error.problem or str(error), line) from None
    except RecursionError:  # PyYAML composes nested collections recursively
        raise StrictYAMLError("nested too deeply", None) from None
    finally:
        loader.dispose()
