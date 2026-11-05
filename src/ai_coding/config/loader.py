"""Safe YAML loading for configuration files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CONFIG_FILENAME = ".ai-coding.yml"
ALT_CONFIG_FILENAMES = (".ai-coding.yaml",)
MAX_CONFIG_BYTES = 1_048_576  # 1 MiB: configuration is small; refuse pathological input.


class ConfigLoadError(Exception):
    """The configuration file cannot be read or parsed."""

    def __init__(self, message: str, *, line: int | None = None, column: int | None = None):
        super().__init__(message)
        self.line = line
        self.column = column


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys (a duplicate could silently override a policy)."""


def _construct_mapping(loader: _UniqueKeySafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in mapping:
            mark = key_node.start_mark
            raise ConfigLoadError(
                f"duplicate key '{key}'", line=mark.line + 1, column=mark.column + 1
            )
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


STATE_CONFIG = ".ai-coding/config.yml"


def find_project_config(root: Path) -> Path | None:
    """The repository's configuration, else the platform-managed one in ``.ai-coding/``.

    ``.ai-coding/config.yml`` is written by ``ai-coding create`` and by the server for
    repositories without their own configuration; it is never committed.
    """
    found = find_config(root)
    if found is not None:
        return found
    state = root / STATE_CONFIG
    return state if state.is_file() else None


def find_config(start: Path) -> Path | None:
    """Return the configuration file in ``start`` (a directory or a file path)."""
    if start.is_file():
        return start
    for name in (CONFIG_FILENAME, *ALT_CONFIG_FILENAMES):
        candidate = start / name
        if candidate.is_file():
            return candidate
    return None


MAX_EXPANDED_NODES = 100_000  # a real configuration has a few hundred


def _expanded_size(root: yaml.Node, limit: int) -> int:
    """Number of nodes after alias expansion, computed on the shared graph (memoized).

    Aliases make the composed graph a DAG: "billion laughs" is tiny as a graph but
    expands to 9**9 nodes, which the schema validator would walk (B-801, found by
    fuzzing). Stops counting as soon as ``limit`` is exceeded.
    """
    sizes: dict[int, int] = {}
    stack: list[tuple[yaml.Node, bool]] = [(root, False)]
    while stack:  # iterative post-order: no recursion limit on deep documents
        node, done = stack.pop()
        if id(node) in sizes:
            continue
        children = _children(node)
        if done or not children:
            total = 1 + sum(sizes.get(id(c), 0) for c in children)
            sizes[id(node)] = min(total, limit + 1)
            continue
        stack.append((node, True))
        stack.extend((c, False) for c in children if id(c) not in sizes)
    return sizes[id(root)]


def _children(node: yaml.Node) -> list[yaml.Node]:
    if isinstance(node, yaml.SequenceNode):
        return list(node.value)
    if isinstance(node, yaml.MappingNode):
        return [part for pair in node.value for part in pair]
    return []


def parse_yaml(text: str) -> Any:
    """Parse YAML text safely (no arbitrary object construction, no duplicate keys,
    bounded alias expansion)."""
    try:
        loader = _UniqueKeySafeLoader(text)
        try:
            node = loader.get_single_node()
            if node is None:
                return None
            if _expanded_size(node, MAX_EXPANDED_NODES) > MAX_EXPANDED_NODES:
                raise ConfigLoadError(
                    f"the document expands to more than {MAX_EXPANDED_NODES} nodes "
                    "(YAML aliases); refusing it"
                )
            return loader.construct_document(node)
        finally:
            loader.dispose()
    except ConfigLoadError:
        raise
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark
        raise ConfigLoadError(
            f"invalid YAML: {exc.problem or exc}",
            line=mark.line + 1 if mark else None,
            column=mark.column + 1 if mark else None,
        ) from exc
    except yaml.YAMLError as exc:
        raise ConfigLoadError(f"invalid YAML: {exc}") from exc


def read_config_text(path: Path) -> str:
    """Read a configuration file with size and encoding checks."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ConfigLoadError(f"cannot read {path}: {exc.strerror or exc}") from exc
    if size > MAX_CONFIG_BYTES:
        raise ConfigLoadError(f"{path} is larger than {MAX_CONFIG_BYTES} bytes")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigLoadError(f"{path} is not valid UTF-8") from exc
    except OSError as exc:
        raise ConfigLoadError(f"cannot read {path}: {exc.strerror or exc}") from exc
