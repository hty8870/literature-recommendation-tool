from __future__ import annotations

import asyncio
import json
import threading
import uuid
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from tools import luna_native_product_retest as native
from tools import luna_product_retest as shared
from tools.benchmark.core import BenchmarkUsageCallback
from tools.gap_benchmark.data import load_data

from citation_tool.config import Settings
from citation_tool.reasons import ModelConfig
from citation_tool.retrieval import build_fixture_adapters


def reply(model="gpt-6-luna", response_id="resp_test_1", *, with_usage=True):
    return LLMResult(
        generations=[
            [
                ChatGeneration(
                    message=AIMessage(
                        content=[
                            {
                                "type": "reasoning",
                                "summary": [{"type": "summary_text", "text": "PRIVATE_THOUGHT"}],
                            },
                            {"type": "text", "text": "PRIVATE_ANSWER"},
                        ],
                        response_metadata={
                            "model_name": model,
                            **({"id": response_id} if response_id else {}),
                        },
                        usage_metadata={
                            "input_tokens": 10,
                            "output_tokens": 5,
                            "total_tokens": 15,
                            "input_token_details": {"cache_read": 2},
                            "output_token_details": {"reasoning": 1},
                        }
                        if with_usage
                        else None,
                    )
                )
            ]
        ]
    )


def config():
    return ModelConfig(
        api_key="unit-test-credential",  # noqa: S106
        provider="openai",
        model="gpt-6-luna",
        base_url="https://api.openai.com/v1",
        api_type="responses",
    )


async def test_native_audit_uses_response_identity_without_persisting_prompt_or_reasoning(tmp_path):
    slots = native.NativeModelSlots()
    audit = native.NativeModelAudit(tmp_path, slots)
    callback = BenchmarkUsageCallback()
    audit.usage_callback = callback
    run_id = uuid.uuid4()
    await audit.on_chat_model_start(
        {"api_key": "DO_NOT_RECORD"}, [["PRIVATE_PROMPT"]], run_id=run_id
    )
    assert slots.active == 1
    await audit.on_llm_end(reply(), run_id=run_id)
    assert slots.active == 0
    result = native.collect_model_audit(tmp_path)
    assert result["model_valid"] is True
    assert result["response_ids"] == ["resp_test_1"]
    assert result["session_ids"] == []
    assert result["session_isolation_valid"] is False
    assert result["model_calls"] == 1
    assert result["usage"]["input_tokens"] == 10
    assert result["usage"]["reasoning_tokens"] == 1
    assert result["usage"]["reported_response_usage_complete"] is True
    assert result["usage"]["usage_complete"] is False
    assert callback.totals.served_models == {"gpt-6-luna"}
    serialized = "".join(path.read_text(encoding="utf-8") for path in tmp_path.glob("*.json"))
    assert "PRIVATE_" not in serialized
    assert "DO_NOT_RECORD" not in serialized
    assert "session_id" not in serialized


@pytest.mark.parametrize(
    ("model", "response_id"), [("deepseek-flash", "resp_other"), ("gpt-6-luna", None)]
)
async def test_native_audit_rejects_wrong_or_unverifiable_response_identity(
    tmp_path, model, response_id
):
    audit = native.NativeModelAudit(tmp_path, native.NativeModelSlots())
    run_id = uuid.uuid4()
    await audit.on_chat_model_start({}, [], run_id=run_id)
    await audit.on_llm_end(reply(model, response_id), run_id=run_id)
    assert native.collect_model_audit(tmp_path)["model_valid"] is False


async def test_native_audit_rejects_a_partly_missing_id_and_cross_call_reused_id(tmp_path):
    audit = native.NativeModelAudit(tmp_path, native.NativeModelSlots())
    run_id = uuid.uuid4()
    await audit.on_chat_model_start({}, [], run_id=run_id)
    mixed = reply()
    mixed.generations[0].extend(reply(response_id=None).generations[0])
    await audit.on_llm_end(mixed, run_id=run_id)
    assert native.collect_model_audit(tmp_path)["model_valid"] is False
    other = tmp_path / "duplicate"
    audit = native.NativeModelAudit(other, native.NativeModelSlots())
    for _ in range(2):
        run_id = uuid.uuid4()
        await audit.on_chat_model_start({}, [], run_id=run_id)
        await audit.on_llm_end(reply(response_id="resp_reused"), run_id=run_id)
    assert native.collect_model_audit(other)["response_ids_unique"] is False
    assert native.collect_model_audit(other)["model_valid"] is False


async def test_native_audit_never_assumes_missing_usage_is_zero_cost_or_unknown_model(tmp_path):
    audit = native.NativeModelAudit(tmp_path, native.NativeModelSlots())
    run_id = uuid.uuid4()
    await audit.on_chat_model_start({}, [], run_id=run_id)
    await audit.on_llm_end(reply(with_usage=False), run_id=run_id)
    result = native.collect_model_audit(tmp_path)
    assert result["model_valid"] is True
    assert result["usage"]["reported_response_usage_complete"] is False
    assert result["usage"]["usage_complete"] is False
    assert "lower bounds" in result["usage"]["measurement_boundary"]


async def test_native_case_seals_before_another_case_can_dispatch(tmp_path):
    audit = native.NativeModelAudit(tmp_path, native.NativeModelSlots())
    run_id = uuid.uuid4()
    await audit.on_chat_model_start({}, [], run_id=run_id)
    with pytest.raises(RuntimeError, match="remain"):
        audit.seal()
    await audit.on_llm_end(reply(), run_id=run_id)
    audit.seal()
    before = {path.name: path.read_bytes() for path in tmp_path.glob("*.json")}
    with pytest.raises(RuntimeError, match="closed"):
        await audit.on_chat_model_start({}, [], run_id=uuid.uuid4())
    with pytest.raises(RuntimeError, match="closed"):
        await audit.on_llm_end(reply(), run_id=run_id)
    with pytest.raises(RuntimeError, match="closed"):
        await audit.on_llm_error(RuntimeError(), run_id=run_id)
    assert before == {path.name: path.read_bytes() for path in tmp_path.glob("*.json")}


async def test_native_audit_saves_failed_and_unmatched_calls_without_error_message_leaks(tmp_path):
    audit = native.NativeModelAudit(tmp_path, native.NativeModelSlots())
    run_id = uuid.uuid4()
    await audit.on_chat_model_start({}, [], run_id=run_id)
    await audit.on_llm_error(RuntimeError("DO_NOT_RECORD"), run_id=run_id)
    await audit.on_llm_end(reply(), run_id=uuid.uuid4())
    result = native.collect_model_audit(tmp_path)
    assert result["model_valid"] is False
    assert result["model_calls"] == 1
    assert {row["status"] for row in result["artifacts"]} == {"failed", "audit_error"}
    assert "DO_NOT_RECORD" not in json.dumps(result)


async def test_native_audit_enforces_one_shared_two_call_envelope(tmp_path):
    slots = native.NativeModelSlots()
    audits = [native.NativeModelAudit(tmp_path / str(index), slots) for index in range(6)]

    async def one(index):
        run_id = uuid.uuid4()
        await audits[index].on_chat_model_start({}, [], run_id=run_id)
        await asyncio.sleep(0.01)
        assert slots.active <= 2
        await audits[index].on_llm_end(reply(response_id=f"resp_{index}"), run_id=run_id)

    await asyncio.gather(*(one(index) for index in range(6)))
    assert slots.maximum == 2
    assert slots.active == 0
    await slots.wait_idle()
    assert all(
        native.collect_model_audit(tmp_path / str(index))["model_valid"] for index in range(6)
    )


async def test_native_queued_cancellation_does_not_release_another_calls_slot(tmp_path):
    slots = native.NativeModelSlots()
    audit = native.NativeModelAudit(tmp_path, slots)
    first, second, waiting = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await audit.on_chat_model_start({}, [], run_id=first)
    await audit.on_chat_model_start({}, [], run_id=second)
    task = asyncio.create_task(audit.on_chat_model_start({}, [], run_id=waiting))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slots.active == 2
    await audit.on_llm_error(asyncio.CancelledError(), run_id=first)
    await audit.on_llm_error(asyncio.CancelledError(), run_id=second)
    assert slots.active == 0
    assert native.collect_model_audit(tmp_path)["model_calls"] == 2


async def test_native_app_uses_real_gateway_configuration_without_network_or_cli(tmp_path):
    adapters = build_fixture_adapters(Settings.for_tests(tmp_path))
    adapter = next(iter(adapters.values()))
    settings = replace(Settings.for_tests(tmp_path), libraries=(adapter.library,))
    audit = native.NativeModelAudit(tmp_path / "calls", native.NativeModelSlots())
    gateway = native.NativeLunaGateway(config(), audit)
    callback = BenchmarkUsageCallback()
    app, reasons, models = native.build_product_app(settings, adapter, gateway, callback)
    assert app.state.agent_runner.model_name == "gpt-6-luna"
    assert app.state.search_service.reason_provider == "openai"
    assert reasons._cache is app.state.store
    assert app.state.citation_texts._gateway is gateway
    for model in (gateway.agent_model(), reasons._model, models.source_model, models.ranking_model):
        assert model.use_responses_api is True
        assert model.store is False
        assert model.callbacks == [audit]
    await app.state.citation_texts.aclose()
    app.state.manuscripts.close()
    await gateway.aclose()
    if gateway.agent_model().http_async_client is None:
        assert not gateway.agent_model().root_async_client.is_closed()


async def test_shared_case_hooks_drive_the_real_asgi_path_without_cli_or_gold(
    tmp_path, monkeypatch
):
    import httpx

    from citation_tool.api import create_app
    from test_agent import ScriptedChatModel, StubReasonGenerator

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    settings = Settings.for_tests(runtime)
    adapters = build_fixture_adapters(settings)
    adapter = next(iter(adapters.values()))
    settings = replace(settings, libraries=(adapter.library,))
    monkeypatch.setattr(shared, "product_settings", lambda *args: settings)
    monkeypatch.setattr(shared, "build_product_adapter", lambda *args: adapter)
    monkeypatch.setattr(
        shared, "build_kimi_gateway", lambda **kwargs: pytest.fail("CLI must not run")
    )
    slots = native.NativeModelSlots()
    audit = native.NativeModelAudit(runtime / "model-calls", slots)
    gateway = object()

    def builder(actual_settings, actual_adapter, actual_gateway, callback):
        assert actual_gateway is gateway
        audit.usage_callback = callback
        model = ScriptedChatModel(
            responses=[
                AIMessage(
                    content='{"papers":[],"conclusion":"No retrieval requested."}',
                    response_metadata={"model_name": "gpt-6-luna", "id": "resp_asgi"},
                    usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                )
            ]
        )
        model.callbacks = [audit]
        app = create_app(
            actual_settings,
            adapters={actual_adapter.library.library_id: actual_adapter},
            reason_generator=StubReasonGenerator(),
            agent_model=model,
        )
        return app, None, None

    task = {
        "case_id": "native-plumbing",
        "run_case_id": "native-plumbing",
        "repetition": 1,
        "slice": "support",
        "family": "plumbing",
        "dense_threshold": 0.8,
        "request": {"input_type": "keywords", "keywords": ["public test"]},
        "baseline_query": "Do not search; return an empty JSON papers list.",
        "expect": {"secret_gold": "DO_NOT_SEND"},
    }
    record = await shared.run_case(
        task,
        root=tmp_path,
        runtime=runtime,
        corpus={},
        index=None,
        implementation_hash="fixture",
        shared_async=asyncio.Semaphore(2),
        shared_sync=threading.BoundedSemaphore(2),
        gateway_override=gateway,
        app_builder=builder,
        audit_collector=native.collect_model_audit,
    )
    assert record["completed"] is True
    assert record["model_valid"] is True
    assert record["agent_run_id"]
    assert record["queries"] == []
    request = json.loads((runtime / "request.json").read_text(encoding="utf-8"))
    assert "expect" not in request["request"]
    assert "DO_NOT_SEND" not in json.dumps(request)
    actual = native._native_record(
        record, native.collect_model_audit(runtime / "model-calls"), runtime
    )
    assert actual["transport"] == native.TRANSPORT
    assert actual["kimi_session_ids"] == []
    assert actual["session_isolation_valid"] is True
    assert actual["native_response_ids"] == ["resp_asgi"]
    assert actual["model_requested"] == "gpt-6-luna"
    assert httpx.__version__


def test_native_contract_freezes_transport_and_keeps_original_data_and_limits(
    tmp_path, monkeypatch
):
    suite, corpus = load_data()
    existing, _, _ = native.prepare_run("scientific-gap-luna-dev-20261117-v1")
    root = tmp_path / "contract"
    root.mkdir()
    (root / "plan.json").write_bytes((existing / "plan.json").read_bytes())
    monkeypatch.setattr(
        native, "authorized_config", lambda: pytest.fail("prepare must not read credentials")
    )
    contract = native.product_contract(root, suite, corpus)
    assert contract["tasks"] == 28 and contract["corpus_records"] == 5183
    assert contract["transport"] == native.TRANSPORT
    assert contract["expected_model"] == "gpt-6-luna"
    assert contract["limits"] == {
        "timeout_seconds": 900,
        "steps": 96,
        "parent_tools": 32,
        "max_parallel_subjects": 1,
        "max_process_model_calls": 2,
    }
    assert contract["provider_storage_requested"] is False
    assert contract["input_mapping"] == shared.REQUEST_MAPPING_VERSION
    assert contract["implementation_hash"] != shared.product_implementation_hash(root, corpus)
    shared.ensure_product_contract(root, contract)
    shared.ensure_product_contract(root, contract)
    with pytest.raises(ValueError, match="contract changed"):
        shared.ensure_product_contract(root, {**contract, "transport": "changed"})


async def test_native_resume_preserves_failed_records_without_reading_credentials_again(
    tmp_path, monkeypatch
):
    task = {"case_id": "sf-native-fixture"}
    monkeypatch.setattr(native, "prepare_run", lambda name: (tmp_path, {"tasks": [task]}, {}))
    monkeypatch.setattr(
        native, "product_contract", lambda *args: {"implementation_hash": "native-fixture"}
    )
    monkeypatch.setattr(native, "product_implementation_hash", lambda *args: "native-fixture")
    monkeypatch.setattr(shared, "product_settings", lambda *args: None)
    monkeypatch.setattr(shared, "require_prebuilt_index", lambda *args: None)
    monkeypatch.setattr(native, "authorized_config", config)

    async def failed_case(*args, **kwargs):
        return {
            "schema": shared.SCHEMA,
            "system": "product",
            "case_id": task["case_id"],
            "implementation_hash": "native-fixture",
            "transport": native.TRANSPORT,
            "completed": False,
            "model_valid": False,
            "model_request_count": 0,
            "reason_metrics": {"generated": 0, "total": 0},
            "ranking_metrics": {"completed": 0, "total": 0},
            "error": {"code": "NATIVE_FIXTURE_FAILURE"},
        }

    monkeypatch.setattr(native, "run_case", failed_case)
    summary = await native.run_products("fixture", resume=False)
    assert summary["cases"] == 1 and summary["completed"] == 0
    path = tmp_path / "product" / f"{task['case_id']}.json"
    before = path.read_bytes()
    monkeypatch.setattr(native, "authorized_config", lambda: pytest.fail("no new model calls"))
    summary = await native.run_products("fixture", resume=True)
    assert summary["cases"] == 1 and summary["completed"] == 0
    assert path.read_bytes() == before


async def test_native_run_rejects_parallel_cases_before_loading_data(monkeypatch):
    monkeypatch.setattr(
        native, "prepare_run", lambda name: pytest.fail("must reject before preparation")
    )
    with pytest.raises(ValueError, match="one isolated"):
        await native.run_products("unused", resume=False, concurrency=2)
