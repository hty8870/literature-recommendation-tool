import json
import zipfile

from langchain_core.messages import HumanMessage, ToolMessage
from tools.gap_benchmark.opencode_ablation import observation, source_material
from tools.gap_benchmark.opencode_assessment import (
    extra_fields_only,
    family_outcomes,
    traced_targets,
)
from tools.gap_benchmark.opencode_baseline import evidence_trace
from tools.gap_benchmark.opencode_native import (
    anthropic_messages,
    chat_messages,
    reply,
    visible_parent_context,
)


def test_chat_transport_replays_opaque_continuation_without_exposing_it():
    msg, private_parts = reply(
        {
            "model": "demo",
            "id": "res-1",
            "choices": [
                {
                    "message": {
                        "content": None,
                        "reasoning_content": "opaque-state",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {"name": "search", "arguments": '{"q":"test"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        },
        "openai",
    )
    history = [HumanMessage("public question"), msg, ToolMessage("{}", tool_call_id="call-1")]
    assert private_parts == 1
    assert msg.text == ""
    assert msg.tool_calls[0]["args"] == {"q": "test"}
    assert chat_messages(history)[1]["reasoning_content"] == "opaque-state"
    assert "opaque-state" not in json.dumps(visible_parent_context(history, "unused"))


def test_messages_transport_returns_only_public_text_and_native_tool_calls():
    blocks = [
        {"type": "thinking", "thinking": "opaque-state", "signature": "signature"},
        {"type": "text", "text": "visible"},
        {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {"q": "a"}},
    ]
    msg, n = reply({"model": "demo", "id": "msg_1", "content": blocks}, "anthropic")
    history = [HumanMessage("question"), msg, ToolMessage("result", tool_call_id="toolu_1")]
    rendered = anthropic_messages(history)[1]
    assert n == 1 and msg.text == "visible"
    assert rendered[1]["content"] == blocks
    assert rendered[2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": "result",
            }
        ],
    }


def test_legacy_projection_changes_only_one_field_at_a_time():
    gold_never_used = {
        "work_id": "work:scifact:42",
        "title": "T",
        "evidence_excerpt": "Text.",
        "base_rank": 5,
        "llm_rank": 2,
        "relationship": "insufficient",
        "scope_match": "unknown",
        "rejection_reasons": ["abstract_metadata_only"],
        "reason": {"status": "generated", "text": "R"},
    }
    queries = [{"query_id": "q_1", "candidate_diagnostics": [gold_never_used]}]
    for arm in ("without_placeholder", "without_rationale", "raw_abstracts", "raw_without_skill"):
        assert source_material(observation(queries, arm)) == source_material(queries)
    no_label = observation(queries, "without_placeholder")[0]["candidate_diagnostics"][0]
    assert no_label == {k: v for k, v in gold_never_used.items() if k != "relationship"}
    no_reason = observation(queries, "without_rationale")[0]["candidate_diagnostics"][0]
    assert no_reason == {**gold_never_used, "reason": {"status": "generated"}}


def test_format_sensitivity_cannot_change_scientific_content():
    article = {"doc_id": "42", "stance": "CONTRADICT", "quote": "Verbatim.", "reason": "Scope."}
    answer = {"papers": [article], "conclusion": "Direction."}
    response = {**answer, "papers": [{**article, "quote_note": "extra"}]}
    repaired = extra_fields_only(json.dumps(response))
    assert repaired is not None
    assert json.loads(repaired[0]) == answer
    assert repaired[1] == [["papers", 0, "quote_note"]]
    response["papers"][0]["stance"] = "invalid"
    assert extra_fields_only(json.dumps(response)) is None
    assert extra_fields_only('{"papers":') is None


def test_loss_weights_case_not_document_and_uses_only_public_tool_outputs(tmp_path):
    rows = [
        {"case_id": "a", "outcome": "covered"},
        {"case_id": "a", "outcome": "omitted"},
        {"case_id": "b", "outcome": "covered"},
    ]
    assert family_outcomes(rows)["weighted_shares"]["covered"] == 0.75
    archive = tmp_path / "export.zip"
    events = [
        {"type": "context.append_loop_event", "event": {"type": "content.part", "text": "private"}},
        {
            "type": "context.append_loop_event",
            "event": {
                "type": "tool.result",
                "toolCallId": "seen",
                "result": {"output": "doc 1234, 1378"},
            },
        },
    ]
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("agents/main/wire.jsonl", "\n".join(json.dumps(o) for o in events))
    traces = evidence_trace(archive, {"1234", "1378", "1500"})
    assert len(traces) == 1 and traces[0]["corpus_ids_mentioned"] == ["1234", "1378"]
    assert "private" not in json.dumps(traces)


def test_timed_out_trace_uses_actual_last_turn_and_retains_earlier_receipts(tmp_path):
    source = {
        "title": "Study",
        "abstract": "Original evidence.",
        "abstract_sentences": ["Original evidence."],
    }
    receipt = {
        "query_id": "q_one",
        "candidate_diagnostics": [
            {"work_id": "work:scifact:1234", "evidence_excerpt": source["abstract"]}
        ],
    }
    trace = [
        {"visible_output": "", "tool_calls": [{"name": "search_literature"}], "tool_results": []},
        {
            "visible_output": "Continuing research.",
            "tool_calls": [{"name": "search_literature"}],
            "tool_results": [{"content": receipt}],
        },
    ]
    path = tmp_path / "trace.json"
    path.write_text(json.dumps(trace), encoding="utf-8")
    record = {
        "system": "product",
        "response_text": None,
        "parent_trace_path": path.name,
        "assessment": {"valid_completion": False, "format_valid": False, "papers": []},
    }
    task = {
        "case_id": "sf-12",
        "slice": "support",
        "claim": "Claim",
        "expect": {"evidence": {"1234": [{"label": "SUPPORT", "sentences": [0]}]}},
    }
    one = traced_targets(tmp_path, record, task, {"1234": source})[0]
    assert one["outcome"] == "invalid_run"
    assert one["parent_observed_count_last_turn"] == 1
    assert one["parent_receipts"] == one["parent_receipts_ever"]
    assert one["parent_receipts"][0]["abstract_exact_and_complete"]
    trace.extend([trace[1], {**trace[1], "tool_results": []}])
    path.write_text(json.dumps(trace), encoding="utf-8")
    two = traced_targets(tmp_path, record, task, {"1234": source})[0]
    assert two["parent_observed_count_last_turn"] == 0 and not two["parent_receipts"]
    assert two["parent_receipts_ever"] == one["parent_receipts_ever"]
