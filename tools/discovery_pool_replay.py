from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from time import perf_counter

from citation_tool.config import LibraryConfig
from citation_tool.entities import CandidateDiagnostic, ClaimContext
from citation_tool.jcs import sha256_json
from citation_tool.listwise import (
    LISTWISE_ALGORITHM,
    LISTWISE_PROMPT_VERSION,
    DiscoveryModels,
    ListwiseReranker,
    RerankFailure,
)
from citation_tool.metadata_retrieval import MetadataCorpusAdapter
from citation_tool.reasons import DeepSeekModelGateway, load_model_config
from citation_tool.store import Store

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "data/runtime"


def read_pool(database: Path, tenant: str, query_id: str, claim_id: str | None = None):
    database = database.resolve()
    if not database.is_relative_to(RUNTIME) or not database.is_file():
        raise ValueError("use an existing application database inside data/runtime")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        row = connection.execute(
            "SELECT claims_json FROM searches WHERE tenant_id=? AND query_id=?", (tenant, query_id)
        ).fetchone()
        if row is None:
            raise ValueError("search not found for this tenant")
        claims = [ClaimContext.model_validate(value) for value in json.loads(row[0])]
        if claim_id is not None:
            claims = [claim for claim in claims if claim.claim_id == claim_id]
        if len(claims) != 1:
            raise ValueError("select exactly one existing claim")
        claim = claims[0]
        items = [
            CandidateDiagnostic.model_validate_json(value[0])
            for value in connection.execute(
                "SELECT payload_json FROM candidate_diagnostics WHERE tenant_id=? AND query_id=?",
                (tenant, query_id),
            )
        ]
    items = [
        item
        for item in items
        if item.claim_id == claim.claim_id and item.ranking_status != "ineligible"
    ]
    if not items:
        raise ValueError("no eligible candidates in the selected stored pool")
    evidence = {}
    for item in items:
        record = {
            "work_id": item.work_id,
            "paper_id": item.paper_id,
            "title": item.title,
            "abstract": item.evidence_excerpt,
            "doi": item.doi,
            "authors": item.authors,
            "year": item.year,
            "source_metadata_url": item.source_uri,
            "pub_types": item.publication_types,
            "publication_status": item.publication_status,
        }
        library = LibraryConfig(item.library_ids[0], item.library_ids[0], "", 0, simulation=False)
        adapter = MetadataCorpusAdapter(library, [record])
        evidence[item.work_id] = adapter._candidate(record, claim, 0)
    return claim, items, evidence


async def run(args: argparse.Namespace) -> int:
    from tools.luna_credentials import MODEL, authorized_config

    output = args.output.resolve()
    if not output.is_relative_to(RUNTIME) or output.exists():
        raise ValueError("use a new output path inside data/runtime")
    claim, items, evidence = read_pool(args.database, args.tenant, args.query_id, args.claim_id)
    if args.model == MODEL:
        config = authorized_config()
    else:
        config = load_model_config(RUNTIME / "deepseek.local.json")
        if config is None or config.provider != "deepseek" or config.model != "deepseek-flash":
            raise ValueError("this replay requires the configured official deepseek-flash")
    gateway = DeepSeekModelGateway(config, max_inflight=1, timeout_ms=180_000)
    models = DiscoveryModels(
        gateway.agent_model(), gateway.rerank_model(), config.provider, config.model, 180
    )
    report = {
        "scope": "stored_candidate_pool_only; not new retrieval or independent scientific judgment",
        "source_database": args.database.resolve().relative_to(ROOT).as_posix(),
        "query_id": claim.query_id,
        "claim_id": claim.claim_id,
        "candidate_count": len(items),
        "model": config.model,
        "api_type": config.api_type,
        "prompt_version": LISTWISE_PROMPT_VERSION,
        "algorithm": LISTWISE_ALGORITHM,
        "stored_inputs_sha256": sha256_json(
            [ListwiseReranker._input(item) for item in sorted(items, key=lambda item: item.work_id)]
        ),
    }
    with tempfile.TemporaryDirectory(prefix="pool-replay-", dir=RUNTIME) as temporary:
        store = Store(Path(temporary) / "cache.sqlite")
        reranker = ListwiseReranker(models, store, asyncio.Semaphore(1))
        try:
            started = perf_counter()
            first = await reranker.rank(claim, items, evidence)
            elapsed = perf_counter() - started
            cached = store.get_cached_ranking(claim.tenant_id, first.input_hash)
            exact_replay = cached is not None and first == await reranker.rank(
                claim, list(reversed(items)), evidence
            )
            review = first.score_review
            model_verified = first.served_model == config.model and (
                review.status == "not_applicable" or review.served_model == config.model
            )
            report.update(
                status="passed" if exact_replay and model_verified else "failed",
                ranked_count=len(first.items),
                window_count=first.window_count,
                input_hash=first.input_hash,
                served_model=first.served_model,
                score_review=review.model_dump(mode="json"),
                model_verified=model_verified,
                elapsed_s=round(elapsed, 3),
                exact_cached_replay=exact_replay,
                items=[item.model_dump(mode="json") for item in first.items],
            )
        except RerankFailure as exc:
            report.update(status="failed", failure_code=exc.code, failure_details=exc.details)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "items"}, ensure_ascii=False
        )
    )
    return 0 if report["status"] == "passed" else 1


def main() -> int:
    from tools.luna_credentials import MODEL

    parser = argparse.ArgumentParser(
        description="Replay an unchanged stored pool using the real model"
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--query-id", required=True)
    parser.add_argument("--claim-id")
    parser.add_argument("--model", choices=[MODEL, "deepseek-flash"], default=MODEL)
    parser.add_argument("--output", required=True, type=Path)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    if not __package__:
        sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
