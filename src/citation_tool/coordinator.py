"""In-process coordinator and library workers for M0.

The coordinator is the only writer of public sequence numbers and search state.
Workers process their own library independently and publish each citable
recommendation before moving to the next candidate. Recommendation reasons use
a bounded model pool outside the commit lock; libraries never wait for another
library's first result.

Durability boundary: completed searches, events, and snapshots persist in
SQLite, but an in-flight asyncio task is not a durable queue. Restarting the
process does not resume in-flight searches: startup marks them interrupted,
while completed snapshots and previously committed results remain readable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from typing import TypeVar

from .candidate_pool import CandidatePool
from .candidates import candidate_scoring, retrieval_fingerprint
from .common_scoring import CommonScorer
from .config import Settings
from .entities import (
    CandidateDiagnostic,
    CandidateSourceScore,
    ClaimContext,
    SearchState,
    WorkerStatus,
    utc_now_iso,
)
from .evidence import build_recommendation
from .jcs import sha256_json
from .listwise import DiscoveryModels
from .reasons import ReasonGenerator
from .retrieval import LibraryAdapter, SourceSearchError
from .source_agent import SOURCE_RECALL_PRESERVATION_VERSION, SourceAgent
from .store import Store

logger = logging.getLogger(__name__)
_LibraryResult = TypeVar("_LibraryResult")


class SearchCoordinator:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        adapters: Mapping[str, LibraryAdapter],
        reasons: ReasonGenerator,
        discovery_models: DiscoveryModels | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.adapters = adapters
        self.reasons = reasons
        self.discovery_models = discovery_models
        self._scorer = CommonScorer(adapters) if settings.candidate_pipeline == "agentic" else None
        self._model_gate = asyncio.Semaphore(settings.model_max_inflight)
        self._pools: dict[str, dict[str, CandidatePool]] = {}
        self._source_tasks: dict[str, set[asyncio.Task]] = defaultdict(set)
        self._reason_overrides: dict[str, ReasonGenerator] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._adapter_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._tasks: set[asyncio.Task] = set()
        self._completion: dict[str, Future[bool]] = {}
        self._source_completion: dict[str, Future[bool]] = {}
        self._cancel_requested: set[str] = set()
        self._finalization_requested: set[str] = set()

    def set_reason_override(self, query_id: str, reasons: ReasonGenerator) -> None:
        if query_id in self._reason_overrides:
            raise ValueError("reason generator is already bound to this query")
        self._reason_overrides[query_id] = reasons

    def request_cancel(self, query_id: str) -> None:
        if query_id not in self._completion or (
            query_id in self._cancel_requested and query_id not in self._finalization_requested
        ):
            return
        self._finalization_requested.discard(query_id)
        self._cancel_requested.add(query_id)
        for task in self._source_tasks.get(query_id, set()):
            task.cancel()
        for pool in self._pools.get(query_id, {}).values():
            pool.request_cancel()

    def request_finalization(self, query_id: str) -> None:
        if query_id not in self._cancel_requested:
            self._finalization_requested.add(query_id)
            self._cancel_requested.add(query_id)

    def start(self, tenant_id: str, query_id: str) -> asyncio.Task:
        self._completion[query_id] = Future()
        if self.settings.agent_source_evidence_first:
            self._source_completion[query_id] = Future()
        task = asyncio.create_task(self._run(tenant_id, query_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda _: self._cancel_requested.discard(query_id))
        task.add_done_callback(lambda _: self._finalization_requested.discard(query_id))
        return task

    def has_live_searches(self, query_ids: list[str]) -> bool:
        return any(query_id in self._completion for query_id in query_ids)

    async def wait(self, query_id: str, *, timeout_s: float) -> bool:
        return await self._wait_signal(self._completion.get(query_id), timeout_s)

    async def wait_sources(self, query_id: str, *, timeout_s: float) -> bool:
        return await self._wait_signal(self._source_completion.get(query_id), timeout_s)

    @staticmethod
    async def _wait_signal(signal: Future[bool] | None, timeout_s: float) -> bool:
        if signal is None:
            return False
        try:
            async with asyncio.timeout(timeout_s):
                return await asyncio.shield(asyncio.wrap_future(signal))
        except TimeoutError:
            return False

    async def _run(self, tenant_id: str, query_id: str) -> None:
        try:
            await self._run_search(tenant_id, query_id)
        except Exception as exc:
            logger.exception(
                "search orchestration failed query=%s type=%s", query_id, type(exc).__name__
            )
            try:
                self.store.fail_search(
                    tenant_id, query_id, f"{type(exc).__name__}: in-process orchestration failed"
                )
            except Exception:
                logger.exception("could not persist failed state query=%s", query_id)
        finally:
            source = self._source_completion.pop(query_id, None)
            if source is not None and not source.done():
                source.set_result(False)
            for pool in self._pools.pop(query_id, {}).values():
                await pool.cancel()
            self._source_tasks.pop(query_id, None)
            self._reason_overrides.pop(query_id, None)
            self._locks.pop(query_id, None)
            completed = self._completion.pop(query_id, None)
            if completed is not None:
                completed.set_result(True)

    async def _run_search(self, tenant_id: str, query_id: str) -> None:
        search = self.store.get_search(tenant_id, query_id)
        if search is None:
            return
        claims = [ClaimContext.model_validate(item) for item in json.loads(search["claims_json"])]
        options = json.loads(search["options_json"])
        library_ids = json.loads(search["requested_library_ids"])
        reasons = self._reason_overrides.get(query_id, self.reasons)
        local_only = options.get("local_only", False)
        models = self.discovery_models if query_id not in self._reason_overrides else None
        if self._scorer is not None and any(
            not self.adapters[identifier].simulation for identifier in library_ids
        ):
            if local_only:
                models = None
            elif models is None and hasattr(reasons, "gateway"):
                models = DiscoveryModels.from_reasons(reasons)
            self._pools[query_id] = {
                claim.claim_id: CandidatePool(
                    claim,
                    self.store,
                    self._scorer,
                    models,
                    self._model_gate,
                    score_review_enabled=self.settings.listwise_score_review,
                )
                for claim in claims
            }
        await self._transition(tenant_id, query_id, "running")
        async with asyncio.TaskGroup() as workers:
            for library_id in library_ids:
                workers.create_task(
                    self._run_library(tenant_id, query_id, claims, library_id, options)
                )
        source = self._source_completion.get(query_id)
        if source is not None:
            source.set_result(query_id not in self._cancel_requested)
        finalization_requested = query_id in self._finalization_requested
        cancelled = query_id in self._cancel_requested and not finalization_requested
        pools = list(self._pools.get(query_id, {}).values())
        if cancelled:
            await asyncio.gather(*(pool.cancel() for pool in pools))
            ranking_ok = False
        else:
            outcomes = await asyncio.gather(
                *(pool.finish() for pool in pools), return_exceptions=True
            )
            ranking_ok = all(outcome is True for outcome in outcomes)
        finalization_requested = query_id in self._finalization_requested
        cancelled = query_id in self._cancel_requested and not finalization_requested
        await self._transition(tenant_id, query_id, "draining")
        await self._locked(query_id, lambda: self.store.fix_finalize_cut(tenant_id, query_id))
        await self._transition(tenant_id, query_id, "finalizing")
        state = self.store.get_state_bundle(tenant_id, query_id)
        workers = [json.loads(row["payload_json"]) for row in (state or {}).get("workers", [])]
        all_completed = bool(workers) and all(
            worker["worker_state"] == "completed" for worker in workers
        )
        if cancelled:
            termination = "cancelled"
        elif finalization_requested and not all_completed:
            termination = "user_requested"
        elif all_completed and not ranking_ok:
            termination = "candidate_rerank_failed"
        elif all_completed:
            termination = "all_completed"
        elif any(worker["worker_state"] == "timed_out" for worker in workers):
            termination = "deadline_reached"
        else:
            termination = "coordinator_failed"
        final_state: SearchState = (
            "cancelled" if cancelled else "completed" if all_completed and ranking_ok else "partial"
        )
        manifest = await self._locked(
            query_id,
            lambda: self.store.finalize_search(tenant_id, query_id, final_state, termination),
        )
        logger.info(
            "search finalized query=%s state=%s snapshot=%s partial=%s",
            query_id,
            final_state,
            manifest["snapshot_id"],
            manifest["is_partial"],
        )

    async def _run_library(
        self,
        tenant_id: str,
        query_id: str,
        claims: list[ClaimContext],
        library_id: str,
        options: dict,
    ) -> None:
        adapter = self.adapters[library_id]
        try:
            library = self.settings.library(library_id)
        except KeyError:
            library = adapter.library
        if query_id in self._pools and not adapter.simulation:
            await self._run_source_library(tenant_id, query_id, claims, library_id, options)
            return
        reasons = self._reason_overrides.get(query_id, self.reasons)
        delay_ms = int((options.get("library_delays_ms") or {}).get(library_id, library.delay_ms))
        deadline = (
            time.monotonic()
            + int(options.get("soft_deadline_ms") or self.settings.default_soft_deadline_ms) / 1000
        )
        now = utc_now_iso()
        worker = WorkerStatus(
            tenant_id=tenant_id,
            query_id=query_id,
            library_id=library_id,
            attempt=1,
            worker_id=f"wrk_{library_id}_{query_id[:10]}",
            lane_id=f"lane_{library_id}",
            index_snapshot=adapter.index_snapshot,
            model_versions=adapter.model_versions,
            worker_state="running",
            started_at=now,
            heartbeat_at=now,
            updated_at=now,
        )
        try:
            await self._worker_event(tenant_id, query_id, worker, "worker_progress")
            seen = 0
            verified = 0
            emitted = 0
            last_sequence = None
            timed_out = False
            cancelled = False
            for claim in claims:
                if query_id in self._cancel_requested:
                    cancelled = True
                    break
                try:
                    candidates = await self._search_adapter(adapter, claim, library_id, deadline)
                except TimeoutError:
                    timed_out = True
                    break
                worker = worker.model_copy(
                    update={
                        "index_snapshot": adapter.index_snapshot,
                        "model_versions": dict(adapter.model_versions),
                    }
                )
                for candidate in candidates:
                    if query_id in self._cancel_requested:
                        cancelled = True
                        break
                    await asyncio.sleep(delay_ms / 1000)
                    if query_id in self._cancel_requested:
                        cancelled = True
                        break
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
                    seen += 1
                    recommendation = build_recommendation(
                        tenant_id=tenant_id,
                        query_id=query_id,
                        claim=claim,
                        candidate=candidate,
                    )
                    verified += 1
                    if not recommendation.citable:
                        reason = None
                        if (
                            candidate.source.source_role == "metadata_api"
                            and candidate.document_type_eligible
                            and bool(candidate.context.strip())
                        ):
                            reason = await reasons.generate(claim, candidate)
                        score, score_version, score_group = candidate_scoring(
                            candidate,
                            reason,
                            provider=getattr(reasons, "provider", "unknown"),
                            supports_alignment=getattr(reasons, "supports_alignment", False),
                        )
                        diagnostic = CandidateDiagnostic(
                            result_id=recommendation.result_id,
                            work_id=recommendation.work_id,
                            paper_id=candidate.passage.paper_id,
                            claim_id=claim.claim_id,
                            title=candidate.source.title,
                            year=candidate.source.year,
                            authors=candidate.source.authors,
                            doi=candidate.source.doi,
                            library_ids=[library_id],
                            publication_status=candidate.retraction_status,
                            related_dois=candidate.source.related_dois,
                            relationship="insufficient",
                            scope_match=recommendation.evidence[0].scope_match,
                            rejection_reasons=[
                                *recommendation.rejection_reasons,
                                *(
                                    ["publication_status_unknown"]
                                    if candidate.source.source_role == "metadata_api"
                                    and candidate.retraction_status == "unknown"
                                    else []
                                ),
                                *(
                                    ["publication_retracted"]
                                    if candidate.retraction_status == "retracted"
                                    else []
                                ),
                            ],
                            reason=reason,
                            evidence_excerpt=candidate.context,
                            candidate_score=score,
                            retrieval_scores=(
                                [
                                    CandidateSourceScore(
                                        library_id=library_id,
                                        score=candidate.display_score,
                                        score_version=candidate.score_version,
                                        score_scale_group=candidate.score_scale_group,
                                    )
                                ]
                                if candidate.source.source_role == "metadata_api"
                                and candidate.score_version
                                and candidate.score_scale_group
                                else []
                            ),
                            score_version=score_version,
                            score_scale_group=score_group,
                            score_status="scoped" if score is not None else "failed",
                            publication_types=candidate.publication_types or [],
                            source_uri=candidate.locator.source_uri,
                            quality_note=(
                                "基于真实题录与摘要的待核查候选；尚无全文证据定位，"
                                "不可引用、不参与合格推荐排名。"
                                if candidate.source.source_role == "metadata_api"
                                else "相关候选或核验失败项；不属于可引用推荐，也不参与排名。"
                            ),
                        )
                        diagnostic = diagnostic.model_copy(
                            update={
                                "retrieval_fingerprint": retrieval_fingerprint(
                                    diagnostic, claim.claim_hash
                                )
                            }
                        )
                        worker = worker.model_copy(
                            update={
                                "candidates_seen": seen,
                                "candidates_verified": verified,
                                "heartbeat_at": utc_now_iso(),
                                "updated_at": utc_now_iso(),
                            }
                        )
                        event = await self._locked(
                            query_id,
                            lambda item=diagnostic, current=worker, claim_hash=claim.claim_hash: (
                                self.store.commit_candidate_diagnostic(
                                    tenant_id,
                                    query_id,
                                    current.model_dump(mode="json"),
                                    item,
                                    claim_hash,
                                )
                            ),
                        )
                        last_sequence = event["sequence"]
                        worker = worker.model_copy(update={"last_emitted_sequence": last_sequence})
                        continue
                    reason = await reasons.generate(claim, candidate)
                    recommendation = recommendation.model_copy(
                        update={"reason": reason, "updated_at": utc_now_iso()}
                    )
                    worker = worker.model_copy(
                        update={
                            "model_versions": {
                                **worker.model_versions,
                                "reason_model": reason.model,
                                "reason_prompt_version": reason.prompt_version,
                            }
                        }
                    )
                    worker = worker.model_copy(
                        update={
                            "candidates_seen": seen,
                            "candidates_verified": verified,
                            "last_emitted_sequence": last_sequence,
                            "emitted_event_count": emitted,
                            "recommendations_emitted": emitted,
                            "heartbeat_at": utc_now_iso(),
                            "updated_at": utc_now_iso(),
                        }
                    )
                    event = await self._locked(
                        query_id,
                        lambda rec=recommendation, current=worker: self.store.commit_recommendation(
                            tenant_id,
                            query_id,
                            current.model_dump(mode="json"),
                            rec.model_dump(mode="json"),
                            uuid.uuid4().hex,
                        ),
                    )
                    if event is None:
                        continue
                    emitted += 1
                    last_sequence = event["sequence"]
                    worker = worker.model_copy(
                        update={
                            "last_emitted_sequence": last_sequence,
                            "local_watermark": last_sequence,
                            "emitted_event_count": emitted,
                            "recommendations_emitted": emitted,
                            "candidates_seen": seen,
                            "candidates_verified": verified,
                            "heartbeat_at": utc_now_iso(),
                            "updated_at": utc_now_iso(),
                        }
                    )
                if timed_out or cancelled:
                    break
            if timed_out:
                worker = worker.model_copy(
                    update={
                        "worker_state": "timed_out",
                        "drained": False,
                        "ended_at": utc_now_iso(),
                        "last_emitted_sequence": last_sequence,
                        "local_watermark": last_sequence,
                        "emitted_event_count": emitted,
                        "recommendations_emitted": emitted,
                        "candidates_seen": seen,
                        "candidates_verified": verified,
                        "updated_at": utc_now_iso(),
                    }
                )
                await self._worker_event(tenant_id, query_id, worker, "worker_timed_out")
            elif cancelled:
                worker = worker.model_copy(
                    update={
                        "worker_state": "cancelled",
                        "drained": True,
                        "ended_at": utc_now_iso(),
                        "last_emitted_sequence": last_sequence,
                        "local_watermark": last_sequence,
                        "emitted_event_count": emitted,
                        "recommendations_emitted": emitted,
                        "candidates_seen": seen,
                        "candidates_verified": verified,
                        "updated_at": utc_now_iso(),
                    }
                )
                await self._worker_event(tenant_id, query_id, worker, "worker_cancelled")
            else:
                worker = worker.model_copy(
                    update={
                        "worker_state": "completed",
                        "drained": True,
                        "ended_at": utc_now_iso(),
                        "last_emitted_sequence": last_sequence,
                        "local_watermark": last_sequence,
                        "emitted_event_count": emitted,
                        "recommendations_emitted": emitted,
                        "candidates_seen": seen,
                        "candidates_verified": verified,
                        "updated_at": utc_now_iso(),
                    }
                )
                await self._worker_event(tenant_id, query_id, worker, "worker_completed")
        except Exception as exc:
            if isinstance(exc, SourceSearchError):
                logger.warning(
                    "source worker failed query=%s library=%s code=%s",
                    query_id,
                    library_id,
                    exc.code,
                )
                error = {"code": exc.code, "message": exc.public_message}
            else:
                logger.exception("worker failed query=%s library=%s", query_id, library_id)
                error = {
                    "code": "INTERNAL_ERROR",
                    "message": f"{type(exc).__name__}: library worker failed",
                }
            failed = worker.model_copy(
                update={
                    "worker_state": "failed",
                    "drained": False,
                    "ended_at": utc_now_iso(),
                    "last_error": error,
                    "updated_at": utc_now_iso(),
                }
            )
            try:
                await self._worker_event(tenant_id, query_id, failed, "worker_failed")
            except Exception:
                logger.exception(
                    "could not persist worker failure query=%s library=%s", query_id, library_id
                )

    async def _run_source_library(
        self,
        tenant_id: str,
        query_id: str,
        claims: list[ClaimContext],
        library_id: str,
        options: dict,
    ) -> None:
        adapter = self.adapters[library_id]
        local_only = options.get("local_only", False)
        now = utc_now_iso()
        worker = WorkerStatus(
            tenant_id=tenant_id,
            query_id=query_id,
            library_id=library_id,
            attempt=1,
            worker_id=f"wrk_{library_id}_{query_id[:10]}",
            lane_id=f"lane_{library_id}",
            index_snapshot=adapter.index_snapshot,
            model_versions=dict(adapter.model_versions),
            worker_state="running",
            source_agent_status="disabled" if local_only else "running",
            started_at=now,
            heartbeat_at=now,
            updated_at=now,
        )
        deadline = time.monotonic() + options["soft_deadline_ms"] / 1000
        seen: set[str] = set()
        submitted: set[str] = set()
        agent = None
        try:
            await self._worker_event(tenant_id, query_id, worker, "worker_progress")
            for claim in claims:
                pool = self._pools[query_id][claim.claim_id]
                if not local_only and pool.models is None:
                    raise SourceSearchError("MODEL_NOT_CONFIGURED", "来源子 Agent 未配置模型。")

                async def search(recall):
                    nonlocal worker
                    if query_id in self._cancel_requested:
                        raise asyncio.CancelledError
                    candidates = await self._search_adapter(adapter, recall, library_id, deadline)
                    seen.update(
                        f"{item.passage.paper_id}:{sha256_json(item.context)}"
                        for item in candidates
                    )
                    worker = worker.model_copy(
                        update={
                            "candidates_seen": len(seen),
                            "index_snapshot": adapter.index_snapshot,
                            "model_versions": dict(adapter.model_versions),
                            "heartbeat_at": utc_now_iso(),
                            "updated_at": utc_now_iso(),
                        }
                    )
                    await self._worker_event(tenant_id, query_id, worker, "worker_progress")
                    return candidates

                async def submit(candidate, recall, target_pool=pool):
                    nonlocal worker
                    if query_id in self._cancel_requested:
                        raise asyncio.CancelledError
                    delay = options["library_delays_ms"].get(library_id, 0)
                    if delay:
                        await asyncio.sleep(delay / 1000)
                    await target_pool.submit(candidate, recall)
                    submitted.add(f"{candidate.passage.paper_id}:{sha256_json(candidate.context)}")
                    worker = worker.model_copy(
                        update={
                            "candidates_verified": len(submitted),
                            "source_candidates_submitted": len(submitted),
                            "heartbeat_at": utc_now_iso(),
                            "updated_at": utc_now_iso(),
                        }
                    )
                    await self._worker_event(tenant_id, query_id, worker, "worker_progress")

                if local_only:
                    for candidate in await search(claim):
                        await submit(candidate, claim)
                    continue
                initial_error = None
                try:
                    initial = await search(claim)
                except SourceSearchError as exc:
                    initial = []
                    initial_error = {"code": exc.code, "message": exc.public_message}
                if query_id in self._cancel_requested:
                    raise asyncio.CancelledError
                agent = SourceAgent(pool.models.source_model, self.settings, self._model_gate)
                task = asyncio.create_task(
                    agent.run(claim, library_id, initial, search, submit, initial_error)
                )
                self._source_tasks[query_id].add(task)
                try:
                    trace = await asyncio.wait_for(
                        task, timeout=max(0.001, deadline - time.monotonic())
                    )
                finally:
                    self._source_tasks[query_id].discard(task)
                worker = worker.model_copy(
                    update={
                        "source_actions": [*worker.source_actions, *trace["actions"]],
                        "source_decision": trace["decision"],
                        "source_available_work_ids": trace["available_work_ids"],
                        "last_error": trace["last_source_error"],
                        "model_versions": {
                            **worker.model_versions,
                            "source_agent_prompt": trace["prompt_version"],
                            "source_agent_model": pool.models.model_name,
                        },
                    }
                )
            worker = worker.model_copy(
                update={
                    "worker_state": "failed" if worker.last_error else "completed",
                    "source_agent_status": "completed",
                    "drained": worker.last_error is None,
                }
            )
            event_type = "worker_failed" if worker.last_error else "worker_completed"
        except asyncio.CancelledError:
            worker = worker.model_copy(
                update={
                    "worker_state": "cancelled",
                    "source_agent_status": "failed",
                    "drained": True,
                }
            )
            event_type = "worker_cancelled"
        except TimeoutError:
            worker = worker.model_copy(
                update={
                    "worker_state": "timed_out",
                    "source_agent_status": "failed",
                    "last_error": {
                        "code": "SOURCE_AGENT_TIMEOUT",
                        "message": "来源子 Agent 超时；已提交候选保留。",
                    },
                }
            )
            event_type = "worker_timed_out"
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, SourceSearchError)
                else "MODEL_INSUFFICIENT_BALANCE"
                if getattr(exc, "status_code", None) == 402
                else "SOURCE_AGENT_FAILED"
            )
            logger.warning("source agent failed library=%s type=%s", library_id, type(exc).__name__)
            worker = worker.model_copy(
                update={
                    "worker_state": "failed",
                    "source_agent_status": "unavailable"
                    if code == "MODEL_NOT_CONFIGURED"
                    else "failed",
                    "last_error": {
                        "code": code,
                        "message": "来源子 Agent 未完成；已提交候选保留。",
                    },
                }
            )
            event_type = "worker_failed"
        if agent is not None and worker.source_agent_status != "completed":
            worker = worker.model_copy(
                update={
                    "source_actions": list(agent.actions),
                    "source_available_work_ids": sorted(agent.available_work_ids),
                }
            )
            if agent.recall_preserved_count:
                error = dict(worker.last_error or {})
                details = dict(error.get("details") or {})
                details["recall_preservation"] = {
                    "policy_version": SOURCE_RECALL_PRESERVATION_VERSION,
                    "submitted": agent.recall_preserved_count,
                    "agent_approved": False,
                }
                worker = worker.model_copy(
                    update={
                        "last_error": {**error, "details": details} if worker.last_error else None,
                        "source_decision": (
                            f"来源决策失败；保留 {agent.recall_preserved_count} 条可靠召回候选，"
                            "未经来源 Agent 审批，仍由中央模型排序。"
                        ),
                        "model_versions": {
                            **worker.model_versions,
                            "source_recall_preservation": SOURCE_RECALL_PRESERVATION_VERSION,
                        },
                    }
                )
        worker = worker.model_copy(update={
            "ended_at": utc_now_iso(), "updated_at": utc_now_iso(),
            **({"source_agent_status": "disabled"} if local_only else {}),
        })
        await self._worker_event(tenant_id, query_id, worker, event_type)

    async def _search_adapter(
        self, adapter: LibraryAdapter, claim: ClaimContext, library_id: str, deadline: float
    ):
        return await self.run_library_operation(library_id, deadline, lambda: adapter.search(claim))

    async def run_library_operation(
        self, library_id: str, deadline: float, operation: Callable[[], _LibraryResult]
    ) -> _LibraryResult:
        lock = self._adapter_locks[library_id]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        await asyncio.wait_for(lock.acquire(), timeout=remaining)
        search_task = asyncio.create_task(asyncio.to_thread(operation))
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            return await asyncio.wait_for(asyncio.shield(search_task), timeout=remaining)
        finally:
            if search_task.done():
                self._release_adapter_lock(lock, search_task)
            else:
                search_task.add_done_callback(lambda task: self._release_adapter_lock(lock, task))

    @staticmethod
    def _release_adapter_lock(lock: asyncio.Lock, task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()
        lock.release()

    async def _transition(self, tenant_id: str, query_id: str, state: str) -> None:
        await self._locked(
            query_id, lambda: self.store.append_state_event(tenant_id, query_id, state)
        )

    async def _worker_event(
        self, tenant_id: str, query_id: str, worker: WorkerStatus, event_type: str
    ) -> dict:
        return await self._locked(
            query_id,
            lambda: self.store.commit_worker_event(
                tenant_id, query_id, worker.model_dump(mode="json"), event_type
            ),
        )

    async def _locked(self, query_id: str, action: Callable[[], dict]) -> dict:
        async with self._locks[query_id]:
            return action()
