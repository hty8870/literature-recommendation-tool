"""Deterministic long-conversation fixtures for the QASPER workspace context pair."""

from __future__ import annotations

import json
from pathlib import Path

from citation_tool.entities import AgentRun, SearchCreate, utc_now_iso
from citation_tool.store import Store

LATE_REQUEST = (
    "Please answer the exact research question I asked in the first turn of this conversation. "
    "Keep the first turn's evidence and JSON requirements. The intervening editorial notes "
    "were not changes to that question."
)


def fixture(original_prompt: str) -> dict:
    notes = (
        "Preserve section headings and their order, even if the bibliography is long.",
        "Don't turn a caption into a table cell or infer the value of any missing graphic.",
        "Use the text's spelling of model names and use standard spacing in the editor.",
        "Please leave citation formatting for later; this is not a request for new sources.",
        "Keep archival identifiers and the visible title exactly as originally supplied.",
        "Do not count the number of pages from the number of paragraphs in the export.",
        "Line wrapping is a display setting and should not introduce new page numbers.",
        "Section labels may be repeated; preserve their order and section boundaries.",
        "Do not expand abbreviated venue names if the supplied manuscript lacks full names.",
        "Please do not add a bibliography appendix or renumber any of the document labels.",
        "The exported plain text and the manuscript must remain separate views.",
        "Keep layout notes and editorial metadata out of the research response.",
        "Keep the abstract separate from the body and keep all ordinary paragraphs in order.",
        "We're done with presentation comments; no scientific requirements were changed.",
    )
    boilerplate = (
        " This is an editorial note for a later layout pass, not a change to the original "
        "research request. It does not introduce any experiments, factual findings, sources, "
        "or values about the paper. It should be kept separately from the source text. "
        "Retain the existing artifact, file order, version identifiers, and provenance labels "
        "when restoring the page and its reading position."
    )
    acknowledgement = (
        "Noted for the presentation layer only. I will keep the archived conversation and the "
        "manuscript separate and will not edit the supplied source. I have not answered the "
        "original research question, run a search, or introduced any evidence in this turn. "
        "The existing labels and original instructions still apply. This note is not a quote."
    )
    return {
        "turns": [[original_prompt, "I'll use the supplied manuscript; no answer yet."]]
        + [[note + boilerplate + boilerplate, acknowledgement] for note in notes],
        "followup": LATE_REQUEST,
    }


def seed(store: Store, tenant: str, manuscript: str, library: str, fixture_path: Path) -> str:
    value = json.loads(fixture_path.read_text(encoding="utf-8"))
    request = SearchCreate(input_type="full_text", manuscript_id=manuscript, library_ids=[library])
    cid = None
    for i, (user, answer) in enumerate(value["turns"], 1):
        now = utc_now_iso()
        run = AgentRun(
            run_id=f"agt_seed_{i:04d}",
            tenant_id=tenant,
            request=request,
            status="pending",
            provider="benchmark-history-fixture",
            model="none",
            conversation_id=cid,
            user_message=user,
            created_at=now,
            updated_at=now,
        )
        run, _ = store.create_conversation_agent_run(run, None, create_conversation=cid is None)
        cid = run.conversation_id
        closed = run.model_copy(update={"status": "completed", "summary": answer})
        store.finish_conversation_run(closed)
    assert cid is not None
    return cid
