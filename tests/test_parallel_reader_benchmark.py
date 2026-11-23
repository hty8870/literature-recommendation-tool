from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from openai import InternalServerError
from tools.parallel_luna import ApiAdmissionStopped, ObservedLunaClient, ParallelLunaGateway
from tools.qasper_reader_pair import CaseAudit, _neighbor_diagnostic, paired
from tools.qasper_scoring import score
from tools.scifact_reader_pair import LibraryAudit

from citation_tool.reasons import ModelConfig

URL = "https://api.openai.com/v1/responses"


async def test_shared_api_window_expands_only_after_successful_parallel_requests():
    entered = 0
    arrival_goal = 4
    ready = asyncio.Event()

    async def server(request):
        nonlocal entered
        entered += 1
        if entered >= arrival_goal:
            ready.set()
        await asyncio.wait_for(ready.wait(), timeout=2)
        return httpx.Response(200, json={"ok": True})

    async with ObservedLunaClient(8, transport=httpx.MockTransport(server)) as client:
        assert client.limit == 4
        await client.expand_after_pilot()
        assert client.limit == 4
        await asyncio.gather(*(client.post(URL) for _ in range(4)))
        assert client.peak == 4
        await client.expand_after_pilot()
        assert client.limit == 8
        ready.clear()
        arrival_goal = 12
        await asyncio.gather(*(client.post(URL) for _ in range(8)))
        assert entered == 12 and client.peak == 8
        assert client.active == 0 and len(client.rows) == 12


async def test_rate_limit_is_not_retried_and_auth_failure_stops_admission():
    replies = iter((429, 403))
    attempted = []

    async def server(request):
        status = next(replies)
        attempted.append(status)
        return httpx.Response(status, headers={"Retry-After": "0"}, json={"error": {}})

    async with ObservedLunaClient(8, transport=httpx.MockTransport(server)) as client:
        with pytest.raises(ValueError, match="official Responses"):
            await client.post("https://other.example/v1/responses")
        assert (await client.post(URL)).status_code == 429
        assert client.limit == 2
        assert attempted == [429]
        assert (await client.post(URL)).status_code == 403
        with pytest.raises(ApiAdmissionStopped, match="HTTP_403"):
            await client.post(URL)
        assert attempted == [429, 403]


async def test_gateway_uses_native_responses_without_replaying_a_failed_post(tmp_path):
    audit = CaseAudit(tmp_path, ["P0001 [Body] frozen sentence"])
    seen = []

    async def server(request):
        sent = json.loads(request.content)
        seen.append((sent["model"], sent["store"]))
        return httpx.Response(503, json={"error": {"message": "temporary"}})

    config = ModelConfig(
        api_key="benchmark-test",
        model="gpt-6-luna",
        provider="openai",  # noqa: S106
        api_type="responses",
        base_url="https://api.openai.com/v1",
    )
    async with ObservedLunaClient(4, transport=httpx.MockTransport(server)) as client:
        gateway = ParallelLunaGateway(config, client, audit)
        with pytest.raises(InternalServerError, match="temporary"):
            await gateway.agent_model().ainvoke([HumanMessage(content="test")])
        assert seen == [("gpt-6-luna", False)]
        assert client.rows[0]["status"] == 503
        assert client.active == 0


async def test_qasper_audit_counts_only_text_actually_disclosed_at_frozen_offsets(tmp_path):
    content = "P0001 [Body] The true result follows. The unseen tail is different."
    audit = CaseAudit(tmp_path, [content])
    body_start = content.index("The true")
    stop = body_start + len("The true result follows.")
    receipt = ToolMessage(
        name="read_manuscript",
        tool_call_id="tool-one",
        content=json.dumps(
            {
                "view": "find",
                "matches": [
                    {
                        "passage_index": 0,
                        "start": body_start,
                        "text": content[body_start:stop],
                    }
                ],
            }
        ),
    )
    run = uuid.uuid4()
    await audit.on_chat_model_start({}, [[receipt, receipt]], run_id=run)
    await audit.on_llm_error(RuntimeError("mock"), run_id=run)
    assert audit.bad_spans == 0
    assert len(audit.observations) == 1
    assert audit.evidence(
        json.dumps(
            {
                "status": "answered",
                "answer": "A result.",
                "evidence": [
                    {"passage_id": "P0001", "quote": "The true result follows."},
                    {"passage_id": "P0001", "quote": "The unseen tail is different."},
                ],
            }
        )
    ) == [True, False]


def test_qasper_neighbor_attribution_distinguishes_disclosure_and_final_citation():
    passages = {"P0001": "Outcome was higher.", "P0002": "Only controls were matched."}
    refs = [{"paragraph_ids": list(passages), "answer": "Higher for matched controls."}]
    answer = json.dumps(
        {
            "status": "answered",
            "answer": "Higher for matched controls.",
            "evidence": [{"passage_id": "P0002", "quote": passages["P0002"]}],
        }
    )
    row = {
        "answer": answer,
        "receipt_windows": {
            "primary": [("P0001", 0, 7), ("P0001", 7, len(passages["P0001"]))],
            "neighbors": [("P0002", 0, len(passages["P0002"]))],
        },
    }
    graded = score(answer, stratum="extractive", passages=passages, references=refs)
    observed = _neighbor_diagnostic(row, passages, refs, graded)
    assert observed["reference_group_fully_delivered"]
    assert not observed["reference_group_fully_delivered_by_primary"]
    assert observed["annotated_full_from_neighbors_only"] == ["P0002"]
    assert observed["annotated_neighbor_only_cites"] == ["P0002"]
    assert not graded["complete_reference_paragraph_set"]


async def test_scifact_does_not_count_discovery_as_reading(tmp_path):
    audit = LibraryAudit(tmp_path)
    msgs = [
        ToolMessage(
            name="read_literature",
            tool_call_id="a",
            content=json.dumps(
                {
                    "action": "discover",
                    "leads": [{"work_id": "work:scifact:23456"}],
                }
            ),
        ),
        ToolMessage(
            name="read_literature",
            tool_call_id="b",
            content=json.dumps(
                {
                    "action": "open",
                    "source": {"work_id": "work:scifact:12345"},
                    "text_ref": "txt_1",
                    "passages": [{"text": "The first actual paragraph."}],
                }
            ),
        ),
        ToolMessage(
            name="read_literature",
            tool_call_id="c",
            content=json.dumps(
                {
                    "action": "read",
                    "text_ref": "txt_1",
                    "passages": [{"text": "A later paragraph."}],
                }
            ),
        ),
    ]
    run = uuid.uuid4()
    await audit.on_chat_model_start({}, [msgs], run_id=run)
    await audit.on_llm_error(RuntimeError("mock"), run_id=run)
    assert audit.reader_docs == {"12345"}
    assert audit.delivered == {"12345": {"The first actual paragraph.", "A later paragraph."}}


def test_paired_changes_keep_zero_scores_and_distinguish_missing_pairs():
    rows = [
        {"case": a, "arm": b, "q": x}
        for a, b, x in (
            ("a", "off", 0),
            ("a", "on", 1),
            ("b", "off", 1),
            ("b", "on", 0),
            ("c", "on", 1),
        )
    ]
    result = paired(rows, "q")
    assert (result["pairs"], result["better"], result["worse"], result["delta"]) == (2, 1, 1, 0)
