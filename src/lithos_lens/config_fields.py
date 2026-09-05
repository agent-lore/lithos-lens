"""Primitive TOML field validators shared by the ``config.py`` section parsers.

Each ``optional_*`` reader takes one ``[section]`` table, returns the default
when the key is absent, and raises :class:`~lithos_lens.errors.ConfigError`
naming the file, section, and key when the value has the wrong shape — so a
misconfigured field fails at load with a message an operator can act on.

They live beside ``config.py`` rather than inside it to keep that module under
the guardrail's god-module ceiling (docs/architecture.toml ``[budgets]``); the
section parsers and the env overrides stay there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from lithos_lens.errors import ConfigError
from lithos_lens.tasks import TASK_STATUSES, TaskStatusName

__all__ = [
    "optional_bool",
    "optional_int",
    "optional_path",
    "optional_status_groups",
    "optional_str",
    "optional_str_list",
]


def optional_str(
    data: dict[str, Any],
    key: str,
    default: str,
    config_path: Path,
    section: str,
) -> str:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, str):
        raise ConfigError(f"{config_path}: [{section}].{key} must be a string")
    return value


def optional_path(
    data: dict[str, Any],
    key: str,
    default: Path,
    config_path: Path,
    section: str,
) -> Path:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, str):
        raise ConfigError(f"{config_path}: [{section}].{key} must be a string path")
    return Path(value).expanduser()


def optional_int(
    data: dict[str, Any],
    key: str,
    default: int,
    config_path: Path,
    section: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, int):
        raise ConfigError(f"{config_path}: [{section}].{key} must be an integer")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{config_path}: [{section}].{key} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{config_path}: [{section}].{key} must be <= {maximum}")
    return value


def optional_bool(
    data: dict[str, Any],
    key: str,
    default: bool,
    config_path: Path,
    section: str,
) -> bool:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, bool):
        raise ConfigError(f"{config_path}: [{section}].{key} must be a boolean")
    return value


def optional_status_groups(
    data: dict[str, Any],
    key: str,
    default: tuple[TaskStatusName, ...],
    config_path: Path,
    section: str,
) -> tuple[TaskStatusName, ...]:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{config_path}: [{section}].{key} must be a list of strings")
    groups: list[TaskStatusName] = []
    for item in value:
        if item not in TASK_STATUSES:
            raise ConfigError(
                f"{config_path}: [{section}].{key} contains invalid status {item!r}"
            )
        if item not in groups:
            groups.append(cast(TaskStatusName, item))
    if not groups:
        raise ConfigError(f"{config_path}: [{section}].{key} must not be empty")
    return tuple(groups)


def optional_str_list(
    data: dict[str, Any],
    key: str,
    default: tuple[str, ...],
    config_path: Path,
    section: str,
) -> tuple[str, ...]:
    """A list-of-strings knob, EMPTY-LIST-permitting but not blank-permitting.

    An empty list is a meaningful value here (it is how an operator opts out of
    a list-scoped rule), so unlike ``optional_status_groups`` it is accepted;
    a blank entry never is — a knob matched by prefix would match everything.
    """
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{config_path}: [{section}].{key} must be a list of strings")
    items = [item.strip() for item in value]
    if any(not item for item in items):
        raise ConfigError(
            f"{config_path}: [{section}].{key} must not contain an empty string"
        )
    return tuple(items)
