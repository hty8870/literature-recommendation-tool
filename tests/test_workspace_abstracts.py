from types import SimpleNamespace

import pytest

from citation_tool.config import Settings
from citation_tool.entities import CandidateSourceRecord
from citation_tool.research_documents import ResearchDocuments
from citation_tool.research_workspace import ResearchWorkspace, WorkspaceError


def reader(*, enabled):
    first = CandidateSourceRecord(
        library_id="library_a",
        paper_id="paper_a",
        title="Published title",
        abstract="第一段：测到了响应。\nThe confidence interval includes zero.",
        doi="10.1000/paper-a",
        publication_status="none",
        content_hash="version-a",
    )
    second = first.model_copy(
        update={
            "library_id": "library_b",
            "paper_id": "paper_b",
            "abstract": "Engine note: 二版原文。",
            "doi": "10.1000/paper-b",
            "content_hash": "version-b",
            "publication_status": "corrected",
        }
    )
    rows = [
        SimpleNamespace(work_id="work_a", result_id="r_a", source_records=[first, second]),
        SimpleNamespace(work_id="work_a", result_id="r_a_repeat", source_records=[first]),
    ]
    state = SimpleNamespace(
        session=SimpleNamespace(query_id="q_allowed"), candidate_diagnostics=rows, results=[]
    )
    service = SimpleNamespace(
        state=lambda tenant, q: state if (tenant, q) == ("tenant", "q_allowed") else None
    )
    run = SimpleNamespace(
        tenant_id="tenant", turn_index=2, prior_query_ids=[], active_query_id=None
    )
    docs = ResearchDocuments(service, run, [], [], None, None, batch_abstracts=enabled)
    receipt = docs.add_query(
        {
            "query_id": "q_allowed",
            "candidate_diagnostics": [
                {
                    "source_records": [first.model_dump(), second.model_dump()],
                    "reason": "Not evidence.",
                }
            ],
        }
    )
    return docs, receipt, rows


async def test_grouped_abstracts_are_exact_versioned_and_optional():
    docs, receipt, rows = reader(enabled=True)
    path = receipt["abstracts_path"]
    assert path in list(docs.catalog("searches/q_allowed"))
    assert docs.hints()["active_abstracts_path"] == path
    assert "candidate_diagnostics" not in receipt
    workspace = ResearchWorkspace(
        docs.catalog, docs.resolve, manuscript_budget={}, disclose=docs.disclose
    )
    tools = {tool.name: tool for tool in workspace.tools()}
    page = await tools["Read"].ainvoke({"path": path, "n_lines": 3})
    assert page["kind"] == "candidate_abstracts"
    assert page["next_arguments"]["version"] == page["version"]
    assert docs.disclosed_dois == {"10.1000/paper-a"}
    hits = await tools["Grep"].ainvoke({"path": path, "pattern": "零|zero|二版"})
    assert [row["anchor"]["paper_id"] for row in hits["lines"]] == ["paper_a", "paper_b"]
    for line, source in zip(hits["lines"], rows[0].source_records, strict=True):
        anchor = line["anchor"]
        assert source.abstract[anchor["start"] : anchor["end"]] == line["text"]
        assert anchor["content_hash"] == source.content_hash
        assert anchor["publication_status"] == source.publication_status
    tail = await tools["Read"].ainvoke(page["next_arguments"])
    assert any("corrected" in item["text"] for item in tail["lines"])
    assert len(tail["lines"]) == 2
    assert docs.disclosed_dois == {"10.1000/paper-a", "10.1000/paper-b"}
    assert "Not evidence" not in str(page) + str(tail)
    rows[0].source_records.pop()
    stale = await tools["Read"].ainvoke(page["next_arguments"])
    assert stale["error"]["code"] == "WORKSPACE_VERSION_CHANGED"
    unauthorized = await tools["Read"].ainvoke({"path": "searches/q_foreign/abstracts.txt"})
    assert unauthorized["error"]["code"] == "WORKSPACE_NOT_FOUND"


def test_disabled_group_has_no_advertised_or_readable_view():
    docs, receipt, _ = reader(enabled=False)
    path = "searches/q_allowed/abstracts.txt"
    assert path not in list(docs.catalog("searches/q_allowed"))
    assert "abstracts_path" not in receipt
    assert "active_abstracts_path" not in docs.hints()
    with pytest.raises(WorkspaceError) as raised:
        docs.resolve(path)
    assert raised.value.code == "WORKSPACE_NOT_FOUND"
    with pytest.raises(ValueError, match="requires a research workspace"):
        Settings(agent_workspace_abstracts=True).validate()
