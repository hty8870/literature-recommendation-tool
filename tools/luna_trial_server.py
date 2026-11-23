"""Local-only trial server for the product's three real sources and 6luna models.

This factory is for browser acceptance on public literature metadata.  It binds only to
``127.0.0.1`` when started through ``server_main``, stores its database, auth database,
and Kimi model-call audit under ``CITATION_LUNA_TRIAL_ROOT``, and never loads the existing
DeepSeek credential file.  It does not modify the global Kimi model, the existing
production server, or any proxy.

The trial uses Europe PMC online, the 11,002-record multi-domain snapshot, and the 50-record
aBiotech sample.  The snapshot must have its prebuilt E5 cache; a missing or mismatched
cache aborts startup instead of embedding the whole corpus.  The same ``gpt-6-luna`` Kimi
gateway supplies the agent, source/rerank, reason, design-review, and citation models.

This transport is the local CLI JSON-envelope seam, not the normal Kimi agent and not a
native provider function-calling endpoint. The health endpoint reports the injected
``gpt-6-luna`` reason model; it does not imply the production default changed.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
from tools.benchmark.core import ROOT
from tools.kimi_chat_model import (
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    TRANSPORT,
    build_discovery_components,
    build_kimi_gateway,
)

from citation_tool.config import LibraryConfig, Settings
from citation_tool.publication import canonical_work_id
from citation_tool.runtime import build_metadata_adapters
from citation_tool.semantic import EMBEDDING_CACHE_VERSION, EmbeddingIndex

TRIAL_ROOT_ENV = "CITATION_LUNA_TRIAL_ROOT"
TRIAL_PORT_ENV = "CITATION_LUNA_TRIAL_PORT"
DEFAULT_TRIAL_ROOT = ROOT / "data" / "runtime" / "luna-trial"
DEFAULT_PORT = 8876
TRIAL_HOST = "127.0.0.1"
LARGE_CORPUS = ROOT / "data" / "runtime" / "quality-corpus" / "large-corpus.json"
SAMPLE_CORPUS = ROOT / "data" / "runtime" / "quality-corpus" / "real-corpus.json"
EMBEDDING_CACHE_DIR = ROOT / "data" / "runtime" / "embedding-models"
LIBRARY_IDS = ("europepmc-online", "europepmc-snapshot", "abiotech-sample")
TRIAL_BOUNDARY = (
    "Public literature metadata only; local browser trial on 127.0.0.1. "
    "No private manuscripts, no production deployment, no DeepSeek fallback. "
    "Transport is the Kimi CLI JSON tool envelope, not native function calling."
)


def trial_root() -> Path:
    raw = os.environ.get(TRIAL_ROOT_ENV, "").strip()
    root = Path(raw) if raw else DEFAULT_TRIAL_ROOT
    if not root.is_absolute():
        root = (ROOT / root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def trial_settings(root: Path) -> Settings:
    libraries = (
        LibraryConfig(
            library_id="europepmc-online",
            display_name="Europe PMC 在线",
            fixture_set="",
            delay_ms=0,
            adapter_id="europepmc-online-api-v1",
            simulation=False,
            source_type="remote_api",
        ),
        LibraryConfig(
            library_id="europepmc-snapshot",
            display_name="多学科文献快照",
            fixture_set="",
            delay_ms=0,
            adapter_id="metadata-hybrid-e5-rrf-v1",
            simulation=False,
            source_type="local_snapshot",
            corpus_path=LARGE_CORPUS,
        ),
        LibraryConfig(
            library_id="abiotech-sample",
            display_name="aBiotech 老师样例库",
            fixture_set="",
            delay_ms=0,
            adapter_id="metadata-snapshot-bm25-v1",
            simulation=False,
            source_type="local_snapshot",
            corpus_path=SAMPLE_CORPUS,
        ),
    )
    settings = replace(
        Settings(),
        db_path=root / "trial.db",
        auth_db_path=root / "auth.db",
        libraries=libraries,
        library_mode="metadata",
        corpus_path=LARGE_CORPUS,
        sample_corpus_path=SAMPLE_CORPUS,
        embedding_enabled=True,
        embedding_cache_dir=EMBEDDING_CACHE_DIR,
        model_config_path=None,
        allow_user_models=False,
        model_max_inflight=2,
        model_timeout_ms=300_000,
        agent_timeout_ms=900_000,
        agent_max_tool_calls=32,
        agent_recursion_limit=96,
        agent_max_concurrent_runs=2,
        default_soft_deadline_ms=180_000,
        online_enabled=True,
        auth_required=False,
        citation_text_max_concurrent=1,
        manuscript_upload_enabled=True,
        trusted_hosts=("127.0.0.1", "localhost"),
        base_path="",
    )
    settings.validate()
    return settings


def validate_trial_adapters(adapters: dict[str, Any]) -> None:
    if set(adapters) != set(LIBRARY_IDS):
        raise ValueError(
            "trial server requires exactly the three real sources: " + ", ".join(LIBRARY_IDS)
        )


def _verify_prebuilt_index(index: EmbeddingIndex) -> None:
    path = index.cache_path
    if not path.is_file():
        raise RuntimeError(
            f"prebuilt E5 cache missing for the trial snapshot: {path}; "
            "refusing to start and embed the whole corpus"
        )
    expected_ids = [canonical_work_id(record) for record in index._records]
    try:
        with np.load(path, allow_pickle=False) as cached:
            valid = (
                cached["version"].item() == EMBEDDING_CACHE_VERSION
                and cached["model"].item() == index.model_name
                and cached["model_revision"].item() == index.model_revision
                and cached["ids"].tolist() == expected_ids
                and cached["vectors"].ndim == 2
                and cached["vectors"].shape[0] == len(expected_ids)
                and np.isfinite(cached["vectors"]).all()
            )
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError(f"prebuilt E5 cache cannot be validated: {path}") from exc
    if not valid:
        raise RuntimeError(
            f"prebuilt E5 cache does not match the trial snapshot/model: {path}; "
            "refusing to rebuild it during the trial"
        )


def _verify_snapshot_cache(adapters: dict[str, Any]) -> None:
    snapshot = adapters.get("europepmc-snapshot")
    index = getattr(snapshot, "_index", None)
    if not isinstance(index, EmbeddingIndex):
        raise RuntimeError("trial snapshot did not build a semantic index")
    _verify_prebuilt_index(index)


def create_trial_app(
    *,
    settings: Settings | None = None,
    gateway: Any = None,
    adapters: dict[str, Any] | None = None,
) -> Any:
    root = trial_root()
    settings = settings or trial_settings(root)
    if adapters is None:
        adapters = build_metadata_adapters(settings)
        _verify_snapshot_cache(adapters)
    validate_trial_adapters(adapters)
    if gateway is None:
        gateway = build_kimi_gateway(
            timeout_s=300.0,
            max_concurrency=2,
            artifact_root=root / "model-calls",
        )
    reasons, agent_model, discovery_models = build_discovery_components(
        gateway, max_inflight=2, timeout_ms=300_000
    )
    from citation_tool.api import create_app

    app = create_app(
        settings,
        adapters=adapters,
        reason_generator=reasons,
        agent_model=agent_model,
        discovery_models=discovery_models,
    )
    reasons._cache = app.state.store
    app.state.agent_runner.provider = gateway.config.provider
    if app.state.search_service.reason_provider != DEFAULT_PROVIDER:
        raise RuntimeError("trial app is not using the Kimi reason model")
    if app.state.agent_runner.model_name != DEFAULT_MODEL:
        raise RuntimeError("trial agent is not using the expected model")
    app.state.trial_root = str(root)
    app.state.model_audit_dir = str(root / "model-calls")
    app.state.trial_boundary = TRIAL_BOUNDARY
    app.state.transport = TRANSPORT
    return app


def create_native_trial_app():
    from tools.luna_credentials import authorized_config

    from citation_tool.api import create_app
    from citation_tool.reasons import DeepSeekModelGateway, DeepSeekReasonGenerator

    root = trial_root()
    settings = replace(trial_settings(root), model_timeout_ms=180_000)
    adapters = build_metadata_adapters(settings)
    _verify_snapshot_cache(adapters)
    validate_trial_adapters(adapters)
    gateway = DeepSeekModelGateway(authorized_config(), max_inflight=2, timeout_ms=180_000)
    reasons = DeepSeekReasonGenerator(gateway, max_inflight=2, timeout_ms=180_000)
    app = create_app(
        settings,
        adapters=adapters,
        reason_generator=reasons,
        agent_model=gateway.agent_model(),
    )
    reasons._cache = app.state.store
    app.state.trial_boundary = (
        "Public test material only; native OpenAI Responses, store=false; "
        "no CLI model envelope and no DeepSeek fallback."
    )
    if app.state.agent_runner.model_name != DEFAULT_MODEL:
        raise ValueError("native trial model identity differs")
    return app


def server_main() -> int:
    import uvicorn

    port = int(os.environ.get(TRIAL_PORT_ENV, str(DEFAULT_PORT)))
    if not 1 <= port <= 65535:
        raise ValueError("CITATION_LUNA_TRIAL_PORT is outside 1-65535")
    uvicorn.run(
        "tools.luna_trial_server:create_native_trial_app"
        if os.environ.get("CITATION_LUNA_NATIVE_API") == "1"
        else "tools.luna_trial_server:create_trial_app",
        factory=True,
        host=TRIAL_HOST,
        port=port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(server_main())
