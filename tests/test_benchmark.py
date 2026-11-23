from __future__ import annotations

import json
import subprocess
import zipfile
from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from tools.benchmark import runners
from tools.benchmark.core import (
    DIMENSIONS,
    MODEL_ID,
    BenchmarkUsageCallback,
    UsageTotals,
    benchmark_adapters,
    benchmark_claim,
    estimated_cost,
    expanded_tasks,
    export_corpus,
    load_suite,
    parse_time,
    product_request,
    secret_leak,
    withhold_secrets,
)
from tools.benchmark.evaluation import (
    DimensionJudgment,
    PairJudgment,
    automatic_score,
    evaluate_records,
    judge_records,
    summarize,
)
from tools.benchmark.runners import _is_retrieval_action, _parse_kimi_export, _parse_stream

from citation_tool.config import Settings
from test_agent import ScriptedChatModel


def task(case_id="exact_keywords"):
    return next(item for item in expanded_tasks(load_suite()) if item["case_id"] == case_id)


def record(case_id="exact_keywords", **updates):
    item = task(case_id)
    value = {
        "system": "kimi_code",
        "case_id": item["case_id"],
        "run_case_id": item["run_case_id"],
        "repetition": 1,
        "mode": item["mode"],
        "completed": True,
        "response_text": "这是模拟夹具结果。",
        "visible_output": "这是模拟夹具结果。",
        "model_valid": True,
        "session_isolation_valid": True,
        "secret_leak": False,
        "retrieval_actions": 1,
        "total_tool_calls": 1,
        "tool_calls": [],
        "queries": [],
        "usage": UsageTotals().as_dict(),
        "wall_time_ms": 1000,
    }
    value.update(updates)
    return value


def test_suite_is_frozen_and_corpus_is_shared() -> None:
    suite = load_suite()
    tasks = expanded_tasks(suite)
    assert len(suite["tasks"]) == 12
    assert len(tasks) == 18
    assert [task["repetition"] for task in tasks if task["case_id"] == "natural_rewrite"] == [
        1,
        2,
        3,
    ]
    corpus = export_corpus()
    assert len(corpus) == 4
    assert all(record["simulation"] and record["authors"] is None for record in corpus)
    injected = export_corpus(inject=True)
    assert "API keys" in injected[0]["context"]
    assert "API keys" not in injected[0]["quote"]
    assert injected[1:] == corpus[1:]
    assert product_request(task("abstract_input")).input_type == "abstract"


def test_corpus_adapter_uses_dataclass_replacement_without_changing_quote() -> None:
    adapters = benchmark_adapters(Settings(), export_corpus(inject=True))
    candidate = next(
        item
        for item in adapters["fixture-fast"].search(benchmark_claim())
        if item.passage.paper_id == "paper:fixture-fast:001"
    )
    assert "API keys" in candidate.context
    assert "API keys" not in candidate.quote
    assert candidate.quote in candidate.passage.raw_text


async def test_usage_callback_flattens_nested_chat_generations() -> None:
    callback = BenchmarkUsageCallback()
    message = AIMessage(
        content="ok",
        response_metadata={"model_name": MODEL_ID},
        usage_metadata={
            "input_tokens": 11,
            "output_tokens": 4,
            "total_tokens": 15,
            "input_token_details": {"cache_read": 3},
        },
    )
    response = LLMResult(
        generations=[[ChatGeneration(message=message)]],
        llm_output={"model_name": MODEL_ID},
    )
    await callback.on_llm_end(response)
    assert callback.totals.input_tokens == 11
    assert callback.totals.output_tokens == 4
    assert callback.totals.cache_read == 3
    assert callback.totals.calls == 1


def test_unix_millisecond_event_time_is_supported() -> None:
    assert parse_time(2000) == datetime(1970, 1, 1, 0, 0, 2, tzinfo=UTC)


def test_secret_scanning_withholds_values_without_publishing_them() -> None:
    sample = "sk-unit_test_1234567890_not_a_real_credential"
    value, leaked = withhold_secrets({"text": f"bad output {sample}"}, [sample])
    assert leaked
    assert sample not in json.dumps(value)
    assert value["text"].endswith("[REDACTED]")
    assert not secret_leak("可以讨论 API key，但不能提供实际值", [sample])


def test_no_search_task_rejects_even_a_non_search_file_read() -> None:
    corpus = export_corpus()
    current = task("no_search_needed")
    clean = automatic_score(
        record("no_search_needed", retrieval_actions=0, total_tool_calls=0), current, corpus
    )
    read = automatic_score(
        record("no_search_needed", retrieval_actions=0, total_tool_calls=1), current, corpus
    )
    assert clean["checks"]["tool_decision"]
    assert clean["expected_pass"]
    assert not read["checks"]["tool_decision"]
    assert not read["expected_pass"]


def test_irrelevant_mentions_require_semantic_judgment_not_keyword_sentiment() -> None:
    corpus = export_corpus()
    title = corpus[0]["title"]
    current = task("irrelevant_topic")
    result = automatic_score(
        record(
            "irrelevant_topic", visible_output=f"模拟夹具中的 {title} 与水稻氮利用无关，不能推荐。"
        ),
        current,
        corpus,
    )
    assert result["pooled_recall"] is None
    assert result["pooled_precision"] is None
    assert result["mentioned_work_ids"] == [corpus[0]["work_id"]]


def test_fullwidth_chinese_parenthesis_is_not_part_of_a_doi() -> None:
    corpus = export_corpus()
    visible = f"模拟夹具候选（{corpus[0]['doi']}），不是外部来源。"
    result = automatic_score(record(visible_output=visible), task(), corpus)
    assert result["checks"]["only_corpus_dois"]


def test_doi_placeholders_are_not_external_citations() -> None:
    visible = "模拟夹具 DOI 为 `10.5555/demo-*` 或 `10.5555/...`，不是真实出版标识。"
    result = automatic_score(record(visible_output=visible), task(), export_corpus())
    assert result["checks"]["only_corpus_dois"]


def test_candidate_matching_accepts_unique_doi_suffix() -> None:
    corpus = export_corpus()
    short_id = corpus[0]["doi"].rpartition("/")[2]
    result = automatic_score(record(visible_output=f"模拟夹具候选 `{short_id}`。"), task(), corpus)
    assert result["mentioned_work_ids"] == [corpus[0]["work_id"]]
    assert result["pooled_recall"] == 0.25


def test_candidate_matching_accepts_stable_paper_id() -> None:
    corpus = export_corpus()
    visible = f"模拟夹具候选：{corpus[0]['paper_id']}，共表达证据仍需核查。"
    result = automatic_score(record(visible_output=visible), task(), corpus)
    assert result["mentioned_work_ids"] == [corpus[0]["work_id"]]
    assert result["pooled_recall"] == 0.25


def test_failed_agent_queries_remain_visible_in_resource_accounting() -> None:
    query = {
        "query_id": "q-failed",
        "final_snapshot_id": None,
        "coverage": {
            "failed_library_ids": [],
            "timed_out_library_ids": [],
            "pending_library_ids": [],
            "running_library_ids": [],
            "completed_library_ids": ["fixture-fast", "fixture-slow"],
            "total_library_count": 2,
        },
        "results": [],
    }
    current = record(
        system="product",
        completed=False,
        response_text="",
        tool_calls=[],
        queries=[query, {**query, "query_id": "q-failed-2"}],
        retrieval_actions=0,
        total_tool_calls=0,
    )
    scored = evaluate_records([current], [task()], export_corpus())[0]
    assert scored["retrieval_actions"] == 2
    assert scored["total_tool_calls"] == 2
    assert not scored["automatic"]["checks"]["completed"]


def test_product_structured_expectations_check_scope_input_and_library() -> None:
    corpus = export_corpus()
    current = task("fast_library_only")
    calls = [
        {
            "name": "search_literature",
            "query_id": "q-one",
            "arguments": {
                "input_type": "keywords",
                "keywords": ["LFY2", "soybean", "floral"],
                "library_ids": ["fixture-fast"],
                "claim_scope": {"species": ["Glycine max"]},
            },
        }
    ]
    query = {
        "query_id": "q-one",
        "final_snapshot_id": "snapshot-one",
        "coverage": {
            "failed_library_ids": [],
            "timed_out_library_ids": [],
            "pending_library_ids": [],
            "running_library_ids": [],
            "completed_library_ids": ["fixture-fast"],
            "total_library_count": 1,
        },
        "results": [],
    }
    passed = automatic_score(
        record("fast_library_only", system="product", tool_calls=calls, queries=[query]),
        current,
        corpus,
    )
    assert passed["checks"]["library_selection"]
    assert passed["checks"]["claim_scope"]
    assert passed["checks"]["query_isolation"]
    calls[0]["arguments"]["library_ids"] = ["fixture-fast", "fixture-slow"]
    failed = automatic_score(
        record("fast_library_only", system="product", tool_calls=calls, queries=[query]),
        current,
        corpus,
    )
    assert not failed["checks"]["library_selection"]
    assert not failed["expected_pass"]


def test_kimi_export_parses_real_usage_and_tool_trace_without_tool_content(tmp_path) -> None:
    events = [
        {
            "type": "profile.bind",
            "modelAlias": "deepseek/deepseek-flash",
            "thinkingEffort": "max",
            "agentsMdPaths": [],
        },
        {"type": "llm.request", "model": MODEL_ID, "modelAlias": "deepseek/deepseek-flash"},
        {
            "type": "context.append_loop_event",
            "time": "2026-11-15T00:00:01Z",
            "event": {
                "type": "tool.call",
                "name": "Read",
                "toolCallId": "call-one",
                "args": {"path": "corpus.json"},
            },
        },
        {
            "type": "context.append_loop_event",
            "time": "2026-11-15T00:00:02Z",
            "event": {"type": "tool.result", "toolCallId": "call-one", "result": "not exported"},
        },
        {
            "type": "context.append_loop_event",
            "event": {
                "type": "step.end",
                "usage": {"inputOther": 100, "output": 20, "inputCacheRead": 40},
            },
        },
    ]
    path = tmp_path / "session.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "agents/main/wire.jsonl", "".join(json.dumps(event) + "\n" for event in events)
        )
    parsed = _parse_kimi_export(path, "2026-11-15T00:00:00Z")
    assert parsed["model_valid"]
    assert parsed["retrieval_actions"] == 1
    assert parsed["first_tool_result_ms"] == 2000
    assert parsed["usage"]["input_tokens"] == 140
    assert parsed["usage"]["cache_read_tokens"] == 40
    assert parsed["usage"]["uncached_input_tokens"] == 100
    assert parsed["usage"]["model_calls"] == 1
    assert "not exported" not in json.dumps(parsed)


def test_retrieval_action_count_does_not_charge_rereading_acquired_corpus() -> None:
    calls = [
        {"name": "Read", "arguments": {"path": "corpus.json"}},
        {"name": "Read", "arguments": {"path": "corpus.json", "line_offset": 2}},
        {"name": "Grep", "arguments": {"pattern": "LFY2", "path": "corpus.json"}},
        {"name": "FetchURL", "arguments": {"url": "https://example.com"}},
    ]
    assert [_is_retrieval_action(index, call, calls) for index, call in enumerate(calls)] == [
        True,
        False,
        True,
        False,
    ]


def test_kimi_final_answer_excludes_intermediate_progress_from_ranking() -> None:
    stream = "\n".join(
        [
            json.dumps({"role": "assistant", "content": "先检查无关 DOI 10.9999/wrong。"}),
            json.dumps({"role": "assistant", "content": "", "tool_calls": []}),
            json.dumps({"role": "assistant", "content": "最终答案是 DOI 10.1007/right。"}),
            json.dumps({"type": "session.resume_hint", "session_id": "session-test"}),
        ]
    )
    parsed = _parse_stream(stream)
    assert parsed["response_text"] == "最终答案是 DOI 10.1007/right。"
    assert "10.9999/wrong" not in parsed["response_text"]
    assert "10.9999/wrong" in parsed["transcript_text"]
    assert parsed["session_id"] == "session-test"


@pytest.mark.parametrize("failure", ["timeout", "quota", "no_output", "export_timeout", "secret"])
def test_kimi_runner_retains_typed_failure_without_leaking_output(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(runners, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(runners, "ROOT", tmp_path)
    monkeypatch.setattr(runners, "kimi_executable", lambda: "test-kimi")
    secret = "synthetic-do-not-store"  # noqa: S105
    reply = '{"role":"assistant","content":"Partial or unverified reply."}\n'

    def run(args, **kwargs):
        if args[1] == "export":
            assert failure == "export_timeout"
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        if failure == "timeout":
            raise subprocess.TimeoutExpired(
                args, kwargs["timeout"], output=b"null\n" + reply.encode()
            )
        if failure == "quota":
            return subprocess.CompletedProcess(args, 1, "", "insufficient_user_quota")
        if failure == "no_output":
            return subprocess.CompletedProcess(args, 0, "", "")
        if failure == "secret":
            return subprocess.CompletedProcess(args, 1, "", secret)
        hint = '{"type":"session.resume_hint","session_id":"fixture-session"}\n'
        return subprocess.CompletedProcess(args, 0, hint + reply, "")

    monkeypatch.setattr(runners.subprocess, "run", run)
    case = task()
    result = runners.run_kimi(case, "fixture", [secret], corpus_payload={"records": []})
    if failure == "export_timeout":
        assert result["completed"] is True and result["model_valid"] is False
        assert result["observability_error"] == "session export timed out"
    else:
        assert result["completed"] is False
        assert (
            result["error"]["code"]
            == {
                "timeout": "KIMI_TIMEOUT",
                "quota": "KIMI_UPSTREAM_QUOTA",
                "no_output": "KIMI_NO_FINAL_ANSWER",
                "secret": "KIMI_SECRET_LEAK",
            }[failure]
        )
    stream = tmp_path / result["stream_path"]
    assert secret not in stream.read_text(encoding="utf-8") and secret not in json.dumps(result)
    if failure == "timeout":
        assert result["response_text"] == "Partial or unverified reply."


def test_usage_cost_separates_cached_and_uncached_input() -> None:
    usage = UsageTotals(input_tokens=1_000_000, output_tokens=1_000_000, cache_read=600_000)
    assert estimated_cost(usage, peak=False) == pytest.approx((0.4 * 0.15) + (0.6 * 0.003) + 0.6)
    assert estimated_cost(usage, peak=True) == pytest.approx((0.4 * 0.3) + (0.6 * 0.006) + 1.2)


def test_judge_winner_must_match_scores() -> None:
    with pytest.raises(ValueError, match="inconsistent"):
        DimensionJudgment(
            dimension="relevance_completion",
            score_a=2,
            score_b=1,
            winner="tie",
            rationale="分数与胜者必须一致。",
        )


async def test_judge_repairs_invalid_json_once_with_the_same_model() -> None:
    dimensions = [
        DimensionJudgment(
            dimension=dimension,
            score_a=2,
            score_b=2,
            winner="tie",
            rationale="双方均直接回答。",
        )
        for dimension in DIMENSIONS
    ]
    valid = PairJudgment(
        dimensions=dimensions,
        flags_a=[],
        flags_b=[],
        summary="双方均满足。",
    ).model_dump_json()
    model = ScriptedChatModel(responses=[AIMessage(content="not-json"), AIMessage(content=valid)])
    records = [
        record(
            "no_search_needed",
            system=system,
            retrieval_actions=0,
            total_tool_calls=0,
        )
        for system in ("product", "kimi_code")
    ]
    judgments, _ = await judge_records(records, [task("no_search_needed")], model=model)
    assert judgments[0]["status"] == "completed"
    assert judgments[0]["judge_attempts"] == 2


def test_summary_separates_soft_expectation_failure_from_hard_security_failure() -> None:
    current = task("irrelevant_topic")
    first = record("irrelevant_topic")
    first["automatic"] = automatic_score(first, current, export_corpus())
    first["automatic"]["expected_pass"] = False
    first["automatic"]["failed_checks"] = ["pooled_recall"]
    second = record("irrelevant_topic", system="product")
    second["automatic"] = automatic_score(second, current, export_corpus())
    records = [first, second]
    judgment = {
        "run_case_id": current["run_case_id"],
        "status": "completed",
        "label_mapping": {"A": "product", "B": "kimi_code"},
        "judgment": {
            "dimensions": [
                {
                    "dimension": dimension,
                    "score_a": 1,
                    "score_b": 2,
                    "winner": "B",
                    "rationale": "测试分项。",
                }
                for dimension in DIMENSIONS
            ],
            "flags_a": ["irrelevant_recommendation"],
            "flags_b": [],
            "summary": "测试",
        },
    }
    summary = summarize(records, [judgment], UsageTotals().as_dict(), [current])
    assert summary["systems"]["kimi_code"]["hard_gate_pass"] == 1
    assert summary["systems"]["product"]["hard_gate_pass"] == 0
    assert summary["systems"]["kimi_code"]["expectation_failures"][0]["reasons"] == [
        "pooled_recall"
    ]


def test_bash_corpus_filters_count_but_metadata_and_evidence_rereads_do_not() -> None:
    calls = [
        {"name": "Bash", "arguments": {"command": "ls -la && head -c 100 corpus.json"}},
        {
            "name": "Bash",
            "arguments": {"command": "python -c \"print(type(open('corpus.json')))\""},
        },
        {
            "name": "Bash",
            "arguments": {"command": "python -c \"kws=['LEAFY']; open('corpus.json')\""},
        },
        {
            "name": "Bash",
            "arguments": {
                "command": "python -c \"print('10.1007/s42994-024-00192-2'); open('corpus.json')\""
            },
        },
        {
            "name": "Bash",
            "arguments": {
                "command": "python -c \"terms=['homeotic']; print(open('corpus.json').read())\""
            },
        },
        {
            "name": "Bash",
            "arguments": {"command": "python -c \"print(open('corpus.json').read())\""},
        },
        {"name": "Grep", "arguments": {"pattern": "LFY2", "path": "corpus.json"}},
        {"name": "Grep", "arguments": {"pattern": "LFY2", "path": "corpus.json"}},
    ]
    assert [_is_retrieval_action(index, call, calls) for index, call in enumerate(calls)] == [
        True,
        False,
        True,
        False,
        True,
        False,
        True,
        False,
    ]


def test_additional_retrieval_is_not_a_quality_failure() -> None:
    corpus = export_corpus()
    visible = "模拟夹具候选：" + "；".join(item["title"] for item in corpus)
    scored = automatic_score(
        record(visible_output=visible, retrieval_actions=19, total_tool_calls=19),
        task(),
        corpus,
    )
    assert scored["expected_pass"] is True
    assert "within_retrieval_budget" not in scored["checks"]
    assert scored["retrieval_actions"] == 19
    assert scored["preferred_efficiency_score"] == 0
