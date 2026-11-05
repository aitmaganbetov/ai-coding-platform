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


def find_config(start: Path) -> Path | None:
    """Return the configuration file in ``start`` (a directory or a file path)."""
    if start.is_file():
        return start
    for name in (CONFIG_FILENAME, *ALT_CONFIG_FILENAMES):
        candidate = start / name
        if candidate.is_file():
            return candidate
    return None


def parse_yaml(text: str) -> Any:
    """Parse YAML text safely (no arbitrary object construction, no duplicate keys)."""
    try:
        return yaml.load(text, Loader=_UniqueKeySafeLoader)  # noqa: S506 - SafeLoader subclass
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
