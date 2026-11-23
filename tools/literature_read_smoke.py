"""Check the opt-in reader with real public sources and the native Luna Agent."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
from tools.gap_benchmark.data import save_new
from tools.luna_credentials import MODEL, authorized_config
from tools.luna_native_product_retest import (
    NativeLunaGateway,
    NativeModelAudit,
    NativeModelSlots,
    build_product_app,
    collect_model_audit,
)
from tools.qasper_benchmark import code_hash, fetch_json, kimi_case

from citation_tool.api import create_app
from citation_tool.config import PROJECT_ROOT, LibraryConfig, Settings
from citation_tool.entities import utc_now_iso
from citation_tool.jcs import sha256_json
from citation_tool.literature_reader import LiteratureReader
from citation_tool.online import EuropePMCOnlineAdapter
from citation_tool.runtime import build_metadata_adapter

SAMPLE = PROJECT_ROOT / "data" / "runtime" / "quality-corpus" / "real-corpus.json"
ONLINE_QUESTION = (
    "Using the original full text of PRISMA 2020 (PMC8005924) from the selected source, "
    "not from memory, give the exact original sentence stating whether PRISMA 2020 should "
    "be used to assess the conduct or methodological quality of systematic reviews. "
    "Return one JSON object with fields doi, quote and section (the last section heading). "
    "Do not generate a recommendation ranking."
)
LOCAL_QUESTION = (
    'Find the article whose title contains "floral organ development in soybean" in the '
    "selected snapshot. Give the exact abstract sentence comparing the double mutants and "
    "lfy2 and stating the outcome. Return one JSON object with fields doi, quote and section "
    '(use "Abstract"). Do not generate a recommendation ranking.'
)
ONLINE_EXPECT = {
    "doi": "10.1136/bmj.n71",
    "quote": (
        "PRISMA 2020 should not be used to assess the conduct or methodological quality "
        "of systematic reviews; other tools exist for this purpose."
    ),
    "section": "Scope of the guideline",
}
LOCAL_EXPECT = {
    "doi": "10.1007/s42994-024-00192-2",
    "quote": (
        "The lfy1 lfy2 double mutants displayed more serious defects in floral organ development "
        "than lfy2, resulting in complete sterility."
    ),
    "section": "Abstract",
}


def setup(path: Path, online: bool):
    library = LibraryConfig(
        "europepmc-online" if online else "abiotech-sample",
        "Europe PMC" if online else "aBiotech snapshot",
        "",
        0,
        simulation=False,
        source_type="remote_api" if online else "local_snapshot",
        corpus_path=None if online else SAMPLE,
    )
    settings = Settings(
        db_path=path,
        libraries=(library,),
        library_mode="metadata",
        model_config_path=None,
        allow_user_models=False,
        online_timeout_ms=30_000,
        agent_literature_reader=True,
    )
    adapter = (
        EuropePMCOnlineAdapter(library, timeout_ms=settings.online_timeout_ms)
        if online
        else build_metadata_adapter(settings)
    )
    return settings, adapter


async def verify_paging(root: Path) -> tuple[list[dict], dict]:
    settings, adapter = setup(root / "paging.sqlite", True)
    library_id = adapter.library.library_id
    app = create_app(settings, adapters={library_id: adapter})
    async with app.router.lifespan_context(app):
        tool = LiteratureReader(app.state.search_service, [library_id]).as_tool()
        opened = await tool.ainvoke(
            {
                "action": "open",
                "identifier": "PMC8005924",
                "level": "full_text",
            }
        )
        if opened.get("error"):
            return [], {"passed": False, "error": opened["error"]}
        texts: list[dict] = []
        pages = 0
        window = opened["next_window"]
        while window is not None:
            payload = await tool.ainvoke(
                {
                    "action": "read",
                    "text_ref": opened["text_ref"],
                    "limit": 50,
                    **window,
                }
            )
            if payload.get("error"):
                return [], {"passed": False, "error": payload["error"], "pages": pages}
            pages += 1
            for item in payload["passages"]:
                index = item["offset"]
                if index == len(texts) and item["start"] == 0:
                    texts.append({**item, "text": ""})
                if index != len(texts) - 1 or item["start"] != len(texts[index]["text"]):
                    raise ValueError("read pagination did not cover contiguous positions")
                texts[index]["text"] += item["text"]
            window = payload["next_window"]
        match = await tool.ainvoke(
            {
                "action": "verify",
                "text_ref": opened["text_ref"],
                "text": ONLINE_EXPECT["quote"],
            }
        )
    count = sum(len(row["text"]) for row in texts)
    rows = [
        row
        for row in match.get("matches", [])
        if row["section_path"][-1] == ONLINE_EXPECT["section"]
    ]
    return texts, {
        "passed": len(texts) == opened["block_count"]
        and count == opened["text_chars"]
        and bool(rows)
        and opened["source"]["doi"] == ONLINE_EXPECT["doi"],
        "block_count": len(texts),
        "text_chars": count,
        "pages": pages,
        "provenance": opened["provenance"],
        "verified": rows,
        "text_sha256": sha256_json(texts),
    }


def meets_expectation(answer: str, expected: dict) -> bool:
    try:
        return json.loads(answer) == expected
    except (ValueError, TypeError):
        return False


async def product_case(path: Path, config, online: bool) -> dict:
    settings, adapter = setup(path / "app.sqlite", online)
    settings = replace(settings, agent_timeout_ms=240_000)
    audit = NativeModelAudit(path / "model-calls", NativeModelSlots())
    gateway = NativeLunaGateway(config, audit)
    app, _, _ = build_product_app(settings, adapter, gateway, None)
    prompt, expected = (
        (ONLINE_QUESTION, ONLINE_EXPECT) if online else (LOCAL_QUESTION, LOCAL_EXPECT)
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client,
    ):
        try:
            started = await fetch_json(
                client,
                "POST",
                "/api/v1/agent/runs",
                json={
                    "input_type": "keywords",
                    "keywords": ["original source verification"],
                    "library_ids": [adapter.library.library_id],
                    "message": prompt,
                },
            )
            run_id = started["run_id"]
            task = app.state.agent_runner._tasks.get(run_id)
            if task is not None:
                await task
            result = await fetch_json(client, "GET", f"/api/v1/agent/runs/{run_id}")
        finally:
            await gateway.aclose()
    model = collect_model_audit(path / "model-calls")
    observations = (result.get("context_manifest", {}).get("literature_reader") or {}).get(
        "observations", []
    )
    level = "full_text" if online else "abstract"
    read_source = any(obs.get("level") == level and obs.get("spans") for obs in observations)
    if not online:
        read_source = read_source and any(obs.get("action") == "discover" for obs in observations)
    return {
        "passed": result["status"] == "completed"
        and not result["queries"]
        and read_source
        and model["model_valid"]
        and meets_expectation(result.get("summary") or "", expected),
        "status": result["status"],
        "error": (result.get("error") or {}).get("code"),
        "run_id": run_id,
        "text_read": read_source,
        "model_verified": model["model_valid"],
        "models": model["served_models"],
        "model_calls": model["model_calls"],
        "query_count": len(result["queries"]),
        "answer": result.get("summary"),
        "tool_calls": result["tool_calls"],
        "observations": observations,
    }


async def run(root: Path, *, with_model: bool, compare_kimi: bool) -> dict:
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=False)
    plan = {
        "started_at": utc_now_iso(),
        "code_sha256": code_hash(),
        "model": MODEL if with_model else None,
        "product_questions": [ONLINE_QUESTION, LOCAL_QUESTION],
        "scope": "Public tool acceptance and up to two Luna development probes. "
        "No ranking or broad quality inference. Kimi gets the same official text as an attachment; "
        "this does not test live provider discovery.",
    }
    save_new(root / "plan.json", plan)
    outcome = {"plan": plan}
    try:
        texts, result = await verify_paging(root)
        outcome["paging"] = result
        if with_model:
            config = await asyncio.to_thread(authorized_config)
            outcome["local_luna"] = await product_case(root / "local", config, False)
            save_new(root / "local.json", outcome["local_luna"])
            outcome["online_luna"] = await product_case(root / "online", config, True)
            save_new(root / "online.json", outcome["online_luna"])
        if texts and compare_kimi:
            material = root / "material"
            material.mkdir()
            (material / "paper.txt").write_text(
                "\n".join(f"{' / '.join(row['section_path'])}: {row['text']}" for row in texts),
                encoding="utf-8",
            )
            (material / "prompt.txt").write_text(ONLINE_QUESTION, encoding="utf-8")
            row = await asyncio.to_thread(kimi_case, root / "kimi", material)
            outcome["kimi_code"] = {
                **row,
                "passed": bool(row.get("completed"))
                and row.get("model_valid")
                and meets_expectation(row.get("answer") or "", ONLINE_EXPECT),
            }
    except (OSError, ValueError, TimeoutError, httpx.HTTPError) as exc:
        outcome["execution_error"] = type(exc).__name__
    outcome["passed"] = "execution_error" not in outcome and all(
        outcome.get(key, {}).get("passed")
        for key in (
            "paging",
            *(["local_luna", "online_luna"] if with_model else []),
            *(["kimi_code"] if compare_kimi else []),
        )
    )
    save_new(root / "assessment.json", outcome)
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument("--luna", action="store_true", help="send two public questions to Luna")
    parser.add_argument(
        "--compare-kimi", action="store_true", help="one same-text Kimi Code comparison"
    )
    args = parser.parse_args()
    result = asyncio.run(
        run(args.output_directory, with_model=args.luna, compare_kimi=args.compare_kimi)
    )
    print(json.dumps({k: v.get("passed") if isinstance(v, dict) else v for k, v in result.items()}))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
