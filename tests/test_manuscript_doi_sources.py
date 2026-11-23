from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

import citation_tool.agent as agent_module
import citation_tool.manuscript_tools as reader_module
from citation_tool.agent import _unprovided_dois
from citation_tool.api import create_app
from citation_tool.config import Settings
from citation_tool.entities import SearchCreate
from citation_tool.manuscript_tools import build_manuscript_tools, manuscript_read_dois
from citation_tool.manuscripts import ManuscriptError
from citation_tool.publication import DOI_SOURCE_RULE_VERSION, citation_doi_spans, citation_dois
from test_agent import ScriptedChatModel

FOLDED = "10.1093/molbev/msm158"
CONTINUOUS = "10.1104/pp.102.017459"
REFERENCE = (
    "between duplicated genes in Arabidopsis. Mol Biol Evol\n"
    "24(10):2298–2309. https://doi.org/10.1093/molbev/\nmsm158"
)
SECOND = "131(3):911–926. https://doi.org/10.1104/pp.102.017459\nWang H, other text."


class ScopedReader:
    def __init__(self, texts=(REFERENCE, SECOND)):
        self.rows = [
            {
                "passage_index": index,
                "text": text,
                "page": index + 1,
                "char_start": sum(len(previous) for previous in texts[:index]),
                "text_truncated": False,
            }
            for index, text in enumerate(texts)
        ]

    @staticmethod
    def allowed(tenant, manuscript):
        return tenant == "local" and manuscript == "ms_public"

    def get(self, tenant, manuscript, *, catalog_limit=20):
        if not self.allowed(tenant, manuscript):
            return None
        return {
            "manuscript_id": manuscript,
            "parse_status": "ready",
            "page_count": len(self.rows),
            "passage_count": len(self.rows),
            "text_chars": sum(len(row["text"]) for row in self.rows),
            "catalog": self.list_catalog(tenant, manuscript, offset=0, limit=catalog_limit)[
                "catalog"
            ],
            "catalog_truncated": len(self.rows) > catalog_limit,
            "warnings": [],
        }

    def passages(self, tenant, manuscript, *, offset, limit):
        if not self.allowed(tenant, manuscript):
            raise ManuscriptError(404, "MANUSCRIPT_NOT_FOUND")
        return {
            "manuscript_id": manuscript,
            "parse_status": "ready",
            "passage_count": len(self.rows),
            "passages": self.rows[offset : offset + limit],
            "offset": offset,
            "limit": limit,
            "has_more": offset + limit < len(self.rows),
        }

    def list_catalog(self, tenant, manuscript, *, offset, limit):
        if not self.allowed(tenant, manuscript):
            raise ManuscriptError(404, "MANUSCRIPT_NOT_FOUND")
        return {
            "manuscript_id": manuscript,
            "passage_count": len(self.rows),
            "catalog": [
                {"passage_index": row["passage_index"], "preview": row["text"]}
                for row in self.rows[offset : offset + limit]
            ],
            "offset": offset,
            "has_more": offset + limit < len(self.rows),
        }


def reader_tool(reader, manifest, *, tenant="local", document="ms_public"):
    return build_manuscript_tools(
        reader,
        tenant_id=tenant,
        current_manuscript_id=document,
        document_ids=[document],
        read_manifest=manifest,
    )[0]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (REFERENCE, FOLDED),
        (REFERENCE.replace("\nmsm158", "\r\n  MSM158"), FOLDED),
        ("doi: 10.1104/\npp.102.017459", CONTINUOUS),
        (SECOND, CONTINUOUS),
        (f"The source ({CONTINUOUS}).", CONTINUOUS),
    ],
)
def test_doi_spans_preserve_exact_source_and_conservatively_fold_path_breaks(text, expected):
    spans = citation_doi_spans(text)
    assert expected in {span["doi"] for span in spans}
    assert expected in citation_dois(text)
    for span in spans:
        assert text[span["start"] : span["end"]] == span["raw"]
        assert 0 <= span["start"] < span["end"] <= len(text)
    if expected == FOLDED:
        assert "10.1093/molbev/" not in citation_dois(text)


@pytest.mark.parametrize(
    "text",
    [
        "https://doi.org/10.1093/molbev/\n\nmsm158",
        "https://doi.org/10.1093/molbev/\nSmith et al. (2020)",
        "https://doi.org/10.1093/molbev/\nmsm158 Smith et al. (2020)",
        "https://doi.org/10.1093/molbev/\n2020\nWang H, other reference.",
        "https://doi.org/10.1093/molbev/\n1\nWang H, other reference.",
        "https://doi.org/10.1093/molbev/\n10.1104/pp.102.017459",
        "https://doi.org/10.1093/molbev/\nhttps://doi.org/10.1104/pp.102.017459",
        "https://doi.org/10.1093/molbev/msm 158",
        "https://doi.org/10.1093/molbev/msm\n158",
    ],
)
def test_unrelated_lines_or_general_whitespace_never_become_a_new_doi(text):
    assert FOLDED not in citation_dois(text)
    assert f"10.1093/molbev/{CONTINUOUS}" not in citation_dois(text)
    assert not any(
        span["normalization"] == "path_delimiter_linebreak" for span in citation_doi_spans(text)
    )


async def test_only_actual_body_windows_produce_tenant_bound_identifier_receipts():
    reader = ScopedReader()
    manifest = []
    tool = reader_tool(reader, manifest)
    catalog = await tool.ainvoke({"view": "catalog", "offset": 0, "limit": 2})
    assert catalog["catalog"]
    assert manuscript_read_dois("local", manifest) == set()
    body = await tool.ainvoke({"view": "passages", "offset": 0, "limit": 1})
    assert manuscript_read_dois("local", manifest) == {FOLDED}
    assert CONTINUOUS not in manuscript_read_dois("local", manifest)
    assert manuscript_read_dois("foreign", manifest) == set()
    receipt = manifest[-1]
    assert receipt["identifier_rule_version"] == DOI_SOURCE_RULE_VERSION
    assert "text" not in receipt
    span = receipt["provided_doi_spans"][0]
    text = body["passages"][0]["text"]
    assert text[span["start"] : span["end"]] == span["raw"]
    assert span["returned_text_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert span["passage_index"] == 0
    assert "between duplicated genes" not in span["raw"]
    forged = [{**receipt, "view": "catalog"}]
    assert not manuscript_read_dois("local", forged)


async def test_truncated_later_text_and_cut_doi_token_are_not_provided(monkeypatch):
    visible = "10.1234/visible"
    reader = ScopedReader((f"Given {visible}\n" + "x" * 80 + f" {CONTINUOUS}",))
    monkeypatch.setattr(reader_module, "MAX_CALL_CHARS", 40)
    manifest = []
    body = await reader_tool(reader, manifest).ainvoke({})
    assert body["passages"][0]["text_truncated"]
    assert manuscript_read_dois("local", manifest) == {visible}
    monkeypatch.setattr(reader_module, "MAX_CALL_CHARS", len(REFERENCE) - 2)
    manifest = []
    await reader_tool(ScopedReader((REFERENCE,)), manifest).ainvoke({})
    assert manuscript_read_dois("local", manifest) == set()


async def test_cross_tenant_read_failure_provides_no_identifiers():
    manifest = []
    failed = await reader_tool(ScopedReader(), manifest, tenant="foreign").ainvoke({})
    assert failed["error"]["code"] == "MANUSCRIPT_NOT_FOUND"
    assert manifest == []
    assert manuscript_read_dois("foreign", manifest) == set()


async def test_original_search_and_request_doi_sources_still_work_with_body_receipts():
    manifest = []
    await reader_tool(ScopedReader(), manifest).ainvoke({"limit": 1})
    request_doi = "10.1234/user-given"
    source_doi = "10.2345/returned"
    relation_doi = "10.2345/corrected"
    excerpt_doi = "10.2345/from-excerpt"
    run = SimpleNamespace(
        tenant_id="local",
        evaluation_anchor=None,
        request=SearchCreate(input_type="keywords", keywords=[f"DOI {request_doi}"]),
        prior_query_ids=["q_1"],
        queries=[],
        manuscript_reads=manifest,
    )
    service = SimpleNamespace(
        state=lambda tenant, query: SimpleNamespace(
            candidate_diagnostics=[
                SimpleNamespace(
                    doi=source_doi,
                    related_dois={"corrected_by": [relation_doi]},
                    evidence_excerpt=f"Explicit source DOI {excerpt_doi}.",
                )
            ],
            results=[],
        )
    )
    summary = f"{request_doi} {source_doi} {relation_doi} {excerpt_doi} {FOLDED} 10.9999/new"
    assert _unprovided_dois(run, service, summary, manuscript_reads=manifest) == ["10.9999/new"]
    assert FOLDED in _unprovided_dois(run, service, FOLDED)


def read_call(arguments=None):
    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": "read-public-body",
                "name": "read_manuscript",
                "args": arguments or {},
            }
        ],
    )


async def test_agent_accepts_already_read_references_without_forcing_search(tmp_path, monkeypatch):
    summary = f"本稿已列出 {FOLDED} 和 {CONTINUOUS}；这些是已有引用编号，题录和具体支持仍待核查。"
    model = ScriptedChatModel(responses=[read_call(), AIMessage(content=summary)])
    app = create_app(Settings.for_tests(tmp_path), agent_model=model)
    app.state.search_service.manuscripts = ScopedReader()
    calls = []
    original = agent_module._unprovided_dois

    def observe(run, service, text, **kwargs):
        calls.append((len(run.manuscript_reads), len(kwargs.get("manuscript_reads", []))))
        return original(run, service, text, **kwargs)

    monkeypatch.setattr(agent_module, "_unprovided_dois", observe)
    async with app.router.lifespan_context(app):
        pending = app.state.agent_runner.start(
            "local", SearchCreate(input_type="full_text", manuscript_id="ms_public"), None
        )
        await app.state.agent_runner._tasks[pending.run_id]
        run = app.state.store.get_agent_run("local", pending.run_id)
    assert run.status == "completed", run.error
    assert run.summary == summary
    assert run.queries == []
    assert calls == [(0, 1)]
    assert manuscript_read_dois("local", run.manuscript_reads) == {FOLDED, CONTINUOUS}
    assert "between duplicated genes" not in run.model_dump_json()


@pytest.mark.parametrize("mode", ["unread", "catalog", "foreign", "wrong_doi"])
async def test_agent_still_rejects_identifiers_not_in_actual_read_body(tmp_path, mode):
    wanted = "10.1093/molbev/not-provided" if mode == "wrong_doi" else FOLDED
    summary = f"现有引用 {wanted}。"
    replies = []
    if mode != "unread":
        replies.append(read_call({"view": "catalog"} if mode == "catalog" else {}))
    replies += [AIMessage(content=summary), AIMessage(content=summary)]
    model = ScriptedChatModel(responses=replies)
    app = create_app(Settings.for_tests(tmp_path), agent_model=model)
    app.state.search_service.manuscripts = (
        ScopedReader((f"Already listed DOI {FOLDED}", SECOND))
        if mode in {"unread", "catalog"}
        else ScopedReader()
    )
    tenant = "foreign" if mode == "foreign" else "local"
    async with app.router.lifespan_context(app):
        pending = app.state.agent_runner.start(
            tenant, SearchCreate(input_type="full_text", manuscript_id="ms_public"), None
        )
        await app.state.agent_runner._tasks[pending.run_id]
        run = app.state.store.get_agent_run(tenant, pending.run_id)
    assert run.status == "failed"
    assert run.error["code"] == "AGENT_UNGROUNDED_OUTPUT"
    assert run.error["source_dois"] == [wanted]


async def test_correction_can_read_provided_reference_before_second_validation(tmp_path):
    summary = f"本稿已有编号 {FOLDED}，尚未独立核查题录。"
    model = ScriptedChatModel(
        responses=[AIMessage(content=summary), read_call(), AIMessage(content=summary)]
    )
    app = create_app(Settings.for_tests(tmp_path), agent_model=model)
    app.state.search_service.manuscripts = ScopedReader()
    async with app.router.lifespan_context(app):
        pending = app.state.agent_runner.start(
            "local", SearchCreate(input_type="full_text", manuscript_id="ms_public"), None
        )
        await app.state.agent_runner._tasks[pending.run_id]
        run = app.state.store.get_agent_run("local", pending.run_id)
    assert run.status == "completed", run.error
    assert run.queries == []
    assert manuscript_read_dois("local", run.manuscript_reads) == {FOLDED, CONTINUOUS}
