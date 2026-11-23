from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from tools.benchmark.core import ROOT, BenchmarkUsageCallback
from tools.gap_benchmark.data import RUNTIME, load_data
from tools.kimi_chat_model import KimiLunaConfig, KimiLunaGateway
from tools.luna_gap_retest import prepare_run
from tools.luna_product_retest import (
    CONTRACT_SCHEMA,
    EXPECTED_CASES,
    EXPECTED_RECORDS,
    SCHEMA,
    build_product_adapter,
    build_product_app,
    build_record,
    collect_model_audit,
    ensure_product_contract,
    failure_record,
    load_reusable_record,
    prepare,
    product_contract,
    product_settings,
    request_payload,
    require_prebuilt_index,
)
from tools.luna_trial_server import (
    LIBRARY_IDS,
    TRIAL_HOST,
    TRIAL_ROOT_ENV,
    _verify_snapshot_cache,
    create_trial_app,
    trial_root,
    trial_settings,
    validate_trial_adapters,
)
from tools.quality_benchmark.core import EMBEDDING_CACHE_DIR

from citation_tool.config import Settings
from citation_tool.retrieval import build_fixture_adapters
from citation_tool.runtime import build_metadata_adapters
from conftest import require_runtime_file

RUN_NAME = "scientific-gap-luna-dev-20261117-v1"


@pytest.fixture(scope="module")
def frozen() -> tuple[dict, dict]:
    require_runtime_file(RUNTIME / "corpus.json")
    return load_data()


def write_artifact(
    directory: Path,
    name: str,
    *,
    model: str = "gpt-6-luna",
    valid: bool = True,
    session: str = "session-1",
    calls: int = 1,
    error: dict | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        json.dumps(
            {
                "schema": "kimi-chat-model-call-v1",
                "call_id": name.removesuffix(".json"),
                "session_id": session,
                "model_observed": [model],
                "model_valid": valid,
                "model_calls": calls,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_tokens": 0,
                    "cache_creation_tokens": 0,
                    "model_calls": calls,
                },
                "duration_ms": 12.5,
                "tool_calls_requested": [],
                "tool_choice": None,
                "json_mode": True,
                "request_sha256": f"hash-{name}",
                "error": error,
            }
        ),
        encoding="utf-8",
    )
    return path


def sample_query() -> dict:
    return {
        "query_id": "q1",
        "input_type": "abstract",
        "search_state": "completed",
        "coverage": {"partial": False},
        "requested_library_ids": ["scifact"],
        "results": [],
        "candidate_diagnostics": [
            {
                "work_id": "work:scifact:1",
                "title": "A",
                "doi": None,
                "candidate_score": 10.0,
                "base_rank": 1,
                "llm_rank": 1,
                "ranking_status": "completed",
                "publication_types": [],
                "reason": {
                    "status": "generated",
                    "text": "grounded reason",
                    "served_model": "gpt-6-luna",
                },
                "evidence_excerpt": "evidence",
                "source_uri": "https://example.test/1",
            },
            {
                "work_id": "work:scifact:2",
                "title": "B",
                "doi": None,
                "candidate_score": 5.0,
                "base_rank": 2,
                "llm_rank": 2,
                "ranking_status": "failed",
                "publication_types": [],
                "reason": {
                    "status": "failed",
                    "failure_code": "MODEL_UNGROUNDED_OUTPUT",
                    "served_model": None,
                },
                "evidence_excerpt": "evidence",
                "source_uri": "https://example.test/2",
            },
        ],
        "candidate_rankings": [
            {"status": "completed", "failure_code": None},
            {"status": "failed", "failure_code": "LISTWISE_INVALID"},
        ],
        "workers": [
            {
                "source_available_work_ids": ["work:scifact:1"],
                "source_actions": [{"tool": "search_source", "executed": True}],
            }
        ],
        "final_snapshot_id": "snap",
    }


def test_product_prepare_is_static_and_prebuilt(frozen) -> None:
    suite, corpus = frozen
    info = prepare(RUN_NAME)
    assert info["prepared_without_model_calls"] is True
    assert info["tasks"] == EXPECTED_CASES == len(suite["tasks"])
    assert info["corpus_records"] == EXPECTED_RECORDS == len(corpus["records"])
    assert info["expected_model"] == "gpt-6-luna"
    assert (ROOT / info["e5_cache"]).is_file()
    assert info["implementation_hash"]
    assert info["limits"] == {
        "timeout_seconds": 900,
        "steps": 96,
        "parent_tools": 32,
        "max_parallel_subjects": 2,
        "max_process_model_calls": 2,
    }


def test_product_request_does_not_include_gold(frozen) -> None:
    suite, _ = frozen
    task = suite["tasks"][0]
    payload, digest = request_payload(task)
    assert "expect" not in payload
    assert payload["abstract"] == task["request"]["abstract"]
    assert payload["message"] == task["baseline_query"]
    assert payload.get("claim_scope") == task["request"].get("claim_scope")
    assert payload.get("topic_context") == task["request"].get("topic_context")
    from citation_tool.agent import AgentRunCreate, _evaluation_anchor

    anchor = _evaluation_anchor(AgentRunCreate.model_validate(payload).search_create())
    assert anchor.claim_text == task["request"]["abstract"]
    assert digest


def test_product_settings_and_adapter_use_prebuilt_cache(tmp_path, frozen) -> None:
    suite, corpus = frozen
    task = suite["tasks"][0]
    runtime = tmp_path / "case"
    settings = product_settings(runtime, task)
    assert settings.model_config_path is None
    assert settings.allow_user_models is False
    assert settings.libraries[0].library_id == "scifact"
    assert settings.db_path == runtime / "app.db"
    assert settings.embedding_cache_dir == EMBEDDING_CACHE_DIR
    assert settings.agent_timeout_ms == 900_000
    assert settings.agent_max_tool_calls == 32
    assert settings.agent_recursion_limit == 96
    index = require_prebuilt_index(corpus, settings)
    assert index.cache_path.is_file()
    adapter = build_product_adapter(settings, corpus, index, task)
    assert adapter.record_count == EXPECTED_RECORDS
    assert "bm25-plus-e5" in adapter.model_versions["retrieval"]
    assert adapter._top_k == 10
    pointwise = build_product_adapter(
        replace(settings, candidate_pipeline="pointwise"), corpus, index, task
    )
    assert pointwise._top_k == 5


def test_product_app_wiring_uses_kimi_without_model_calls(tmp_path) -> None:
    adapters = build_fixture_adapters(Settings.for_tests(tmp_path))
    adapter = next(iter(adapters.values()))
    settings = replace(Settings.for_tests(tmp_path), libraries=(adapter.library,))
    gateway = KimiLunaGateway(
        KimiLunaConfig(command=(sys.executable, "fake-kimi"), artifact_root=None)
    )
    app, reasons, models = build_product_app(settings, adapter, gateway, BenchmarkUsageCallback())
    assert app.state.agent_runner.provider == "kimi-code-cli"
    assert app.state.agent_runner.model_name == "gpt-6-luna"
    assert app.state.search_service.reason_provider == "kimi-code-cli"
    assert app.state.citation_texts._gateway is gateway
    assert reasons._cache is app.state.store
    assert models.model_name == "gpt-6-luna"
    asyncio.run(app.state.citation_texts.aclose())
    app.state.manuscripts.close()


def test_model_audit_verifies_each_artifact(tmp_path) -> None:
    calls = tmp_path / "calls"
    write_artifact(calls, "a.json", session="s1")
    write_artifact(calls, "b.json", session="s2")
    audit = collect_model_audit(calls)
    assert audit["artifact_count"] == 2
    assert audit["model_calls"] == 2
    assert audit["model_valid"] is True
    assert audit["session_isolation_valid"] is True
    assert audit["usage"]["input_tokens"] == 20
    assert audit["usage"]["output_tokens"] == 10
    write_artifact(calls, "c.json", model="deepseek-flash", valid=False, session="s3")
    audit = collect_model_audit(calls)
    assert audit["model_valid"] is False
    assert audit["served_models"] == ["deepseek-flash", "gpt-6-luna"]


def test_model_audit_retains_usage_incompleteness_without_invalidating_identity(tmp_path) -> None:
    calls = tmp_path / "calls"
    path = write_artifact(calls, "a.json", session="s1", calls=2)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["usage"].update(usage_report_count=1, usage_complete=False)
    path.write_text(json.dumps(value), encoding="utf-8")
    audit = collect_model_audit(calls)
    assert audit["model_valid"] is True
    assert audit["model_calls"] == 2
    assert audit["usage"]["usage_report_count"] == 1
    assert audit["usage"]["usage_complete"] is False
    assert "lower_bound" in audit["usage"]["measurement_boundary"]
    value["usage"].update(usage_report_count=2, usage_complete=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    assert collect_model_audit(calls)["usage"]["usage_complete"] is True


def test_legacy_or_absent_usage_reports_are_not_assumed_complete(tmp_path) -> None:
    calls = tmp_path / "calls"
    assert collect_model_audit(calls)["usage"]["usage_complete"] is False
    write_artifact(calls, "a.json")
    assert collect_model_audit(calls)["usage"]["usage_complete"] is False


def test_build_record_reports_reason_and_ranking_separately(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    write_artifact(runtime / "model-calls", "a.json", session="s1")
    audit = collect_model_audit(runtime / "model-calls")
    task = {
        "case_id": "sf-1",
        "run_case_id": "sf-1",
        "repetition": 1,
        "slice": "support",
        "family": "family-1",
    }
    run = {
        "status": "completed",
        "summary": '{"papers":[],"conclusion":"done"}',
        "run_id": "run-1",
        "served_model": "gpt-6-luna",
        "tool_calls": [{"name": "search_literature"}],
    }
    record = build_record(
        task,
        run=run,
        queries=[sample_query()],
        audit=audit,
        callback=BenchmarkUsageCallback(),
        runtime=runtime,
        root=tmp_path,
        implementation_hash="impl",
        request_sha256="request",
        started_at="2026-11-17T00:00:00Z",
        started=time.monotonic(),
        error=None,
        missing_query_states=[],
    )
    assert record["schema"] == SCHEMA
    assert record["completed"] is True
    assert record["model_valid"] is True
    assert record["model_request_count"] == 1
    assert record["model_calls"][0]["model_observed"] == ["gpt-6-luna"]
    assert record["reason_metrics"]["total"] == 2
    assert record["reason_metrics"]["generated"] == 1
    assert record["reason_metrics"]["failed"] == 1
    assert record["reason_metrics"]["all_generated"] is False
    assert record["ranking_metrics"]["total"] == 2
    assert record["ranking_metrics"]["completed"] == 1
    assert record["ranking_metrics"]["failed"] == 1
    assert record["ranking_metrics"]["all_completed"] is False
    assert record["queries"][0]["query_id"] == "q1"
    assert record["gold_sent_to_model"] is False
    assert Path(record["runtime_dir"]) == Path("runtime")
    assert Path(record["model_audit_dir"]) == Path("runtime/model-calls")


def test_build_record_marks_invalid_model(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    write_artifact(runtime / "model-calls", "a.json", model="deepseek-flash", valid=False)
    audit = collect_model_audit(runtime / "model-calls")
    task = {
        "case_id": "sf-1",
        "run_case_id": "sf-1",
        "repetition": 1,
        "slice": "support",
        "family": "family-1",
    }
    record = build_record(
        task,
        run={"status": "completed", "summary": "x"},
        queries=[],
        audit=audit,
        callback=BenchmarkUsageCallback(),
        runtime=runtime,
        root=tmp_path,
        implementation_hash="impl",
        request_sha256=None,
        started_at="2026-11-17T00:00:00Z",
        started=time.monotonic(),
        error=None,
        missing_query_states=[],
    )
    assert record["model_valid"] is False
    assert record["model_observed"] == ["deepseek-flash"]


def test_failure_record_preserves_error_and_model_audit(tmp_path, frozen) -> None:
    suite, _ = frozen
    runtime = tmp_path / "runtime"
    write_artifact(runtime / "model-calls", "a.json", session="s1")
    record = failure_record(
        suite["tasks"][0],
        runtime=runtime,
        root=tmp_path,
        implementation_hash="impl",
        error={"code": "BENCHMARK_TIMEOUT", "message": "timed out"},
        started_at="2026-11-17T00:00:00Z",
        started=time.monotonic(),
    )
    assert record["completed"] is False
    assert record["error"]["code"] == "BENCHMARK_TIMEOUT"
    assert record["response_text"] == ""
    assert record["model_valid"] is True
    assert record["model_request_count"] == 1
    assert record["reason_metrics"]["total"] == 0
    assert record["ranking_metrics"]["total"] == 0


def test_resume_requires_flag_and_same_hash(tmp_path) -> None:
    path = tmp_path / "record.json"
    value = {
        "schema": SCHEMA,
        "system": "product",
        "implementation_hash": "same",
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(FileExistsError):
        load_reusable_record(path, resume=False, implementation_hash="same")
    with pytest.raises(ValueError):
        load_reusable_record(path, resume=True, implementation_hash="other")
    assert load_reusable_record(path, resume=True, implementation_hash="same") == value
    assert (
        load_reusable_record(tmp_path / "missing.json", resume=True, implementation_hash="x")
        is None
    )


def test_product_contract_is_frozen(tmp_path, frozen) -> None:
    suite, corpus = frozen
    real_root, _, _ = prepare_run(RUN_NAME)
    root = tmp_path / "run"
    root.mkdir()
    shutil.copy(real_root / "plan.json", root / "plan.json")
    contract = product_contract(root, suite, corpus)
    assert contract["schema"] == CONTRACT_SCHEMA
    assert contract["tasks"] == EXPECTED_CASES
    assert contract["corpus_records"] == EXPECTED_RECORDS
    assert contract["model_alias"] == "OpenAI/gpt-6-luna"
    assert contract["expected_model"] == "gpt-6-luna"
    assert contract["limits"]["max_process_model_calls"] == 2
    ensure_product_contract(root, contract)
    ensure_product_contract(root, contract)
    changed = dict(contract)
    changed["corpus_sha256"] = "changed"
    with pytest.raises(ValueError):
        ensure_product_contract(root, changed)


def test_trial_settings_have_three_real_sources_and_isolated_paths(tmp_path) -> None:
    root = tmp_path / "trial"
    settings = trial_settings(root)
    assert tuple(library.library_id for library in settings.libraries) == LIBRARY_IDS
    assert {library.source_type for library in settings.libraries} == {
        "remote_api",
        "local_snapshot",
    }
    assert settings.db_path == root / "trial.db"
    assert settings.auth_db_path == root / "auth.db"
    assert settings.model_config_path is None
    assert settings.allow_user_models is False
    assert settings.embedding_cache_dir == EMBEDDING_CACHE_DIR
    assert settings.online_enabled is True
    assert settings.agent_timeout_ms == 900_000
    assert settings.citation_text_max_concurrent == 1
    assert settings.manuscript_upload_enabled is True
    assert TRIAL_HOST == "127.0.0.1"


def test_trial_root_env_creates_new_runtime(tmp_path, monkeypatch) -> None:
    target = tmp_path / "new-trial"
    monkeypatch.setenv(TRIAL_ROOT_ENV, str(target))
    assert trial_root() == target
    assert target.is_dir()


def test_trial_adapters_are_exactly_three_and_cache_is_prebuilt(tmp_path) -> None:
    settings = trial_settings(tmp_path / "trial")
    require_runtime_file(settings.corpus_path)
    require_runtime_file(settings.sample_corpus_path)
    adapters = build_metadata_adapters(settings)
    validate_trial_adapters(adapters)
    _verify_snapshot_cache(adapters)
    assert adapters["europepmc-snapshot"].record_count == 11002
    assert adapters["abiotech-sample"].record_count == 50
    with pytest.raises(ValueError):
        validate_trial_adapters({"europepmc-online": object()})


def test_trial_factory_rejects_incomplete_sources_without_starting(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(TRIAL_ROOT_ENV, str(tmp_path / "trial-root"))
    settings = trial_settings(tmp_path / "trial")
    gateway = KimiLunaGateway(
        KimiLunaConfig(command=(sys.executable, "fake-kimi"), artifact_root=None)
    )
    with pytest.raises(ValueError):
        create_trial_app(
            settings=settings,
            gateway=gateway,
            adapters={"europepmc-online": object()},
        )
