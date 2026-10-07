"""The environment-override pass over a parsed config: env beats file.

Extracted from :mod:`lithos_lens.config` (task "T3-W1 posture and operator
identity", 2026-10-03), discharging the god-module exception that file's
``[budgets]`` note had already chartered: config.py sat at 813 lines on the
stop-loss, the ``[lithos-lens.writes]`` knobs needed ~70 more, and the note
pre-committed the next change that grew it to this extraction rather than to a
fourth budget raise.

It is a real seam, not a line-count device. ``config.py`` answers "what does
this FILE say", one function per ``[lithos-lens.*]`` table over a parsed TOML
dict; this module answers "what does the ENVIRONMENT say over it", one pass
over an already-valid :class:`~lithos_lens.config_schema.LithosLensConfig`,
and it is the half the docs<->code env guardrail
(``tests/test_config_env_prefix.py``) scans by AST for every
``os.environ.get`` literal. The two halves share only the schema and the
per-value validators.

Every ``os.environ.get("LITHOS_LENS_…")`` read below is deliberately a
literal: that guardrail matches on them, and it reads the SOURCE rather than
the runtime, so a computed name would be invisible to it and to the README
table it compares against.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from lithos_lens.config_fields import (
    env_project_convention,
    warn_deprecated_env,
)
from lithos_lens.config_schema import (
    MAX_GRAPH_INT_KNOBS,
    MAX_KNOWLEDGE_GRAPH_EDGE_TABLE_MAX_EDGES,
    MAX_KNOWLEDGE_GRAPH_EDGE_TABLE_TTL_S,
    MAX_KNOWLEDGE_RELATED_TITLE_FANOUT_CAP,
    MAX_TASKS_INT_KNOBS,
    MIN_TASKS_INT_KNOBS,
    LithosLensConfig,
    parse_log_level,
)
from lithos_lens.errors import ConfigError
from lithos_lens.operator import OPERATOR_ID_RULE, valid_operator_id

__all__ = ["apply_env_overrides"]


def apply_env_overrides(cfg: LithosLensConfig) -> LithosLensConfig:
    env_override = os.environ.get("LITHOS_LENS_ENVIRONMENT", "")
    data_dir_override = os.environ.get("LITHOS_LENS_DATA_DIR", "")
    log_level_override = os.environ.get("LITHOS_LENS_LOG_LEVEL", "")
    lithos_url_override = os.environ.get("LITHOS_LENS_LITHOS_URL", "")
    lithos_mcp_sse_path_override = os.environ.get("LITHOS_LENS_MCP_SSE_PATH", "")
    lithos_events_path_override = os.environ.get("LITHOS_LENS_SSE_EVENTS_PATH", "")
    agent_id_override = os.environ.get("LITHOS_LENS_AGENT_ID", "")
    tasks_visible_cap_override = os.environ.get("LITHOS_LENS_TASKS_VISIBLE_CAP", "")
    tasks_frontier_limit_override = os.environ.get(
        "LITHOS_LENS_TASKS_FRONTIER_LIMIT", ""
    )
    gate_wait_env = os.environ.get("LITHOS_LENS_TASKS_GATE_WAITING_ATTENTION_HOURS", "")
    claim_expiry_env = os.environ.get(
        "LITHOS_LENS_TASKS_CLAIM_EXPIRING_SOON_MINUTES", ""
    )
    stale_open_env = os.environ.get("LITHOS_LENS_TASKS_STALE_OPEN_AGE_DAYS", "")
    description_preview_env = os.environ.get(
        "LITHOS_LENS_TASKS_DESCRIPTION_PREVIEW_CHARS", ""
    )
    # Deprecated (§4.4) and read anyway: "ignored" says what CONSULTS the
    # value, not that a value an operator set may be dropped. No "" default,
    # like ``trigger_prefixes_env`` below: WRITING the knob is what the notice
    # and the validation are about, and ``FOO=`` is writing it.
    project_convention_env = os.environ.get("LITHOS_LENS_TASKS_PROJECT_CONVENTION")
    agent_inactive_env = os.environ.get("LITHOS_LENS_TASKS_AGENT_INACTIVE_DAYS", "")
    unclaimed_env = os.environ.get("LITHOS_LENS_TASKS_UNCLAIMED_READY_AGE_MINUTES", "")
    # No "" default, unlike every other read in this pass: an EMPTY value of
    # this knob is meaningful (it is the documented opt-out), so absent and
    # blank have to stay distinguishable — hence ``None`` for absent.
    trigger_prefixes_env = os.environ.get(
        "LITHOS_LENS_TASKS_DISPATCH_TRIGGER_TAG_PREFIXES"
    )
    knowledge_fanout_cap_override = os.environ.get(
        "LITHOS_LENS_KNOWLEDGE_RELATED_TITLE_FANOUT_CAP", ""
    )
    # No "" default, for ``trigger_prefixes_env``'s reason: blank is the
    # documented empty list (only the ``ingested-by:*`` tag marks intake).
    intake_prefixes_env = os.environ.get("LITHOS_LENS_KNOWLEDGE_INTAKE_PATH_PREFIXES")
    edge_table_ttl_env = os.environ.get(
        "LITHOS_LENS_KNOWLEDGE_GRAPH_EDGE_TABLE_TTL_S", ""
    )
    edge_table_max_env = os.environ.get(
        "LITHOS_LENS_KNOWLEDGE_GRAPH_EDGE_TABLE_MAX_EDGES", ""
    )
    graph_cache_ttl_env = os.environ.get("LITHOS_LENS_GRAPH_CACHE_TTL_S", "")
    graph_max_tasks_env = os.environ.get("LITHOS_LENS_GRAPH_MAX_TASKS", "")
    graph_concurrency_env = os.environ.get("LITHOS_LENS_GRAPH_FETCH_CONCURRENCY", "")
    graph_mini_nodes_env = os.environ.get("LITHOS_LENS_GRAPH_MINI_GRAPH_MAX_NODES", "")
    writes_default_operator_env = os.environ.get(
        "LITHOS_LENS_WRITES_DEFAULT_OPERATOR", ""
    )
    writes_confirm_cancel_env = os.environ.get("LITHOS_LENS_WRITES_CONFIRM_CANCEL", "")
    llm_enabled_override = os.environ.get("LITHOS_LENS_LLM_ENABLED", "")
    llm_model_override = os.environ.get("LITHOS_LENS_LLM_MODEL", "")
    llm_provider_override = os.environ.get("LITHOS_LENS_LLM_PROVIDER", "")
    llm_api_key_override = os.environ.get("LITHOS_LENS_LLM_API_KEY", "")
    llm_base_url_override = os.environ.get("LITHOS_LENS_LLM_BASE_URL", "")
    llm_extra_headers_override = os.environ.get(
        "LITHOS_LENS_LLM_EXTRA_HEADERS_JSON", ""
    )
    llm_max_tokens_override = os.environ.get("LITHOS_LENS_LLM_MAX_TOKENS", "")
    telemetry_enabled_override = os.environ.get("LITHOS_LENS_OTEL_ENABLED", "")
    telemetry_endpoint_override = os.environ.get("LITHOS_LENS_OTEL_ENDPOINT", "")

    new_cfg = cfg
    if env_override:
        new_cfg = replace(new_cfg, environment=env_override)
    if data_dir_override:
        new_storage = replace(
            new_cfg.storage, data_dir=Path(data_dir_override).expanduser()
        )
        new_cfg = replace(new_cfg, storage=new_storage)
    if log_level_override:
        new_logging = replace(
            new_cfg.logging, level=parse_log_level(log_level_override)
        )
        new_cfg = replace(new_cfg, logging=new_logging)
    if (
        lithos_url_override
        or lithos_mcp_sse_path_override
        or lithos_events_path_override
        or agent_id_override
    ):
        new_lithos = replace(
            new_cfg.lithos,
            url=lithos_url_override or new_cfg.lithos.url,
            mcp_sse_path=lithos_mcp_sse_path_override or new_cfg.lithos.mcp_sse_path,
            sse_events_path=lithos_events_path_override
            or new_cfg.lithos.sse_events_path,
            agent_id=agent_id_override or new_cfg.lithos.agent_id,
        )
        new_cfg = replace(new_cfg, lithos=new_lithos)
    # Every [tasks] env override is an independent positive integer, collected
    # in one pass and applied in a single replace(). The names follow the
    # shipped convention (LITHOS_LENS_TASKS_<FIELD>), and the literal
    # os.environ.get reads above are what the docs<->code env guardrail matches
    # on — it reads them by AST, so each one has to appear verbatim.
    tasks_env_overrides = {
        field: _parse_env_int(
            f"LITHOS_LENS_TASKS_{field.upper()}",
            raw,
            minimum=MIN_TASKS_INT_KNOBS.get(field, 1),
            maximum=MAX_TASKS_INT_KNOBS.get(field),
        )
        for field, raw in (
            ("visible_cap", tasks_visible_cap_override),
            ("frontier_limit", tasks_frontier_limit_override),
            ("gate_waiting_attention_hours", gate_wait_env),
            ("claim_expiring_soon_minutes", claim_expiry_env),
            ("stale_open_age_days", stale_open_env),
            ("description_preview_chars", description_preview_env),
            ("agent_inactive_days", agent_inactive_env),
            ("unclaimed_ready_age_minutes", unclaimed_env),
        )
        if raw
    }
    if tasks_env_overrides:
        new_cfg = replace(new_cfg, tasks=replace(new_cfg.tasks, **tasks_env_overrides))
    if project_convention_env is not None:
        # Notice first, then validation: it reports the knob being WRITTEN,
        # true whatever the value says, and an operator correcting a typo
        # should not boot twice to learn the knob is dead anyway.
        warn_deprecated_env(
            "LITHOS_LENS_TASKS_PROJECT_CONVENTION",
            "lithos-lens.tasks.project_convention",
        )
        new_cfg = replace(
            new_cfg,
            tasks=replace(
                new_cfg.tasks,
                project_convention=env_project_convention(
                    "LITHOS_LENS_TASKS_PROJECT_CONVENTION", project_convention_env
                ),
            ),
        )
    if trigger_prefixes_env is not None:
        # Comma-separated, unlike its integer neighbours, and gated on PRESENCE
        # rather than truthiness: setting it to the empty string is how an
        # operator writes the empty list (rule 6 back to every ready task), the
        # same opt-out the TOML key spells ``[]``.
        new_cfg = replace(
            new_cfg,
            tasks=replace(
                new_cfg.tasks,
                dispatch_trigger_tag_prefixes=_parse_env_str_list(
                    "LITHOS_LENS_TASKS_DISPATCH_TRIGGER_TAG_PREFIXES",
                    trigger_prefixes_env,
                ),
            ),
        )
    # The [graph] overrides follow the same shipped convention as [tasks]
    # (LITHOS_LENS_GRAPH_<FIELD>), collected in one pass and applied in a
    # single replace(). The literal os.environ.get reads above are what the
    # docs<->code env guardrail matches on by AST, so each appears verbatim.
    graph_env_overrides = {
        field: _parse_env_int(
            f"LITHOS_LENS_GRAPH_{field.upper()}",
            raw,
            maximum=MAX_GRAPH_INT_KNOBS.get(field),
        )
        for field, raw in (
            ("cache_ttl_s", graph_cache_ttl_env),
            ("max_tasks", graph_max_tasks_env),
            ("fetch_concurrency", graph_concurrency_env),
            ("mini_graph_max_nodes", graph_mini_nodes_env),
        )
        if raw
    }
    if graph_env_overrides:
        new_cfg = replace(new_cfg, graph=replace(new_cfg.graph, **graph_env_overrides))
    if knowledge_fanout_cap_override:
        new_knowledge = replace(
            new_cfg.knowledge,
            related_title_fanout_cap=_parse_env_int(
                "LITHOS_LENS_KNOWLEDGE_RELATED_TITLE_FANOUT_CAP",
                knowledge_fanout_cap_override,
                # Same bounds as the [lithos-lens.knowledge] TOML key: a
                # misconfigured env can't amplify the per-request read fan-out.
                maximum=MAX_KNOWLEDGE_RELATED_TITLE_FANOUT_CAP,
            ),
        )
        new_cfg = replace(new_cfg, knowledge=new_knowledge)
    if intake_prefixes_env is not None:
        new_cfg = replace(
            new_cfg,
            knowledge=replace(
                new_cfg.knowledge,
                intake_path_prefixes=_parse_env_str_list(
                    "LITHOS_LENS_KNOWLEDGE_INTAKE_PATH_PREFIXES", intake_prefixes_env
                ),
            ),
        )
    # The edge-table snapshot's two knobs (K2 D2), same bounds as their TOML
    # keys, collected and applied in one replace() like the [graph] ones.
    edge_table_env_overrides = {
        field: _parse_env_int(name, raw, maximum=maximum)
        for field, name, raw, maximum in (
            (
                "graph_edge_table_ttl_s",
                "LITHOS_LENS_KNOWLEDGE_GRAPH_EDGE_TABLE_TTL_S",
                edge_table_ttl_env,
                MAX_KNOWLEDGE_GRAPH_EDGE_TABLE_TTL_S,
            ),
            (
                "graph_edge_table_max_edges",
                "LITHOS_LENS_KNOWLEDGE_GRAPH_EDGE_TABLE_MAX_EDGES",
                edge_table_max_env,
                MAX_KNOWLEDGE_GRAPH_EDGE_TABLE_MAX_EDGES,
            ),
        )
        if raw
    }
    if edge_table_env_overrides:
        new_cfg = replace(
            new_cfg, knowledge=replace(new_cfg.knowledge, **edge_table_env_overrides)
        )
    if writes_default_operator_env or writes_confirm_cancel_env:
        new_writes = new_cfg.writes
        if writes_default_operator_env:
            # The env spelling is validated exactly like the TOML key: this is
            # the value production sets so its single operator is not prompted
            # (REQUIREMENTS §4), and a typo there must fail the boot it is set
            # in rather than silently leave the deployment with no identity.
            if not valid_operator_id(writes_default_operator_env):
                raise ConfigError(
                    "LITHOS_LENS_WRITES_DEFAULT_OPERATOR must be "
                    f"{OPERATOR_ID_RULE} (got "
                    f"{writes_default_operator_env!r}); unset it for no default"
                )
            new_writes = replace(
                new_writes, default_operator=writes_default_operator_env
            )
        if writes_confirm_cancel_env:
            new_writes = replace(
                new_writes,
                confirm_cancel=_parse_env_bool(
                    "LITHOS_LENS_WRITES_CONFIRM_CANCEL", writes_confirm_cancel_env
                ),
            )
        new_cfg = replace(new_cfg, writes=new_writes)
    if any(
        [
            llm_enabled_override,
            llm_model_override,
            llm_provider_override,
            llm_api_key_override,
            llm_base_url_override,
            llm_extra_headers_override,
            llm_max_tokens_override,
        ]
    ):
        new_llm = replace(
            new_cfg.llm,
            enabled=_parse_env_bool("LITHOS_LENS_LLM_ENABLED", llm_enabled_override)
            if llm_enabled_override
            else new_cfg.llm.enabled,
            provider=llm_provider_override or new_cfg.llm.provider,
            model=llm_model_override or new_cfg.llm.model,
            api_key=llm_api_key_override or new_cfg.llm.api_key,
            base_url=llm_base_url_override or new_cfg.llm.base_url,
            extra_headers_json=llm_extra_headers_override
            or new_cfg.llm.extra_headers_json,
            max_tokens=_parse_env_int(
                "LITHOS_LENS_LLM_MAX_TOKENS", llm_max_tokens_override
            )
            if llm_max_tokens_override
            else new_cfg.llm.max_tokens,
        )
        new_cfg = replace(new_cfg, llm=new_llm)
    if telemetry_enabled_override or telemetry_endpoint_override:
        new_telemetry = new_cfg.telemetry
        if telemetry_enabled_override:
            new_telemetry = replace(
                new_telemetry,
                enabled=_parse_env_bool(
                    "LITHOS_LENS_OTEL_ENABLED", telemetry_enabled_override
                ),
            )
        if telemetry_endpoint_override:
            new_telemetry = replace(new_telemetry, endpoint=telemetry_endpoint_override)
        new_cfg = replace(new_cfg, telemetry=new_telemetry)
    return new_cfg


def _parse_env_int(
    name: str, value: str, *, minimum: int = 1, maximum: int | None = None
) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    if maximum is not None and parsed > maximum:
        raise ConfigError(f"{name} must be <= {maximum}")
    return parsed


def _parse_env_str_list(name: str, value: str) -> tuple[str, ...]:
    """A comma-separated env list; blank VALUE is the empty list, blank ENTRY is
    an error (the TOML twin draws the same line: ``[]`` yes, ``[""]`` no)."""
    if not value.strip():
        return ()
    items = [item.strip() for item in value.split(",")]
    if any(not item for item in items):
        raise ConfigError(f"{name} must not contain an empty comma-separated entry")
    return tuple(items)


def _parse_env_bool(name: str, value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean")
