"""Validated local configuration; secrets live only in the ignored runtime directory."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUNDLED_CORPORA = PROJECT_ROOT / "corpora"
BUNDLED_CORPUS_PATH = BUNDLED_CORPORA / "europepmc-snapshot.json"
BUNDLED_SAMPLE_PATH = BUNDLED_CORPORA / "abiotech-sample.json"
BIBLIOGRAPHIC_ONLY = "bibliographic_metadata_only"
LOCAL_ONLY_ADAPTER_MODE = "real_abstract_metadata_local_only"
LOCAL_ONLY_NOTICE = "仅检索已选本地快照的题录与摘要；没有在线查库、模型重排、推荐理由或全文核查。"


@dataclass(frozen=True, slots=True)
class LibraryConfig:
    library_id: str
    display_name: str
    fixture_set: str
    delay_ms: int
    adapter_id: str = "fixture-local-v1"
    simulation: bool = True
    source_type: Literal["fixture", "local_snapshot", "remote_api"] = "fixture"
    corpus_path: Path | None = None
    record_count: int | None = None


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: Path = PROJECT_ROOT / "data" / "runtime" / "app.db"
    default_tenant: str = "local"
    libraries: tuple[LibraryConfig, ...] = (
        LibraryConfig(
            library_id="fixture-fast",
            display_name="本地演示快库（模拟夹具，非真实检索）",
            fixture_set="fast",
            delay_ms=250,
        ),
        LibraryConfig(
            library_id="fixture-slow",
            display_name="本地演示慢库（模拟夹具，非真实检索）",
            fixture_set="slow",
            delay_ms=1600,
        ),
    )
    sse_poll_ms: int = 50
    sse_heartbeat_ms: int = 15_000
    event_retention_max_per_query: int = 10_000
    default_soft_deadline_ms: int = 180_000
    max_soft_deadline_ms: int = 300_000
    max_library_delay_ms: int = 10_000
    fixture_demo_scope_default: bool = False
    model_config_path: Path | None = PROJECT_ROOT / "data" / "runtime" / "deepseek.local.json"
    model_max_inflight: int = 2
    model_timeout_ms: int = 45_000
    allow_user_models: bool = True
    account_model_key_file: Path | None = None
    demo_continuity: bool = False
    agent_timeout_ms: int = 900_000
    agent_full_text_timeout_ms: int = 1_800_000
    agent_max_tool_calls: int = 32
    agent_recursion_limit: int = 96
    agent_max_concurrent_runs: int = 2
    agent_evidence_view: bool = False
    agent_abstract_evidence_mode: Literal["legacy", "separate", "scoped"] = "legacy"
    agent_evidence_decision_guidance: bool = False
    agent_source_evidence_first: bool = False
    agent_parent_hide_rank_rationale: bool = False
    agent_parent_evidence_delta: bool = False
    agent_parent_abstract_references: bool = False
    agent_literature_reader: bool = False
    agent_manuscript_find_neighbors: bool = False
    agent_research_workspace: bool = False
    agent_research_context: bool = False
    agent_workspace_abstracts: bool = False
    candidate_pipeline: Literal["agentic", "pointwise"] = "agentic"
    listwise_score_review: bool = True
    library_mode: str = "fixture"
    corpus_path: Path = PROJECT_ROOT / "data" / "runtime" / "quality-corpus" / "real-corpus.json"
    sample_corpus_path: Path = (
        PROJECT_ROOT / "data" / "runtime" / "quality-corpus" / "real-corpus.json"
    )
    online_enabled: bool = True
    online_timeout_ms: int = 20_000
    online_result_limit: int = 50
    auth_required: bool = False
    accounts_enabled: bool = True
    learning_enabled: bool = False
    auth_db_path: Path = PROJECT_ROOT / "data" / "runtime" / "auth.db"
    trusted_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")
    secure_cookies: bool = False
    base_path: str = ""
    embedding_enabled: bool = False
    embedding_model: str = "intfloat/multilingual-e5-small"
    embedding_cache_dir: Path = PROJECT_ROOT / "data" / "runtime" / "embeddings"
    embedding_dense_threshold: float = 0.80
    manuscript_upload_enabled: bool = True
    manuscript_scan_required: bool = False
    manuscript_scan_socket: str = "/run/citation-av/clamd.sock"
    manuscript_scan_timeout_s: int = 60
    manuscript_max_upload_bytes: int = 50_000_000
    manuscript_max_pages: int = 300
    manuscript_parse_timeout_s: int = 180
    manuscript_max_concurrent: int = 1
    citation_text_max_concurrent: int = 2

    def validate(self) -> None:
        ids = [library.library_id for library in self.libraries]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("libraries must be non-empty with unique library_id values")
        if not self.default_tenant.strip() or ":" in self.default_tenant:
            raise ValueError("default_tenant must be non-empty and outside the account namespace")
        if self.sse_poll_ms < 1 or self.sse_poll_ms > 1_000:
            raise ValueError("sse_poll_ms must be between 1 and 1000")
        if self.sse_heartbeat_ms <= self.sse_poll_ms:
            raise ValueError("sse_heartbeat_ms must be greater than sse_poll_ms")
        if self.event_retention_max_per_query < 1:
            raise ValueError("event_retention_max_per_query must be positive")
        if not 100 <= self.default_soft_deadline_ms <= self.max_soft_deadline_ms:
            raise ValueError("default_soft_deadline_ms is outside supported bounds")
        for library in self.libraries:
            if not library.library_id.strip():
                raise ValueError("library_id must not be empty")
            if not 0 <= library.delay_ms <= self.max_library_delay_ms:
                raise ValueError(f"{library.library_id}.delay_ms is outside supported bounds")
        if not self.db_path.name:
            raise ValueError("db_path must point to a file")
        if self.model_max_inflight < 1 or self.model_max_inflight > 8:
            raise ValueError("model_max_inflight must be between 1 and 8")
        if not 1_000 <= self.model_timeout_ms <= self.max_soft_deadline_ms:
            raise ValueError("model_timeout_ms is outside supported bounds")
        if not 5_000 <= self.agent_timeout_ms <= 900_000:
            raise ValueError("agent_timeout_ms is outside supported bounds")
        if not 5_000 <= self.agent_full_text_timeout_ms <= 3_600_000:
            raise ValueError("agent_full_text_timeout_ms is outside supported bounds")
        if not 1 <= self.agent_max_tool_calls <= 64:
            raise ValueError("agent_max_tool_calls must be between 1 and 64")
        if not 4 <= self.agent_recursion_limit <= 160:
            raise ValueError("agent_recursion_limit must be between 4 and 160")
        if not 1 <= self.agent_max_concurrent_runs <= 8:
            raise ValueError("agent_max_concurrent_runs must be between 1 and 8")
        if self.agent_research_context and not self.agent_research_workspace:
            raise ValueError("agent_research_context requires a research workspace for readback")
        if self.agent_workspace_abstracts and not self.agent_research_workspace:
            raise ValueError("agent_workspace_abstracts requires a research workspace")
        if self.agent_abstract_evidence_mode not in {"legacy", "separate", "scoped"}:
            raise ValueError("agent_abstract_evidence_mode must be legacy, separate or scoped")
        if self.agent_evidence_view and self.agent_abstract_evidence_mode != "legacy":
            raise ValueError("the compact and abstract evidence views cannot be combined")
        if self.candidate_pipeline not in {"agentic", "pointwise"}:
            raise ValueError("candidate_pipeline must be agentic or pointwise")
        if self.library_mode not in {"fixture", "metadata"}:
            raise ValueError("library_mode must be fixture or metadata")
        if not 1_000 <= self.online_timeout_ms <= 60_000:
            raise ValueError("online_timeout_ms must be between 1000 and 60000")
        if not 5 <= self.online_result_limit <= 100:
            raise ValueError("online_result_limit must be between 5 and 100")
        for library in self.libraries:
            if library.record_count is not None and library.record_count < 0:
                raise ValueError(f"{library.library_id}.record_count must not be negative")
            if library.source_type == "local_snapshot" and library.corpus_path is None:
                raise ValueError(f"{library.library_id} requires a corpus_path")
        if not self.trusted_hosts or any(not host.strip() for host in self.trusted_hosts):
            raise ValueError("trusted_hosts must contain non-empty hostnames")
        if self.base_path and (not self.base_path.startswith("/") or self.base_path.endswith("/")):
            raise ValueError("base_path must be empty or an absolute path without trailing slash")
        if not self.embedding_model.strip() or not self.embedding_cache_dir.name:
            raise ValueError("embedding model and cache directory are required")
        if not 0 <= self.embedding_dense_threshold <= 1:
            raise ValueError("embedding_dense_threshold must be between 0 and 1")
        if (
            not self.manuscript_scan_socket.startswith("/")
            or "\0" in self.manuscript_scan_socket
            or len(self.manuscript_scan_socket.encode("utf-8")) > 103
        ):
            raise ValueError("manuscript_scan_socket must be a bounded absolute Unix socket path")
        if not 1 <= self.manuscript_scan_timeout_s <= 90:
            raise ValueError("manuscript_scan_timeout_s must be between 1 and 90")
        if not 1_000 <= self.manuscript_max_upload_bytes <= 50_000_000:
            raise ValueError("manuscript_max_upload_bytes must be between 1000 and 50000000")
        if not 1 <= self.manuscript_max_pages <= 300:
            raise ValueError("manuscript_max_pages must be between 1 and 300")
        if not 10 <= self.manuscript_parse_timeout_s <= 900:
            raise ValueError("manuscript_parse_timeout_s must be between 10 and 900")
        if not 1 <= self.manuscript_max_concurrent <= 2:
            raise ValueError("manuscript_max_concurrent must be between 1 and 2")
        if not 1 <= self.citation_text_max_concurrent <= 4:
            raise ValueError("citation_text_max_concurrent must be between 1 and 4")

    def library(self, library_id: str) -> LibraryConfig:
        for library in self.libraries:
            if library.library_id == library_id:
                return library
        raise KeyError(library_id)

    @classmethod
    def for_tests(cls, tmp_path: Path, *, retention: int = 10_000) -> Settings:
        """Fast, isolated settings; tests still control item delays explicitly."""
        settings = replace(
            cls(),
            db_path=tmp_path / "app.db",
            sse_poll_ms=10,
            sse_heartbeat_ms=1_000,
            event_retention_max_per_query=retention,
            default_soft_deadline_ms=5_000,
            fixture_demo_scope_default=True,
            model_config_path=None,
            candidate_pipeline="pointwise",
        )
        settings.validate()
        return settings
