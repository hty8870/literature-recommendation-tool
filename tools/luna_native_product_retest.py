"""Native OpenAI Responses retest of the unchanged, inspected SciFact development tasks."""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult
from tools import luna_product_retest as shared
from tools.benchmark.core import ROOT, BenchmarkUsageCallback, file_hash
from tools.gap_benchmark.data import save_new
from tools.luna_credentials import MODEL, authorized_config
from tools.luna_gap_retest import prepare_run

from citation_tool.entities import utc_now_iso
from citation_tool.jcs import sha256_json
from citation_tool.listwise import DiscoveryModels
from citation_tool.reasons import DeepSeekModelGateway, DeepSeekReasonGenerator, ModelConfig

TRANSPORT = "openai-responses-native-v1"
AUDIT_VERSION = "native-responses-audit-v1"
TRANSPORT_NOTE = (
    "Product uses native OpenAI Responses with store=false and encrypted reasoning context; "
    "the reused Kimi baseline uses its normal tools/skills in isolated CLI sessions. "
    "Native response IDs and LangChain spans are not Kimi session/export records. "
    "This inspected development comparison does not isolate architecture or thinking settings."
)
USAGE_BOUNDARY = (
    "Logical LangChain dispatches and reported API response usage only; SDK-internal HTTP "
    "retry attempts and unreturned usage are not observed. Totals are observable lower bounds."
)
MAX_PARALLEL_SUBJECTS = 1
MAX_MODEL_CALLS = 2


class NativeModelSlots:
    def __init__(self, limit: int = MAX_MODEL_CALLS) -> None:
        if limit != MAX_MODEL_CALLS:
            raise ValueError("native retest requires the shared two-call envelope")
        self.semaphore = asyncio.Semaphore(limit)
        self.active = 0
        self.maximum = 0
        self.idle = asyncio.Event()
        self.idle.set()

    async def acquire(self) -> None:
        await self.semaphore.acquire()
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.idle.clear()

    def release(self) -> None:
        if self.active <= 0:
            raise RuntimeError("native model slot released without ownership")
        self.active -= 1
        self.semaphore.release()
        if not self.active:
            self.idle.set()

    async def wait_idle(self) -> None:
        await asyncio.wait_for(self.idle.wait(), timeout=10)


def _response_metadata(response: LLMResult) -> tuple[list[str], list[str], dict, bool]:
    models: set[str] = set()
    ids: list[str] = []
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "reasoning_tokens": 0,
    }
    messages = 0
    reports = 0
    for group in response.generations:
        for generation in group:
            message = getattr(generation, "message", None)
            metadata = getattr(message, "response_metadata", {}) or {}
            model = metadata.get("model_name") or metadata.get("model")
            response_id = metadata.get("id") or metadata.get("response_id")
            if not response_id:
                candidate_id = getattr(message, "id", None)
                response_id = candidate_id if str(candidate_id).startswith("resp_") else None
            messages += 1
            models.add(shared._redact(model) if isinstance(model, str) and model else "unverified")
            if isinstance(response_id, str) and response_id and not response_id.startswith("sk-"):
                ids.append(response_id[:200])
            values = getattr(message, "usage_metadata", None)
            if not isinstance(values, dict) or not all(
                isinstance(values.get(key), int) and values[key] >= 0
                for key in ("input_tokens", "output_tokens")
            ):
                continue
            reports += 1
            usage["input_tokens"] += int(values.get("input_tokens", 0) or 0)
            usage["output_tokens"] += int(values.get("output_tokens", 0) or 0)
            inputs = values.get("input_token_details") or {}
            outputs = values.get("output_token_details") or {}
            usage["cache_read_tokens"] += int(inputs.get("cache_read", 0) or 0)
            usage["cache_creation_tokens"] += int(inputs.get("cache_creation", 0) or 0)
            usage["reasoning_tokens"] += int(outputs.get("reasoning", 0) or 0)
    usage["usage_report_count"] = reports
    usage["response_message_count"] = messages
    return sorted(models), ids, usage, bool(messages and reports == messages)


class NativeModelAudit(AsyncCallbackHandler):
    run_inline = True
    raise_error = True

    def __init__(self, directory: Path, slots: NativeModelSlots) -> None:
        self.directory = directory
        self.slots = slots
        self.usage_callback: BenchmarkUsageCallback | None = None
        self._pending: dict[str, dict] = {}
        self._leased: set[str] = set()
        self._started: dict[str, float] = {}
        self._sealed = False

    def seal(self) -> None:
        if self._pending or self._leased or self.slots.active:
            raise RuntimeError("native model calls remain during case closure")
        self._sealed = True

    def _persist(self, row: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self.directory / f"{row['call_id']}.json"
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(destination)

    async def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs) -> None:
        if self._sealed:
            raise RuntimeError("native case closed before model dispatch")
        call_id = str(uuid.UUID(str(run_id)))
        row = {
            "schema": AUDIT_VERSION,
            "call_id": call_id,
            "langchain_run_id": call_id,
            "transport": TRANSPORT,
            "expected_model": MODEL,
            "model_observed": [],
            "response_ids": [],
            "status": "queued",
            "model_valid": False,
            "model_calls": 0,
            "usage": {},
            "started_at": utc_now_iso(),
            "error": None,
        }
        self._pending[call_id] = row
        self._started[call_id] = time.monotonic()
        self._persist(row)
        try:
            await self.slots.acquire()
            self._leased.add(call_id)
            row.update(status="started", model_calls=1, dispatched_at=utc_now_iso())
            self._persist(row)
        except BaseException as exc:
            row.update(status="failed", error={"type": type(exc).__name__})
            self._persist(row)
            self._release(call_id)
            self._pending.pop(call_id, None)
            raise

    def _release(self, call_id: str) -> None:
        if call_id in self._leased:
            self._leased.remove(call_id)
            self.slots.release()

    def _unmatched(self, run_id, event: str) -> None:
        self._persist(
            {
                "schema": AUDIT_VERSION,
                "call_id": uuid.uuid4().hex,
                "langchain_run_id": str(run_id),
                "transport": TRANSPORT,
                "status": "audit_error",
                "model_valid": False,
                "model_observed": [],
                "response_ids": [],
                "model_calls": 0,
                "usage": {},
                "error": {"type": f"unmatched_{event}"},
            }
        )

    async def on_llm_end(self, response: LLMResult, *, run_id, **kwargs) -> None:
        if self._sealed:
            raise RuntimeError("native case audit is closed")
        call_id = str(run_id)
        row = self._pending.pop(call_id, None)
        if row is None:
            self._unmatched(run_id, "end")
            return
        try:
            models, response_ids, usage, reports_complete = _response_metadata(response)
            valid = (
                models == [MODEL]
                and bool(response_ids)
                and len(response_ids) == usage["response_message_count"]
                and len(response_ids) == len(set(response_ids))
            )
            row.update(
                status="completed",
                model_observed=models,
                response_ids=response_ids,
                model_valid=valid,
                usage=usage,
                reported_usage_complete=reports_complete,
                duration_ms=round((time.monotonic() - self._started[call_id]) * 1000, 3),
                max_observed_inflight=self.slots.maximum,
            )
            if self.usage_callback is not None:
                for model in models:
                    self.usage_callback.totals.served_models.add(model)
                self.usage_callback.totals.input_tokens += usage["input_tokens"]
                self.usage_callback.totals.output_tokens += usage["output_tokens"]
                self.usage_callback.totals.cache_read += usage["cache_read_tokens"]
                self.usage_callback.totals.cache_creation += usage["cache_creation_tokens"]
                self.usage_callback.totals.calls += 1
            self._persist(row)
        finally:
            self._release(call_id)

    async def close_cancelled_invocation(self, run_id, error: asyncio.CancelledError) -> None:
        if str(run_id) in self._pending:
            await self.on_llm_error(error, run_id=run_id)

    async def on_llm_error(self, error: BaseException, *, run_id, **kwargs) -> None:
        if self._sealed:
            raise RuntimeError("native case audit is closed")
        call_id = str(run_id)
        row = self._pending.pop(call_id, None)
        if row is None:
            self._unmatched(run_id, "error")
            return
        try:
            row.update(
                status="failed",
                error={"type": type(error).__name__},
                duration_ms=round((time.monotonic() - self._started[call_id]) * 1000, 3),
            )
            self._persist(row)
        finally:
            self._release(call_id)


def collect_model_audit(directory: Path) -> dict[str, Any]:
    rows, errors = [], []
    for path in sorted(directory.glob("*.json")) if directory.exists() else []:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            if row.get("schema") != AUDIT_VERSION or row.get("transport") != TRANSPORT:
                raise ValueError("unexpected native audit schema")
            rows.append(row)
        except (OSError, ValueError, TypeError) as exc:
            errors.append({"path": path.name, "type": type(exc).__name__})
    rows.sort(key=lambda row: (row.get("started_at", ""), row.get("call_id", "")))
    models = sorted({model for row in rows for model in row.get("model_observed", [])})
    ids = [value for row in rows for value in row.get("response_ids", [])]
    unique_ids = bool(ids) and len(ids) == len(set(ids))
    verified = (
        bool(rows)
        and not errors
        and unique_ids
        and all(
            row.get("status") == "completed"
            and row.get("model_valid") is True
            and row.get("model_observed") == [MODEL]
            for row in rows
        )
    )
    usage = {
        key: sum(int((row.get("usage") or {}).get(key, 0) or 0) for row in rows)
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "reasoning_tokens",
            "usage_report_count",
        )
    }
    calls = sum(int(row.get("model_calls", 0) or 0) for row in rows)
    usage.update(
        model_calls=calls,
        served_models=models,
        usage_complete=False,
        reported_response_usage_complete=bool(rows)
        and all(row.get("reported_usage_complete") is True for row in rows),
        measurement_boundary=USAGE_BOUNDARY,
        uncached_input_tokens=max(0, usage["input_tokens"] - usage["cache_read_tokens"]),
    )
    return {
        "artifacts": rows,
        "artifact_errors": errors,
        "artifact_count": len(rows),
        "model_calls": calls,
        "usage": usage,
        "served_models": models,
        "response_ids": ids,
        "response_ids_unique": unique_ids,
        "model_valid": verified,
        "session_ids": [],
        "session_isolation_valid": False,
    }


class NativeLunaGateway(DeepSeekModelGateway):
    def __init__(self, config: ModelConfig, audit: NativeModelAudit) -> None:
        config.validate()
        if (
            config.provider != "openai"
            or config.model != MODEL
            or config.api_type != "responses"
            or config.base_url != "https://api.openai.com/v1"
        ):
            raise ValueError("native retest requires the authorized official Luna Responses config")
        self.audit = audit
        self._models = []
        super().__init__(
            config, max_inflight=MAX_MODEL_CALLS, timeout_ms=shared.MODEL_TIMEOUT_S * 1000
        )

    def _client(self, **kwargs):
        model = super()._client(**kwargs)
        model.callbacks = [self.audit]
        self._models.append(model)
        return model

    async def aclose(self) -> None:
        await self.audit.slots.wait_idle()
        self.audit.seal()
        for model in self._models:
            if model.http_async_client is not None:
                await model.http_async_client.aclose()
            if model.http_client is not None:
                model.http_client.close()


def build_product_app(settings, adapter, gateway: NativeLunaGateway, callback):
    from citation_tool.api import create_app

    gateway.audit.usage_callback = callback
    reasons = DeepSeekReasonGenerator(
        gateway, max_inflight=MAX_MODEL_CALLS, timeout_ms=int(shared.MODEL_TIMEOUT_S * 1000)
    )
    models = DiscoveryModels.from_reasons(reasons)
    app = create_app(
        settings,
        adapters={adapter.library.library_id: adapter},
        reason_generator=reasons,
        agent_model=gateway.agent_model(),
        discovery_models=models,
    )
    reasons._cache = app.state.store
    if (
        app.state.agent_runner.model_name != MODEL
        or app.state.search_service.reason_provider != "openai"
    ):
        raise ValueError("native product model wiring differs")
    return app, reasons, models


def _native_record(record: dict, audit: dict, runtime: Path) -> dict:
    served = set(audit["served_models"]) | set(record.get("model_observed", []))
    record.update(
        model_requested=MODEL,
        model_observed=sorted(served),
        model_valid=bool(audit["model_valid"]) and served == {MODEL},
        usage=audit["usage"],
        model_calls=audit["artifacts"],
        model_artifact_count=audit["artifact_count"],
        model_request_count=audit["model_calls"],
        model_artifact_errors=audit["artifact_errors"],
        transport=TRANSPORT,
        transport_boundary=TRANSPORT_NOTE,
        model_request_count_basis="logical LangChain dispatches",
        session_isolation_valid=bool(record.get("agent_run_id")) and audit["response_ids_unique"],
        isolation_basis="fresh case database/AgentRun; unique native response IDs; store=false",
        native_response_ids=audit["response_ids"],
        kimi_session_ids=[],
        case_database=str(runtime / "app.db"),
    )
    return record


async def run_case(
    task,
    *,
    root,
    runtime,
    corpus,
    index,
    implementation_hash,
    config,
    slots,
    app_builder=build_product_app,
):
    audit = NativeModelAudit(runtime / "model-calls", slots)
    gateway = NativeLunaGateway(config, audit)
    started, started_at = time.monotonic(), utc_now_iso()
    cleanup_error = None
    try:
        record = await shared.run_case(
            task,
            root=root,
            runtime=runtime,
            corpus=corpus,
            index=index,
            implementation_hash=implementation_hash,
            shared_async=asyncio.Semaphore(MAX_MODEL_CALLS),
            shared_sync=threading.BoundedSemaphore(MAX_MODEL_CALLS),
            gateway_override=gateway,
            app_builder=app_builder,
            audit_collector=collect_model_audit,
        )
    except Exception as exc:
        record = shared.build_record(
            task,
            run={},
            queries=[],
            audit=collect_model_audit(runtime / "model-calls"),
            callback=audit.usage_callback or BenchmarkUsageCallback(),
            runtime=runtime,
            root=root,
            implementation_hash=implementation_hash,
            request_sha256=None,
            started_at=started_at,
            started=started,
            error={"code": "NATIVE_PRODUCT_RUNNER_ERROR", "message": type(exc).__name__},
            missing_query_states=[],
        )
    finally:
        try:
            await gateway.aclose()
        except Exception as exc:
            cleanup_error = {"code": "NATIVE_MODEL_CLEANUP_FAILED", "message": type(exc).__name__}
    if cleanup_error:
        record.update(completed=False, error=cleanup_error)
    return _native_record(record, collect_model_audit(runtime / "model-calls"), runtime)


def product_implementation_hash(root: Path, corpus: dict) -> str:
    return sha256_json(
        {
            "shared_product": shared.product_implementation_hash(root, corpus),
            "native_runner": file_hash(Path(__file__)),
            "credential_loader_code": file_hash(ROOT / "tools/luna_credentials.py"),
            "lock": file_hash(ROOT / "uv.lock"),
            "manifest": file_hash(ROOT / "pyproject.toml"),
            "transport": TRANSPORT,
            "audit_version": AUDIT_VERSION,
        }
    )


def product_contract(root, suite, corpus) -> dict:
    contract = shared.product_contract(root, suite, corpus)
    contract.update(
        implementation_hash=product_implementation_hash(root, corpus),
        provider="openai",
        transport=TRANSPORT,
        transport_boundary=TRANSPORT_NOTE,
        audit_version=AUDIT_VERSION,
        api_endpoint="https://api.openai.com/v1",
        provider_storage_requested=False,
        thinking_configuration="provider default; not assumed equivalent to Kimi settings",
        usage_measurement=USAGE_BOUNDARY,
    )
    contract["limits"] = {**contract["limits"], "max_parallel_subjects": MAX_PARALLEL_SUBJECTS}
    return contract


def prepare(name: str) -> dict:
    root, suite, corpus = prepare_run(name)
    contract = product_contract(root, suite, corpus)
    return {
        "prepared_without_model_calls": True,
        "contract_written": False,
        "name": name,
        "root": str(root.relative_to(ROOT)),
        **{
            key: contract[key]
            for key in (
                "implementation_hash",
                "tasks",
                "corpus_records",
                "expected_model",
                "transport",
                "audit_version",
                "limits",
                "e5_cache",
                "e5_cache_sha256",
                "input_mapping",
            )
        },
    }


async def run_products(name: str, *, resume: bool, concurrency: int = 1) -> dict:
    if concurrency != MAX_PARALLEL_SUBJECTS:
        raise ValueError("native retest runs one isolated case at a time")
    root, suite, corpus = await asyncio.to_thread(prepare_run, name)
    contract = await asyncio.to_thread(product_contract, root, suite, corpus)
    await asyncio.to_thread(shared.ensure_product_contract, root, contract)
    index = await asyncio.to_thread(
        shared.require_prebuilt_index,
        corpus,
        shared.product_settings(root / "product-runtime" / "_preflight", suite["tasks"][0]),
    )
    records = []
    slots = NativeModelSlots()
    config = None
    for task in suite["tasks"]:
        path = root / "product" / f"{task['case_id']}.json"
        previous = await asyncio.to_thread(
            shared.load_reusable_record,
            path,
            resume=resume,
            implementation_hash=contract["implementation_hash"],
        )
        if previous is not None:
            if previous.get("transport") != TRANSPORT:
                raise ValueError("existing record uses another transport")
            records.append(previous)
            continue
        if config is None:
            config = await asyncio.to_thread(authorized_config)
        runtime = await asyncio.to_thread(shared.new_attempt_dir, root, task["case_id"])
        record = await run_case(
            task,
            root=root,
            runtime=runtime,
            corpus=corpus,
            index=index,
            implementation_hash=contract["implementation_hash"],
            config=config,
            slots=slots,
        )
        await asyncio.to_thread(save_new, path, record)
        records.append(record)
        if (record.get("error") or {}).get("code") == "NATIVE_MODEL_CLEANUP_FAILED":
            raise RuntimeError("native case cleanup failed; failed output retained before stopping")
        await slots.wait_idle()
        print(
            json.dumps(
                {
                    "case": task["case_id"],
                    "completed": record["completed"],
                    "model_valid": record["model_valid"],
                    "logical_model_calls": record["model_request_count"],
                    "reason_metrics": record["reason_metrics"],
                    "ranking_metrics": record["ranking_metrics"],
                    "error": record["error"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    after_hash = await asyncio.to_thread(product_implementation_hash, root, corpus)
    summary = {
        "schema": "scifact-luna-native-product-summary-v1",
        "name": name,
        "created_at": utc_now_iso(),
        "cases": len(records),
        "completed": sum(row["completed"] for row in records),
        "model_valid": sum(row["model_valid"] for row in records),
        "logical_model_calls": sum(row["model_request_count"] for row in records),
        "reason_generated": sum(row["reason_metrics"]["generated"] for row in records),
        "reason_total": sum(row["reason_metrics"]["total"] for row in records),
        "ranking_completed": sum(row["ranking_metrics"]["completed"] for row in records),
        "ranking_total": sum(row["ranking_metrics"]["total"] for row in records),
        "implementation_hash": contract["implementation_hash"],
        "implementation_changed_during_run": after_hash != contract["implementation_hash"],
        "transport": TRANSPORT,
        "transport_boundary": TRANSPORT_NOTE,
        "max_observed_model_inflight": max(
            [slots.maximum]
            + [
                int(call.get("max_observed_inflight", 0) or 0)
                for record in records
                for call in record.get("model_calls", [])
            ]
        ),
        "usage_measurement": USAGE_BOUNDARY,
    }
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    await asyncio.to_thread(save_new, root / f"native-product-summary-{stamp}.json", summary)
    if summary["implementation_changed_during_run"]:
        raise ValueError("native product implementation changed; records retained but invalid")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "run"])
    parser.add_argument("--name", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--concurrency", type=int, choices=[1], default=1)
    arguments = parser.parse_args()
    result = (
        prepare(arguments.name)
        if arguments.command == "prepare"
        else asyncio.run(
            run_products(arguments.name, resume=arguments.resume, concurrency=arguments.concurrency)
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
