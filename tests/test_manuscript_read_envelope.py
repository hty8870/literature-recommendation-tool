from __future__ import annotations

import hashlib

from citation_tool.manuscript_tools import build_manuscript_tools, manuscript_read_dois


class PublicPassageReader:
    def __init__(self):
        self.rows = [
            {
                "passage_index": index,
                "page": index // 8 + 1,
                "paragraph_index": index,
                "text": f"Public synthetic paragraph {index}. " + "x" * 1_000,
                "text_truncated": False,
            }
            for index in range(64)
        ]
        for row in self.rows:
            row["text_sha256"] = hashlib.sha256(row["text"].encode()).hexdigest()

    def passages(self, tenant, manuscript, *, offset, limit):
        assert tenant == "local"
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
        assert tenant == "local"
        return {
            "manuscript_id": manuscript,
            "passage_count": len(self.rows),
            "catalog": [
                {"passage_index": row["passage_index"], "preview": row["text"][:160]}
                for row in self.rows[offset : offset + limit]
            ],
            "offset": offset,
            "has_more": offset + limit < len(self.rows),
        }


async def test_reader_can_cover_a_normal_paper_beyond_the_old_24000_character_cutoff():
    reader = PublicPassageReader()
    manifest = []
    tool = build_manuscript_tools(
        reader,
        tenant_id="local",
        current_manuscript_id="ms_public",
        document_ids=["ms_public"],
        read_manifest=manifest,
    )[0]
    texts = []
    for offset in range(0, len(reader.rows), 8):
        response = await tool.ainvoke({"offset": offset, "limit": 8})
        assert "error" not in response
        assert all(not row["text_truncated"] for row in response["passages"])
        texts.extend(row["text"] for row in response["passages"])
    assert len(texts) == 64
    assert sum(map(len, texts)) > 60_000
    assert not response["has_more"]
    assert len(manifest) == 8


async def test_agent_can_page_the_catalog_without_claiming_to_have_read_the_body():
    reader = PublicPassageReader()
    manifest = []
    tool = build_manuscript_tools(
        reader,
        tenant_id="local",
        current_manuscript_id="ms_public",
        document_ids=["ms_public"],
        read_manifest=manifest,
    )[0]
    response = await tool.ainvoke({"view": "catalog", "offset": 50, "limit": 50})
    assert response["view"] == "catalog"
    assert response["catalog"][0]["passage_index"] == 50
    assert response["returned"] == 14
    assert not response["has_more"]
    assert all("text" not in row and len(row["preview"]) <= 81 for row in response["catalog"])
    assert manifest[0]["passage_indices"] == []
    assert manifest[0]["catalog_indices"] == list(range(50, 64))
    body = await tool.ainvoke({"view": "passages", "limit": 50})
    assert "error" not in body
    assert sum(len(row["text"]) for row in body["passages"]) <= 20_000
    assert body["has_more"] is True
    assert body["next_offset"] == body["passages"][-1]["passage_index"]


async def test_default_large_window_covers_short_paragraphs_without_dropping_body():
    reader = PublicPassageReader()
    reader.rows = [{**row, "text": row["text"][:100]} for row in reader.rows]
    manifest = []
    tool = build_manuscript_tools(
        reader,
        tenant_id="local",
        current_manuscript_id="ms_public",
        document_ids=["ms_public"],
        read_manifest=manifest,
    )[0]
    first = await tool.ainvoke({"offset": 0})
    assert first["returned"] == 50
    second = await tool.ainvoke({"offset": first["next_offset"]})
    assert second["returned"] == 14
    assert second["has_more"] is False
    returned = first["passages"] + second["passages"]
    assert [row["passage_index"] for row in returned] == list(range(64))
    assert [row["text"] for row in returned] == [row["text"] for row in reader.rows]
    assert len(manifest) == 2


async def test_last_partially_returned_paragraph_cannot_claim_end_of_document(monkeypatch):
    import citation_tool.manuscript_tools as reader_module

    reader = PublicPassageReader()
    reader.rows = reader.rows[:1]
    monkeypatch.setattr(reader_module, "MAX_TOTAL_CHARS", 100)
    tool = build_manuscript_tools(
        reader, tenant_id="local", current_manuscript_id="ms_public", document_ids=["ms_public"]
    )[0]
    response = await tool.ainvoke({"offset": 0, "limit": 1})
    assert response["passages"][0]["text_truncated"]
    assert response["has_more"] is True
    assert response["read_manifest"]["chars_remaining"] == 0


async def test_catalog_previews_do_not_spend_the_body_reading_budget():
    reader = PublicPassageReader()
    tool = build_manuscript_tools(
        reader, tenant_id="local", current_manuscript_id="ms_public", document_ids=["ms_public"]
    )[0]
    for offset in (0, 50):
        response = await tool.ainvoke({"view": "catalog", "offset": offset, "limit": 50})
        assert response["read_manifest"]["chars_used"] == 0
        assert response["read_manifest"]["chars_remaining"] == 160_000
        assert response["read_manifest"]["catalog_chars_used"] > 0
    body = await tool.ainvoke({"offset": 0, "limit": 8})
    assert body["read_manifest"]["chars_used"] == sum(len(p["text"]) for p in body["passages"])


async def test_catalog_continuation_uses_actual_returned_count_without_gaps(monkeypatch):
    import citation_tool.manuscript_tools as module

    monkeypatch.setattr(module, "MAX_CALL_CHARS", 1000)
    reader = PublicPassageReader()
    tool = build_manuscript_tools(
        reader, tenant_id="local", current_manuscript_id="ms_public", document_ids=["ms_public"]
    )[0]
    offset, seen = 0, []
    while True:
        response = await tool.ainvoke({"view": "catalog", "offset": offset, "limit": 50})
        assert "error" not in response
        assert response["returned"] > 0
        seen.extend(p["passage_index"] for p in response["catalog"])
        if not response["has_more"]:
            assert response["next_offset"] is None
            break
        assert response["next_offset"] == offset + response["returned"]
        offset = response["next_offset"]
    assert seen == list(range(64))


async def test_catalog_exhaustion_is_an_explicit_error_and_body_remains_available(monkeypatch):
    import citation_tool.manuscript_tools as module

    monkeypatch.setattr(module, "MAX_CATALOG_TOTAL_CHARS", 10)
    tool = build_manuscript_tools(
        PublicPassageReader(),
        tenant_id="local",
        current_manuscript_id="ms_public",
        document_ids=["ms_public"],
    )[0]
    response = await tool.ainvoke({"view": "catalog", "limit": 50})
    assert response["error"]["code"] == "MANUSCRIPT_CATALOG_BUDGET_EXCEEDED"
    assert "has_more" not in response
    assert response["error"]["details"]["body_chars_remaining"] == 160_000
    body = await tool.ainvoke({"offset": 0, "limit": 1})
    assert body["passages"][0]["text"]
    assert not body["passages"][0]["text_truncated"]


async def test_extended_manuscript_literal_location_and_window_are_scope_bound(monkeypatch):
    from citation_tool import manuscript_tools as module

    reader = PublicPassageReader()
    reader.rows[61]["text"] = "A phrase occurs twice. A phrase occurs twice."
    reader.rows[61]["source_method"] = "ocr"
    reader.rows[61]["warnings"] = [{"code": "ocr_text_unverified"}]
    tool = build_manuscript_tools(
        reader,
        tenant_id="local",
        current_manuscript_id="ms_public",
        document_ids=["ms_public"],
        locate_enabled=True,
    )[0]
    prompt = {"view": "find", "text": "A PHRASE OCCURS TWICE.", "limit": 1}
    hit = await tool.ainvoke(prompt)
    (row,) = hit["matches"]
    assert row["passage_index"] == 61 and row["page"] == 8
    assert row["quote"] == "A phrase occurs twice."
    assert row["warnings"] == [{"code": "ocr_text_unverified"}]
    next_hit = await tool.ainvoke({**prompt, **hit["next_window"]})
    assert next_hit["matches"][0]["match_start"] > row["match_start"]
    assert next_hit["next_window"] is None
    assert not (await tool.ainvoke({**prompt, "view": "verify"}))["matched"]
    assert (await tool.ainvoke({**prompt, "manuscript_id": "outside"}))["error"]["code"] == (
        "MANUSCRIPT_NOT_ALLOWED"
    )
    monkeypatch.setattr(module, "MAX_CALL_CHARS", 500)
    first = await tool.ainvoke({"offset": 0, "limit": 1})
    second = await tool.ainvoke({"limit": 1, **first["next_window"]})
    assert (
        first["passages"][0]["text"] + second["passages"][0]["text"]
        == reader.rows[0]["text"][:1000]
    )
    assert second["next_window"] == {"offset": 0, "char_offset": 1000}


def _replace_text(reader, index, text, **coordinates):
    reader.rows[index].update(
        text=text, text_sha256=hashlib.sha256(text.encode()).hexdigest(), **coordinates
    )


async def test_find_discloses_unique_same_section_neighbors_without_changing_hits():
    reader = PublicPassageReader()
    _replace_text(reader, 48, "Wrong chapter.", section_path=["Methods"])
    _replace_text(reader, 49, "Measured effect in group A.", section_path=["Results"])
    control = "Only the matched controls contributed to this comparison. DOI: 10.1234/control."
    _replace_text(
        reader,
        50,
        control,
        section_path=["Results"],
        source_method="ocr",
        warnings=[{"code": "ocr_text_unverified"}],
    )
    _replace_text(reader, 51, "Measured effect in group B.", section_path=["Results"])
    _replace_text(reader, 52, "Wrong page.", section_path=["Results"], page=8)

    def make(neighbors, manifest=None):
        return build_manuscript_tools(
            reader,
            tenant_id="local",
            current_manuscript_id="ms_public",
            document_ids=[],
            locate_enabled=True,
            find_neighbors=neighbors,
            read_manifest=manifest,
        )[0]

    manifest = []
    old, new = make(False), make(True, manifest)
    assert old.description == new.description and old.args_schema == new.args_schema
    request = {"view": "find", "text": "measured effect"}
    baseline, result = await old.ainvoke(request), await new.ainvoke(request)
    assert result["matches"] == baseline["matches"]
    assert result["next_window"] is baseline["next_window"] is None
    (neighbor,) = result["neighbor_passages"]
    assert "neighbor_passages" not in baseline
    assert neighbor["passage_index"] == 50 and neighbor["adjacent_to"] == [49, 51]
    assert neighbor["scope"] == "same_recorded_section" and neighbor["text"] == control
    assert (neighbor["start"], neighbor["end"]) == (0, len(control))
    assert "match_start" not in neighbor and "quote" not in neighbor
    assert neighbor["warnings"] == [{"code": "ocr_text_unverified"}]
    assert manifest[0]["neighbor_passages"][0]["source_sha256"] == neighbor["text_sha256"]
    assert control not in str(manifest)

    assert manuscript_read_dois("local", manifest) == set()
    request["limit"] = 1
    before, after = await old.ainvoke(request), await new.ainvoke(request)
    assert before["matches"] == after["matches"]
    assert before["next_window"] == after["next_window"] == {"offset": 51, "char_offset": 0}
    assert "neighbor_passages" not in await new.ainvoke({"view": "verify", "text": "Measured"})
    assert "neighbor_passages" not in await new.ainvoke({"view": "find", "text": "missing"})
    assert (await new.ainvoke({**request, "manuscript_id": "outside"}))["error"]["code"] == (
        "MANUSCRIPT_NOT_ALLOWED"
    )


async def test_neighbor_budget_preserves_primary_and_does_not_return_partial_neighbor(monkeypatch):
    from citation_tool import manuscript_tools as module

    reader = PublicPassageReader()
    reader.rows = reader.rows[:3]
    for row in reader.rows:
        row["page"] = None
    _replace_text(reader, 0, "Longer context about a different group.")
    _replace_text(reader, 1, "Unique anchor.")
    _replace_text(reader, 2, "Control excluded 🦉.")
    monkeypatch.setattr(module, "MAX_TOTAL_CHARS", 40)
    monkeypatch.setattr(module, "MAX_CALL_CHARS", 36)
    tool = build_manuscript_tools(
        reader,
        tenant_id="local",
        current_manuscript_id="ms_public",
        document_ids=[],
        locate_enabled=True,
        find_neighbors=True,
    )[0]
    result = await tool.ainvoke({"view": "find", "text": "anchor"})
    (hit,) = result["matches"]
    (neighbor,) = result["neighbor_passages"]
    assert hit["text"] == reader.rows[1]["text"]
    assert neighbor["scope"] == "unsectioned_parse_order"
    assert not neighbor["text_truncated"] and neighbor["end"] == len(reader.rows[2]["text"])
    follow = await tool.ainvoke({"offset": 0, "limit": 1})
    assert follow["read_manifest"]["chars_used"] == 40
    assert follow["next_window"] == {
        "offset": 0,
        "char_offset": 40 - len(hit["text"]) - len(neighbor["text"]),
    }


def test_catalog_projection_does_not_repeat_full_body_or_low_value_metadata():
    from citation_tool.manuscript_tools import _catalog_entry

    projected = _catalog_entry(
        {
            "passage_index": 3,
            "page": 1,
            "preview": "Short actual preview",
            "bbox": {"x0": 1},
            "paragraph_text": "UNNECESSARY_FULL_BODY",
            "text_sha256": "hash",
        }
    )
    assert projected == {"passage_index": 3, "page": 1, "preview": "Short actual preview"}


def test_document_identity_is_not_a_scientific_evaluation_criterion():
    from citation_tool.agent import _effective_evaluation_anchor
    from citation_tool.entities import SearchCreate

    request = SearchCreate(input_type="full_text", manuscript_id="ms_opaque_identity123")
    assert _effective_evaluation_anchor(None, request, None) is None
    explicit = _effective_evaluation_anchor(None, request, "仅查方法学背景研究")
    assert explicit.claim_text == "仅查方法学背景研究"
    assert request.manuscript_id not in explicit.claim_text

    from citation_tool.agent import _conversation_message

    displayed_request = _conversation_message(request, None)
    assert "查找相关文献" in displayed_request
    assert request.manuscript_id not in displayed_request
