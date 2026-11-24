"""Environment-only production wiring; no credentials are read from this module."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import (
    BIBLIOGRAPHIC_ONLY,
    BUNDLED_CORPUS_PATH,
    BUNDLED_SAMPLE_PATH,
    LibraryConfig,
    Settings,
)


def _bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized not in {"1", "0", "true", "false", "yes", "no"}:
        raise ValueError(f"{name} must be a boolean")
    return normalized in {"1", "true", "yes"}


def settings_from_env() -> Settings:
    defaults = Settings()
    base_path = os.environ.get("CITATION_BASE_PATH", "").strip()
    if base_path and not base_path.startswith("/"):
        base_path = "/" + base_path
    base_path = base_path.rstrip("/")
    library_mode = os.environ.get("CITATION_LIBRARY_MODE", "metadata")
    account_key_file = os.environ.get("CITATION_ACCOUNT_MODEL_KEY_FILE")
    corpus_path = Path(os.environ.get("CITATION_CORPUS_PATH", BUNDLED_CORPUS_PATH))
    sample_path = Path(os.environ.get("CITATION_SAMPLE_CORPUS_PATH", BUNDLED_SAMPLE_PATH))
    bundled_start = library_mode == "metadata" and (corpus_path, sample_path) == (
        BUNDLED_CORPUS_PATH, BUNDLED_SAMPLE_PATH
    )
    settings = replace(
        defaults,
        db_path=Path(os.environ.get("CITATION_DB_PATH", defaults.db_path)),
        default_tenant=os.environ.get("CITATION_DEFAULT_TENANT", defaults.default_tenant),
        model_config_path=Path(
            os.environ.get("CITATION_UPDATER_MODEL_CONFIG")
            or os.environ.get("CITATION_MODEL_CONFIG", defaults.model_config_path)
        ),
        account_model_key_file=Path(account_key_file) if account_key_file else None,
        library_mode=library_mode,
        candidate_pipeline=os.environ.get("CITATION_CANDIDATE_PIPELINE", "agentic"),
        listwise_score_review=_bool(
            "CITATION_LISTWISE_SCORE_REVIEW", defaults.listwise_score_review
        ),
        corpus_path=corpus_path,
        sample_corpus_path=sample_path,
        model_timeout_ms=int(
            os.environ.get("CITATION_MODEL_TIMEOUT_MS", defaults.model_timeout_ms)
        ),
        allow_user_models=_bool("CITATION_USER_MODELS_ENABLED", defaults.allow_user_models),
        demo_continuity=_bool("CITATION_DEMO_CONTINUITY", bundled_start),
        online_enabled=_bool("CITATION_EUROPEPMC_ONLINE", defaults.online_enabled),
        online_timeout_ms=int(
            os.environ.get("CITATION_ONLINE_TIMEOUT_MS", defaults.online_timeout_ms)
        ),
        online_result_limit=int(
            os.environ.get("CITATION_ONLINE_RESULT_LIMIT", defaults.online_result_limit)
        ),
        auth_required=_bool("CITATION_AUTH_REQUIRED"),
        accounts_enabled=_bool("CITATION_ACCOUNTS_ENABLED", defaults.accounts_enabled),
        learning_enabled=_bool("CITATION_LEARNING_ENABLED", defaults.learning_enabled),
        auth_db_path=Path(os.environ.get("CITATION_AUTH_DB_PATH", defaults.auth_db_path)),
        trusted_hosts=tuple(
            item.strip()
            for item in os.environ.get("CITATION_TRUSTED_HOSTS", "127.0.0.1,localhost").split(",")
            if item.strip()
        ),
        secure_cookies=_bool("CITATION_SECURE_COOKIES"),
        agent_timeout_ms=int(
            os.environ.get("CITATION_AGENT_TIMEOUT_MS", defaults.agent_timeout_ms)
        ),
        agent_full_text_timeout_ms=int(
            os.environ.get(
                "CITATION_AGENT_FULL_TEXT_TIMEOUT_MS", defaults.agent_full_text_timeout_ms
            )
        ),
        agent_max_tool_calls=int(
            os.environ.get("CITATION_AGENT_MAX_TOOL_CALLS", defaults.agent_max_tool_calls)
        ),
        agent_recursion_limit=int(
            os.environ.get("CITATION_AGENT_RECURSION_LIMIT", defaults.agent_recursion_limit)
        ),
        agent_max_concurrent_runs=int(
            os.environ.get("CITATION_AGENT_MAX_CONCURRENT_RUNS", defaults.agent_max_concurrent_runs)
        ),
        agent_source_evidence_first=_bool(
            "CITATION_AGENT_SOURCE_EVIDENCE_FIRST", defaults.agent_source_evidence_first
        ),
        agent_parent_hide_rank_rationale=_bool(
            "CITATION_AGENT_PARENT_HIDE_RANK_RATIONALE", defaults.agent_parent_hide_rank_rationale
        ),
        agent_parent_evidence_delta=_bool(
            "CITATION_AGENT_PARENT_EVIDENCE_DELTA", defaults.agent_parent_evidence_delta
        ),
        agent_parent_abstract_references=_bool(
            "CITATION_AGENT_PARENT_ABSTRACT_REFERENCES", defaults.agent_parent_abstract_references
        ),
        agent_literature_reader=_bool(
            "CITATION_AGENT_LITERATURE_READER", defaults.agent_literature_reader
        ),
        agent_manuscript_find_neighbors=_bool(
            "CITATION_AGENT_MANUSCRIPT_FIND_NEIGHBORS", defaults.agent_manuscript_find_neighbors
        ),
        agent_research_workspace=_bool(
            "CITATION_AGENT_RESEARCH_WORKSPACE", defaults.agent_research_workspace
        ),
        agent_research_context=_bool(
            "CITATION_AGENT_RESEARCH_CONTEXT", defaults.agent_research_context
        ),
        agent_workspace_abstracts=_bool(
            "CITATION_AGENT_WORKSPACE_ABSTRACTS", defaults.agent_workspace_abstracts
        ),
        agent_evidence_view=_bool("CITATION_AGENT_EVIDENCE_VIEW", defaults.agent_evidence_view),
        agent_abstract_evidence_mode=os.environ.get(
            "CITATION_AGENT_ABSTRACT_EVIDENCE_MODE", defaults.agent_abstract_evidence_mode
        ),
        base_path=base_path,
        embedding_enabled=_bool("CITATION_EMBEDDING_ENABLED"),
        embedding_model=os.environ.get("CITATION_EMBEDDING_MODEL", defaults.embedding_model),
        embedding_cache_dir=Path(
            os.environ.get("CITATION_EMBEDDING_CACHE_DIR", defaults.embedding_cache_dir)
        ),
        embedding_dense_threshold=float(
            os.environ.get("CITATION_EMBEDDING_DENSE_THRESHOLD", defaults.embedding_dense_threshold)
        ),
        manuscript_upload_enabled=_bool(
            "CITATION_MANUSCRIPT_UPLOAD_ENABLED", defaults.manuscript_upload_enabled
        ),
        manuscript_scan_required=_bool(
            "CITATION_MANUSCRIPT_SCAN_REQUIRED", defaults.manuscript_scan_required
        ),
        manuscript_scan_socket=os.environ.get(
            "CITATION_MANUSCRIPT_SCAN_SOCKET", defaults.manuscript_scan_socket
        ),
        manuscript_scan_timeout_s=int(
            os.environ.get("CITATION_MANUSCRIPT_SCAN_TIMEOUT_S", defaults.manuscript_scan_timeout_s)
        ),
        manuscript_max_upload_bytes=int(
            os.environ.get("CITATION_MANUSCRIPT_MAX_BYTES", defaults.manuscript_max_upload_bytes)
        ),
        manuscript_max_pages=int(
            os.environ.get("CITATION_MANUSCRIPT_MAX_PAGES", defaults.manuscript_max_pages)
        ),
        manuscript_parse_timeout_s=int(
            os.environ.get(
                "CITATION_MANUSCRIPT_PARSE_TIMEOUT_S", defaults.manuscript_parse_timeout_s
            )
        ),
        manuscript_max_concurrent=int(
            os.environ.get("CITATION_MANUSCRIPT_MAX_CONCURRENT", defaults.manuscript_max_concurrent)
        ),
        citation_text_max_concurrent=int(
            os.environ.get("CITATION_TEXT_MAX_CONCURRENT", defaults.citation_text_max_concurrent)
        ),
    )
    if settings.library_mode == "metadata":
        libraries = [
            LibraryConfig(
                library_id="europepmc-snapshot",
                display_name="多学科文献快照",
                fixture_set="",
                delay_ms=0,
                adapter_id=(
                    "metadata-hybrid-e5-rrf-v1" if settings.embedding_enabled
                    else "metadata-snapshot-bm25-v1"
                ),
                simulation=False,
                source_type="local_snapshot",
                corpus_path=settings.corpus_path,
            ),
            LibraryConfig(
                library_id="abiotech-sample",
                display_name="aBiotech 老师样例库",
                fixture_set="",
                delay_ms=0,
                adapter_id="metadata-snapshot-bm25-v1",
                simulation=False,
                source_type="local_snapshot",
                corpus_path=settings.sample_corpus_path,
            ),
        ]
        if settings.online_enabled:
            libraries.insert(
                0,
                LibraryConfig(
                    library_id="europepmc-online",
                    display_name="Europe PMC 在线",
                    fixture_set="",
                    delay_ms=0,
                    adapter_id="europepmc-online-api-v1",
                    simulation=False,
                    source_type="remote_api",
                ),
            )
        settings = replace(settings, libraries=tuple(libraries))
    settings.validate()
    return settings


def load_metadata_records(path: Path) -> list[dict[str, Any]]:
    if path.stat().st_size > 100_000_000:
        raise ValueError("metadata corpus exceeds the 100MB limit")
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list) or not records:
        raise ValueError("metadata corpus must contain records")
    required = ("work_id", "paper_id", "title", "doi", "source_metadata_url")
    for record in records:
        if not isinstance(record, dict) or not all(
            isinstance(record.get(field), str) and record[field].strip() for field in required
        ):
            raise ValueError("metadata record is missing required bibliographic identity")
        abstract = record.get("abstract")
        if record.get("evidence_level") == BIBLIOGRAPHIC_ONLY:
            if abstract != "":
                raise ValueError("bibliography-only record must have an empty abstract")
        elif not isinstance(abstract, str) or not abstract.strip():
            raise ValueError("metadata record must have an abstract or be marked bibliography-only")
        if record.get("simulation") is not False:
            raise ValueError("metadata adapter only accepts real records")
    return records


def build_metadata_adapter(
    settings: Settings, library: LibraryConfig | None = None, *, require_prebuilt: bool = False
):
    from .metadata_retrieval import MetadataCorpusAdapter

    library = library or next(
        item for item in settings.libraries if item.source_type == "local_snapshot"
    )
    if library.source_type != "local_snapshot" or library.corpus_path is None:
        raise ValueError("a local snapshot library with corpus_path is required")
    records = load_metadata_records(library.corpus_path)
    if settings.embedding_enabled and library.adapter_id == "metadata-hybrid-e5-rrf-v1":
        from .semantic import SemanticMetadataAdapter, hybrid_candidate_window

        return SemanticMetadataAdapter(
            library,
            records,
            settings=settings,
            dense_threshold=settings.embedding_dense_threshold,
            top_k=hybrid_candidate_window(settings),
            require_prebuilt=require_prebuilt,
        )
    return MetadataCorpusAdapter(library, records)


def build_metadata_adapters(settings: Settings, *, require_prebuilt: bool = False) -> dict:
    from .online import EuropePMCOnlineAdapter

    adapters = {}
    for library in settings.libraries:
        if library.source_type == "remote_api":
            adapters[library.library_id] = EuropePMCOnlineAdapter(
                library,
                result_limit=settings.online_result_limit,
                timeout_ms=settings.online_timeout_ms,
            )
        else:
            adapters[library.library_id] = build_metadata_adapter(
                settings, library, require_prebuilt=require_prebuilt
            )
    return adapters
