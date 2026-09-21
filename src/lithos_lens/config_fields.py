"""Primitive TOML field validators shared by the ``config.py`` section parsers.

Each ``optional_*`` reader takes one ``[section]`` table, returns the default
when the key is absent, and raises :class:`~lithos_lens.errors.ConfigError`
naming the file, section, and key when the value has the wrong shape — so a
misconfigured field fails at load with a message an operator can act on.

They live beside ``config.py`` rather than inside it to keep that module under
the guardrail's god-module ceiling (docs/architecture.toml ``[budgets]``); the
section parsers and the env overrides stay there. The deprecation notices
(§4.4) are here for the same reason — and because the one-time latch they need
is state, which is better held in one place than scattered per knob.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

from lithos_lens.errors import ConfigError
from lithos_lens.tasks import (
    PROJECT_CONVENTIONS,
    TASK_STATUSES,
    ProjectConvention,
    TaskStatusName,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEPRECATED_KNOBS",
    "env_project_convention",
    "optional_bool",
    "optional_int",
    "optional_path",
    "optional_project_convention",
    "optional_status_groups",
    "optional_str",
    "optional_str_list",
    "warn_deprecated_env",
    "warn_deprecated_knobs",
]

#: Knobs that are parsed and IGNORED (REQUIREMENTS §4.4), keyed
#: ``<section>.<key>``, each mapped to the sentence its startup notice puts
#: after the knob's name. Being unread does not make a knob unvalidated: a
#: value naming no convention is still a config its author got wrong, so the
#: section parsers keep checking these and the notice says "ignored".
DEPRECATED_KNOBS: dict[str, str] = {
    "lithos-lens.tasks.visible_cap": (
        "is deprecated and unused since the graph-native dashboard (T1) — the "
        "live scale dial is frontier_limit (LITHOS_LENS_TASKS_FRONTIER_LIMIT)."
    ),
    "lithos-lens.tasks.project_convention": (
        "is deprecated and ignored: ?project= matches a row under EITHER §5B.1 "
        "convention, so every control that offers a project offers exactly "
        'what the filter matches. Lens behaves as "both" whatever this says.'
    ),
}

#: Which notices this process has already logged. Module state because the
#: notice is about the deployment's config, not about one ``load_config``
#: call: a reload must not re-announce what the operator was already told.
_WARNED: set[str] = set()


def warn_deprecated_knobs(
    data: dict[str, Any], config_path: Path, section: str
) -> None:
    """Log one notice, once, for every deprecated knob ``section`` writes."""
    for key in data:
        name = f"{section}.{key}"
        if name not in DEPRECATED_KNOBS or name in _WARNED:
            continue
        _WARNED.add(name)
        logger.warning(
            "[%s].%s %s Remove %s from %s.",
            section,
            key,
            DEPRECATED_KNOBS[name],
            key,
            config_path,
        )


def warn_deprecated_env(name: str, knob: str) -> None:
    """The same notice for the ENV spelling of a deprecated knob (§4.4).

    Latched separately from the TOML spelling: a deployment that writes both
    has two places to clean up, and a notice naming only one of them sends the
    operator to a file that is not the whole story.
    """
    if name in _WARNED:
        return
    _WARNED.add(name)
    logger.warning("%s %s Unset %s.", name, DEPRECATED_KNOBS[knob], name)


def optional_project_convention(
    data: dict[str, Any],
    key: str,
    default: ProjectConvention,
    config_path: Path,
    section: str,
) -> ProjectConvention:
    """The §5B.1 posture knob — still VALIDATED though nothing reads it (§4.4).

    Deprecating a knob retires what it selects, not the operator's right to be
    told they mistyped it: a config naming a convention that never existed is
    a config its author got wrong, and swallowing it would turn a typo into
    silence.
    """
    value = optional_str(data, key, default, config_path, section)
    if value not in PROJECT_CONVENTIONS:
        raise ConfigError(
            f"{config_path}: [{section}].{key} must be one of "
            f"{sorted(PROJECT_CONVENTIONS)}"
        )
    return cast(ProjectConvention, value)


def env_project_convention(name: str, raw: str) -> ProjectConvention:
    """The env spelling of the same knob, validated against the same set.

    Parsed rather than dropped so the value an operator set is the value the
    effective config reports — "ignored" is a statement about what READS it,
    not licence to forget it was written.
    """
    if raw not in PROJECT_CONVENTIONS:
        raise ConfigError(f"{name} must be one of {sorted(PROJECT_CONVENTIONS)}")
    return cast(ProjectConvention, raw)


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
