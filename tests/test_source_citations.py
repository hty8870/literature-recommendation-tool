from __future__ import annotations

import asyncio

import httpx
import pytest
from langchain_core.messages import AIMessage

from citation_tool.api import create_app
from citation_tool.config import Settings
from citation_tool.publication import citation_dois
from test_agent import ScriptedChatModel, tool_call
from test_integration import headers, search_body


@pytest.mark.parametrize(
    "text, expected",
    [
        ("DOI **10.1234/AbC-1**（摘要）。", {"10.1234/abc-1"}),
        ("(https://doi.org/10.1234/(SICI)123(4)).", {"10.1234/(sici)123(4)"}),
        ("`10.1234/a%2Fb`", {"10.1234/a/b"}),
        ("编号10.1234/a-1；另一个 DOI 10.5678/B_2。", {"10.1234/a-1", "10.5678/b_2"}),
        ("未提供文献编号。", set()),
    ],
)
def test_citation_doi_parsing_keeps_identity_and_ignores_formatting(text, expected):
    assert citation_dois(text) == expected


async def run_agent(tmp_path, live_server, responses, request, *, settings=None, adapters=None):
    model = ScriptedChatModel(responses=responses)
    base = live_server(
        create_app(settings or Settings.for_tests(tmp_path), agent_model=model, adapters=adapters)
    )
    async with httpx.AsyncClient(timeout=10) as client:
        created = await client.post(f"{base}/api/v1/agent/runs", json=request, headers=headers())
        created.raise_for_status()
        run_id = created.json()["run_id"]
        for _ in range(300):
            run = (await client.get(f"{base}/api/v1/agent/runs/{run_id}")).json()
            if run["status"] in {"completed", "failed", "cancelled"}:
                return run, model
            await asyncio.sleep(0.01)
    raise AssertionError("source-grounded Agent response did not finish")


@pytest.mark.parametrize("repair", [True, False])
async def test_final_answer_rejects_unprovided_dois_even_in_exclusion_notes(
    tmp_path, live_server, repair
):
    invalid = "这项排除研究是 DOI 10.9999/unseen，未纳入推荐。"
    fixed = "本次真实返回 DOI 10.5555/demo-fast-001；只按已返回的模拟数据说明，不补排除项编号。"
    run, model = await run_agent(
        tmp_path,
        live_server,
        [
            tool_call("source-citation", ["LFY2"], ["fixture-fast"]),
            AIMessage(content=invalid),
            AIMessage(content=fixed if repair else invalid),
        ],
        search_body({"fixture-fast": 1}),
    )
    if repair:
        assert run["status"] == "completed" and run["error"] is None
        assert "10.9999/unseen" not in run["summary"]
    else:
        assert run["status"] == "failed"
        assert run["error"]["code"] == "AGENT_UNGROUNDED_OUTPUT"
        assert run["error"]["source_dois"] == ["10.9999/unseen"]
    assert any(
        "10.9999/unseen" in str(message.content) and "自主补查" in str(message.content)
        for message in model.received_messages[-1]
    )


async def test_explicit_user_doi_can_be_explained_without_inventing_a_source(tmp_path, live_server):
    run, _ = await run_agent(
        tmp_path,
        live_server,
        [AIMessage(content="你给出的 DOI 10.9999/user-provided 尚无当前来源，不补造题名或结论。")],
        {"input_type": "keywords", "keywords": ["请解释 10.9999/user-provided，不检索"]},
    )
    assert run["status"] == "completed" and not run["queries"]


async def test_explicit_source_correction_doi_is_not_treated_as_invented(tmp_path, live_server):
    from dataclasses import replace

    from citation_tool.config import LibraryConfig
    from citation_tool.metadata_retrieval import MetadataCorpusAdapter

    library = LibraryConfig("source", "Source", "", 0, simulation=False)
    record = {
        "work_id": "work:10.5555/original",
        "paper_id": "original",
        "title": "LFY2 soybean study",
        "abstract": "LFY2 expression was measured in soybean.",
        "doi": "10.5555/original",
        "source_metadata_url": "https://example.org/original",
        "related_dois": {"corrected_by": ["10.5555/source-correction"]},
    }
    settings = replace(Settings.for_tests(tmp_path), libraries=(library,))
    run, _ = await run_agent(
        tmp_path,
        live_server,
        [
            tool_call("linked-source", ["LFY2"], ["source"]),
            AIMessage(content="来源明确关联更正 DOI 10.5555/source-correction；尚未读取更正全文。"),
        ],
        {"input_type": "keywords", "keywords": ["LFY2"], "library_ids": ["source"]},
        settings=settings,
        adapters={"source": MetadataCorpusAdapter(library, [record])},
    )
    assert run["status"] == "completed" and run["error"] is None


async def test_doi_correction_does_not_force_an_unnecessary_search(tmp_path, live_server):
    run, _ = await run_agent(
        tmp_path,
        live_server,
        [
            AIMessage(content="例如 DOI 10.9999/memory-study 可作参考。"),
            AIMessage(
                content="候选相关性与全文证据不同；此处无需检索，不提供未经来源支持的论文编号。"
            ),
        ],
        {"input_type": "keywords", "keywords": ["只解释候选和证据的区别，不检索"]},
    )
    assert run["status"] == "completed" and not run["queries"]
