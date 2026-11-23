"""Transport-independent search service shared by HTTP and LangChain tools."""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from collections.abc import Mapping

from .accounts import is_account_tenant
from .candidates import candidate_sort_key, merge_candidates
from .config import LOCAL_ONLY_ADAPTER_MODE, LOCAL_ONLY_NOTICE, Settings
from .coordinator import SearchCoordinator
from .entities import (
    CandidateDiagnostic,
    ClaimContext,
    ClaimCoverage,
    Coverage,
    EvaluationAnchor,
    EvidenceScope,
    GroupEntry,
    LibraryInfo,
    Recommendation,
    ResultGroup,
    SearchCreate,
    SearchCreated,
    SearchSession,
    SearchStateResponse,
    WorkerStatus,
    stable_id,
    utc_now_iso,
)
from .evidence import evidence_completeness, scope_applicability
from .jcs import sha256_json
from .listwise import DiscoveryModels
from .manuscripts import PARSE_READY, ManuscriptError, ManuscriptService
from .projects import ProjectStore
from .reasons import ReasonGenerator
from .retrieval import DEMO_SCOPE, LibraryAdapter
from .store import claim_coverage_from_workers, coverage_from_workers

GROUP_ORDER = {"ssg:fixture-bootstrap-v1": 0, "evidence_only": 1}
TENANT_NOTE = "X-Tenant-ID 仅用于本地数据分区，不是认证或授权机制；缺省租户为 local。"


class SearchRequestError(ValueError):
    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


def _library_notice(library) -> str:
    if library.source_type == "fixture":
        return "本地 fixture 模拟库，不是真实论文库。"
    if library.source_type == "remote_api":
        if library.library_id != "europepmc-online":
            return "公开题录与摘要在线检索；候选不可直接引用。"
        return (
            "实时查询 Europe PMC，原生相关性 TopN 后本地重排；不是本地全库索引，候选不可直接引用。"
        )
    count = library.record_count if library.record_count is not None else "未知"
    return f"本地固定快照，{count} 篇真实题录，有可用摘要时一并提供；候选不可直接引用。"


def library_info(library) -> LibraryInfo:
    return LibraryInfo(
        library_id=library.library_id,
        display_name=library.display_name,
        adapter_id=library.adapter_id,
        simulation=library.simulation,
        default_delay_ms=library.delay_ms,
        notice=_library_notice(library),
        source_type=library.source_type,
        record_count=library.record_count,
    )


def library_infos(settings: Settings) -> list[LibraryInfo]:
    return [library_info(library) for library in settings.libraries]


def result_sort_key(recommendation: Recommendation) -> tuple:
    return (
        -(recommendation.display_score or 0),
        -evidence_completeness(recommendation.evidence),
        -scope_applicability(recommendation.evidence),
        recommendation.result_id,
    )


def result_groups(results: list[Recommendation]) -> list[ResultGroup]:
    buckets: dict[tuple[str, str], list[Recommendation]] = defaultdict(list)
    for recommendation in results:
        if recommendation.citable:
            group = recommendation.rankable_group or "evidence_only"
            buckets[(recommendation.claim.claim_id, group)].append(recommendation)
    groups: list[ResultGroup] = []
    for (claim_id, group), items in sorted(
        buckets.items(),
        key=lambda entry: (entry[0][0], GROUP_ORDER.get(entry[0][1], 9), entry[0][1]),
    ):
        items.sort(key=result_sort_key)
        groups.append(
            ResultGroup(
                claim_id=claim_id,
                rankable_group=group,
                entries=[
                    GroupEntry(result_id=item.result_id, rank=index)
                    for index, item in enumerate(items, start=1)
                ],
            )
        )
    return groups


def to_session(search: dict, workers: list[WorkerStatus]) -> SearchSession:
    return SearchSession(
        tenant_id=search["tenant_id"],
        query_id=search["query_id"],
        input_type=search["input_type"],
        search_state=search["search_state"],
        requested_library_ids=json.loads(search["requested_library_ids"]),
        planning_mode=search["planning_mode"],
        claim_count=search["claim_count"],
        claim_coverage=ClaimCoverage(
            **claim_coverage_from_workers(
                search["claim_count"], [w.model_dump(mode="json") for w in workers]
            )
        ),
        applied_through_sequence=search["applied_through_sequence"],
        finalize_cut=search["finalize_cut"],
        event_set_hash=search["event_set_hash"],
        latest_checkpoint_id=search["latest_checkpoint_id"],
        final_snapshot_id=search["final_snapshot_id"],
        created_at=search["created_at"],
        updated_at=search["updated_at"],
    )


class SearchService:
    def __init__(
        self,
        store,
        settings: Settings,
        adapters: Mapping[str, LibraryAdapter],
        reasons: ReasonGenerator,
        *,
        reason_provider: str | None,
        discovery_models: DiscoveryModels | None = None,
        manuscripts: ManuscriptService | None = None,
        projects: ProjectStore | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.projects = projects
        self.adapters = dict(adapters)
        self.library_configs = {item.library_id: item for item in settings.libraries}
        self.library_configs.update(
            {
                key: adapter.library
                for key, adapter in adapters.items()
                if key not in self.library_configs
            }
        )
        self.reason_provider = reason_provider
        self.manuscripts = manuscripts
        self.purging_documents: set[tuple[str, str]] = set()
        self.coordinator = SearchCoordinator(
            store, settings, self.adapters, reasons, discovery_models
        )
        self.simulation = all(
            self.adapters[item.library_id].simulation for item in settings.libraries
        )
        self.adapter_mode = (
            "simulation_fixture" if self.simulation else "real_abstract_metadata"
        ) + ("_live_llm" if reason_provider else "_model_unavailable")
        scope_note = (
            "测试配置对未提供 claim_scope 的请求使用 fixture 主题 scope"
            "（仅演示，不代表真实实验范围）；"
            if settings.fixture_demo_scope_default
            else "未提供 claim_scope 时 scope 为 unknown，结果不会成为可引用推荐；"
        )
        model_note = (
            f"推荐理由由 {reason_provider} 实际调用生成，但不是独立关系核验；"
            if reason_provider
            else "未配置模型，不能生成推荐理由；"
        )
        if self.simulation:
            self.notice = (
                f"M0 演示：文献、分数和覆盖来自本地 fixture，{model_note}"
                "不能据此判断真实检索或引用质量。"
            ) + scope_note
        else:
            self.notice = (
                f"真实题录与摘要候选发现：{model_note}"
                "这些摘要候选尚无全文证据定位，不可直接引用；按需原文引用建议是独立资源，须作者核对。"
            ) + scope_note

    def demo_library_ids(self, tenant_id: str, selected: list[str] | None = None) -> list[str]:
        if (
            not self.settings.demo_continuity
            or self.settings.candidate_pipeline != "agentic"
            or is_account_tenant(tenant_id)
        ):
            return []
        local = {
            library.library_id
            for library in self.settings.libraries
            if library.source_type == "local_snapshot"
            and not self.adapters[library.library_id].simulation
        }
        requested = selected if selected is not None else list(self.library_configs)
        return [library_id for library_id in requested if library_id in local]

    def create(
        self,
        tenant_id: str,
        body: SearchCreate,
        idempotency_key: str | None,
        *,
        reason_override: ReasonGenerator | None = None,
        evaluation_anchor: EvaluationAnchor | None = None,
        workspace_id: str | None = None,
        agent_run_id: str | None = None,
        local_only: bool = False,
    ) -> tuple[SearchCreated, bool]:
        public_ids = tuple(item.library_id for item in self.settings.libraries)
        available = public_ids
        if workspace_id is not None and self.projects is not None:
            available = self.projects.get(tenant_id, workspace_id).available_library_ids
        library_ids = body.library_ids if body.library_ids is not None else list(public_ids)
        if not library_ids:
            raise SearchRequestError("library_ids 不能为空数组。")
        if len(library_ids) != len(set(library_ids)):
            raise SearchRequestError("library_ids 不允许重复。")
        unknown = [item for item in library_ids if item not in available]
        if unknown:
            raise SearchRequestError("请求包含未知 library_id。", details={"library_ids": unknown})
        if local_only:
            library_ids = self.demo_library_ids(tenant_id, library_ids)
            if not library_ids or body.input_type == "full_text" or workspace_id is not None:
                raise SearchRequestError("本地备用检索仅支持访客已选的本地题录库、关键词或摘要。")
            local_options = body.options.model_copy(update={"library_delays_ms": {
                key: delay for key, delay in body.options.library_delays_ms.items()
                if key in library_ids
            }})
            body = body.model_copy(update={"library_ids": library_ids, "options": local_options})
        simulation = all(self.adapters[key].simulation for key in library_ids)
        now = utc_now_iso()
        query_id = f"q_{uuid.uuid4().hex}"
        if (
            body.input_type == "full_text"
            and body.manuscript_id
            and (tenant_id, body.manuscript_id) in self.purging_documents
        ):
            raise SearchRequestError(
                "稿件正在删除，暂不可检索。",
                details={"code": "MANUSCRIPT_PURGE_IN_PROGRESS"},
            )
        manuscript = self._resolve_manuscript(tenant_id, body)
        effective_anchor = (
            _fulltext_evaluation_anchor(evaluation_anchor, body, manuscript)
            if manuscript is not None
            else evaluation_anchor
        )
        claim, scope_source = _build_claim(
            tenant_id,
            query_id,
            body,
            now,
            DEMO_SCOPE if simulation and self.settings.fixture_demo_scope_default else None,
            effective_anchor,
            manuscript,
        )
        resolved_delays = {key: self.library_configs[key].delay_ms for key in library_ids}
        for library_id, delay in body.options.library_delays_ms.items():
            if library_id not in library_ids:
                raise SearchRequestError(
                    "library_delays_ms 只能覆盖本次请求的库。", details={"library_id": library_id}
                )
            if not 0 <= delay <= self.settings.max_library_delay_ms:
                raise SearchRequestError(
                    "library delay 超出允许范围。", details={"library_id": library_id}
                )
            resolved_delays[library_id] = delay
        soft_deadline = body.options.soft_deadline_ms or self.settings.default_soft_deadline_ms
        if not 100 <= soft_deadline <= self.settings.max_soft_deadline_ms:
            raise SearchRequestError("soft_deadline_ms 超出允许范围。")
        options = {
            "library_delays_ms": resolved_delays,
            "soft_deadline_ms": soft_deadline,
            "scope_source": scope_source,
            **({"local_only": True} if local_only else {}),
        }
        notice = self.notice
        if local_only:
            notice = LOCAL_ONLY_NOTICE
        elif body.input_type == "full_text":
            notice = (
                "本查询以用户私有稿件的指定段落为 Claim；检索结果只是该段落的候选证据，"
                "不等于全文科学核验或可引用结论。"
            )
        created = SearchCreated(
            query_id=query_id,
            search_state="created",
            claims=[claim],
            events_url=f"/api/v1/searches/{query_id}/events",
            adapter_mode=LOCAL_ONLY_ADAPTER_MODE if local_only else self.adapter_mode,
            scope_source=scope_source,
            simulation=simulation,
            notice=notice,
        )
        workers = [
            WorkerStatus(
                tenant_id=tenant_id,
                query_id=query_id,
                library_id=library_id,
                attempt=1,
                worker_id=f"wrk_{library_id}_{query_id[:10]}",
                lane_id=f"lane_{library_id}",
                index_snapshot=self.adapters[library_id].index_snapshot,
                model_versions=dict(self.adapters[library_id].model_versions),
                worker_state="pending",
                updated_at=now,
            ).model_dump(mode="json")
            for library_id in library_ids
        ]
        record = {
            "tenant_id": tenant_id,
            "query_id": query_id,
            "input_type": body.input_type,
            "search_state": "created",
            "planning_mode": "agent_tool",
            "requested_library_ids": library_ids,
            "claims": [claim.model_dump(mode="json")],
            "options": options,
            "request": body.model_dump(mode="json"),
            "request_hash": sha256_json({
                "tenant_id": tenant_id,
                "request": body.model_dump(mode="json"),
                **({"local_only": True} if local_only else {}),
            }),
            "idempotency_key": idempotency_key or None,
            "create_response": created.model_dump(mode="json"),
            "claim_count": 1,
            "created_at": now,
        }
        row, replayed = self.store.create_search(record, workers, agent_run_id=agent_run_id)
        if not replayed:
            if reason_override is not None:
                self.coordinator.set_reason_override(query_id, reason_override)
            self.coordinator.start(tenant_id, query_id)
        else:
            created = SearchCreated.model_validate_json(row["create_response_json"])
        return created, replayed

    def _resolve_manuscript(self, tenant_id: str, body: SearchCreate) -> dict | None:
        if body.input_type != "full_text":
            return None
        if body.manuscript_passage_index is None:
            raise SearchRequestError(
                "全文检索必须明确选择 manuscript_passage_index。",
                details={"field": "manuscript_passage_index"},
            )
        if self.manuscripts is None:
            raise SearchRequestError(
                "未配置稿件服务，不能执行全文检索。",
                details={"code": "MANUSCRIPT_SERVICE_UNAVAILABLE"},
            )
        manuscript_id = body.manuscript_id
        assert manuscript_id is not None
        try:
            record = self.manuscripts.get(tenant_id, manuscript_id, catalog_limit=0)
        except ManuscriptError as exc:
            raise SearchRequestError(
                exc.message,
                details={"code": exc.code, **exc.details},
            ) from exc
        if record is None:
            raise SearchRequestError(
                "稿件不存在或不属于当前租户。",
                details={"code": "MANUSCRIPT_NOT_FOUND"},
            )
        if record["parse_status"] != PARSE_READY:
            error = record.get("error") or {}
            raise SearchRequestError(
                "稿件尚未解析完成，不能检索正文段落。",
                details={
                    "code": error.get("code") or "MANUSCRIPT_NOT_READY",
                    "parse_status": record["parse_status"],
                },
            )
        try:
            payload = self.manuscripts.passages(
                tenant_id,
                manuscript_id,
                offset=body.manuscript_passage_index,
                limit=1,
            )
        except ManuscriptError as exc:
            raise SearchRequestError(
                exc.message,
                details={"code": exc.code, **exc.details},
            ) from exc
        passages = payload.get("passages") or []
        if not passages:
            raise SearchRequestError(
                "指定段落不存在。",
                details={
                    "code": "MANUSCRIPT_PASSAGE_NOT_FOUND",
                    "passage_index": body.manuscript_passage_index,
                },
            )
        passage = passages[0]
        if int(passage["passage_index"]) != body.manuscript_passage_index:
            raise SearchRequestError(
                "指定段落与稿件目录不一致。",
                details={
                    "code": "MANUSCRIPT_PASSAGE_MISMATCH",
                    "passage_index": body.manuscript_passage_index,
                },
            )
        return {"record": record, "passage": passage}

    def state(self, tenant_id: str, query_id: str) -> SearchStateResponse | None:
        bundle = self.store.get_state_bundle(tenant_id, query_id)
        if bundle is None:
            return None
        search = bundle["search"]
        results = [
            Recommendation.model_validate_json(row["payload_json"])
            for row in bundle["recommendations"]
        ]
        workers = [
            WorkerStatus.model_validate_json(row["payload_json"]) for row in bundle["workers"]
        ]
        worker_payloads = [worker.model_dump(mode="json") for worker in workers]
        session = to_session(search, workers)
        if (
            session.finalize_cut is not None
            and session.applied_through_sequence < session.finalize_cut
        ):
            raise RuntimeError("提交水位不满足 finalize_cut 不变量。")
        requested = set(session.requested_library_ids)
        options = json.loads(search["options_json"])
        local_only = options.get("local_only", False)
        diagnostics: dict[str, CandidateDiagnostic] = {}
        claim_hashes = {
            claim.claim_id: claim.claim_hash
            for claim in (
                ClaimContext.model_validate(item) for item in json.loads(search["claims_json"])
            )
        }
        for payload in bundle["candidates"]:
            item = CandidateDiagnostic.model_validate(payload)
            previous = diagnostics.get(item.result_id)
            if previous is not None:
                item = merge_candidates(previous, item, claim_hashes[item.claim_id])
            diagnostics[item.result_id] = item
        return SearchStateResponse(
            session=session,
            results=results,
            candidate_diagnostics=sorted(diagnostics.values(), key=candidate_sort_key),
            candidate_rankings=bundle["candidate_rankings"],
            groups=result_groups(results),
            workers=workers,
            coverage=Coverage(**coverage_from_workers(worker_payloads)),
            as_of_sequence=search["applied_through_sequence"],
            latest_sequence=search["latest_sequence"],
            is_live=search["search_state"] not in {"completed", "partial", "failed", "cancelled"},
            adapter_mode=LOCAL_ONLY_ADAPTER_MODE if local_only else self.adapter_mode,
            scope_source=options.get("scope_source", "unknown"),
            simulation_notice=LOCAL_ONLY_NOTICE if local_only else self.notice,
            tenant_note=TENANT_NOTE,
            libraries=[
                item for item in library_infos(self.settings) if item.library_id in requested
            ],
        )

    async def tool_result(
        self,
        tenant_id: str,
        created: SearchCreated,
        *,
        reason_provider: str | None = None,
        sources_first: bool = False,
    ) -> dict:
        timeout_s = self.settings.agent_timeout_ms / 1000
        sources_ready = False
        if sources_first and self.settings.agent_source_evidence_first:
            sources_ready = await self.coordinator.wait_sources(
                created.query_id, timeout_s=timeout_s
            )
        else:
            await self.coordinator.wait(created.query_id, timeout_s=timeout_s)
        state = self.state(tenant_id, created.query_id)
        if state is None:
            raise LookupError("search disappeared before tool result")
        ranks = {entry.result_id: entry.rank for group in state.groups for entry in group.entries}
        results = sorted(
            (item for item in state.results if item.citable),
            key=lambda item: (
                item.claim.claim_id,
                item.rankable_group or "evidence_only",
                ranks.get(item.result_id, 10_000),
                item.result_id,
            ),
        )[:20]
        return {
            "query_id": created.query_id,
            "search_state": state.session.search_state,
            "complete": not state.is_live,
            **(
                {
                    "delivery_stage": "sources_ready",
                    "delivery_note": (
                        "来源提交已结算（失败或超时见覆盖）；榜单尚未定稿。"
                        "可直接读同篇题名摘要判断证据，不把暂态名次或未生成理由当成无关。"
                        "按实际覆盖决定是否补查，不因排序未结束重搜。"
                    ),
                }
                if sources_ready and state.is_live
                else {}
            ),
            "coverage": state.coverage.model_dump(mode="json"),
            "groups": [group.model_dump(mode="json") for group in state.groups],
            "results": [item.model_dump(mode="json") for item in results],
            "candidate_diagnostics": [
                item.model_dump(mode="json") for item in state.candidate_diagnostics
            ],
            "candidate_rankings": [
                item.model_dump(mode="json") for item in state.candidate_rankings
            ],
            "workers": [item.model_dump(mode="json") for item in state.workers],
            "diagnostic_note": (
                "candidate_diagnostics 可直接用于候选发现回答，也可用于判断补查方向；"
                "它们不可作为已核验全文引用、不参与合格推荐排名，也不进入推荐快照。"
            ),
            "simulation": created.simulation,
            "reason_model_configured": (reason_provider or self.reason_provider) is not None,
            "notice": created.notice,
            "scope_source": state.scope_source,
            "final_snapshot_id": state.session.final_snapshot_id,
        }


MAX_FULLTEXT_ANCHOR_CHARS = 7000
FULLTEXT_ANCHOR_PREFIX = "[较早会话需求已按上下文预算省略]\n"


def _fulltext_evaluation_anchor(
    previous: EvaluationAnchor | None,
    body: SearchCreate,
    manuscript: dict,
) -> EvaluationAnchor:
    passage = manuscript["passage"]
    passage_text = str(passage.get("text") or "").strip()
    if previous is None or not previous.claim_text.strip():
        claim_text = passage_text
    else:
        claim_text = (
            "[会话研究需求与修订]\n"
            + previous.claim_text.strip()
            + "\n[本次所选真实段落]\n"
            + passage_text
        )
    if len(claim_text) > MAX_FULLTEXT_ANCHOR_CHARS:
        keep = MAX_FULLTEXT_ANCHOR_CHARS - len(FULLTEXT_ANCHOR_PREFIX)
        claim_text = FULLTEXT_ANCHOR_PREFIX + claim_text[-keep:]
    scope = (
        (previous.scope if previous is not None else None) or body.claim_scope or EvidenceScope()
    )
    return EvaluationAnchor(claim_text=claim_text, scope=scope)


def _build_claim(
    tenant_id: str,
    query_id: str,
    body: SearchCreate,
    now: str,
    default_scope: EvidenceScope | None,
    evaluation_anchor: EvaluationAnchor | None = None,
    manuscript: dict | None = None,
) -> tuple[ClaimContext, str]:
    words: list[str] = []
    manuscript_anchor: dict | None = None
    if body.input_type == "full_text":
        if manuscript is None:
            raise SearchRequestError(
                "全文检索必须指定稿件段落。",
                details={"code": "MANUSCRIPT_PASSAGE_REQUIRED"},
            )
        passage = manuscript["passage"]
        record = manuscript["record"]
        claim_text = str(passage.get("text") or "").strip()
        if not claim_text:
            raise SearchRequestError(
                "指定段落没有可检索文本。",
                details={
                    "code": "MANUSCRIPT_PASSAGE_EMPTY",
                    "passage_index": passage.get("passage_index"),
                },
            )
        topic = [word.strip() for word in (body.topic_context or []) if word.strip()]
        words = topic
        anchor_hash = sha256_json(
            {
                "manuscript_id": body.manuscript_id,
                "content_sha256": record["sha256"],
                "passage_index": passage["passage_index"],
                "text_sha256": passage["text_sha256"],
            }
        )
        claim_type = "discussion"
        manuscript_anchor = {
            "manuscript_id": body.manuscript_id,
            "content_sha256": record["sha256"],
            "passage_index": int(passage["passage_index"]),
            "page": passage.get("page"),
            "paragraph_index": passage.get("paragraph_index"),
            "section_path": list(passage.get("section_path") or []),
            "char_start": int(passage["char_start"]),
            "char_end": int(passage["char_end"]),
            "text_sha256": passage["text_sha256"],
            "source_method": passage.get("source_method"),
            "quote": claim_text,
        }
    elif body.input_type == "keywords":
        words = [word.strip() for word in (body.keywords or []) if word.strip()]
        topic = [word.strip() for word in (body.topic_context or []) if word.strip()] or words
        claim_text = " ".join(topic)
        anchor_hash = sha256_json({"topic_context": topic})
        claim_type = "topic"
    else:
        claim_text = (body.abstract or "").strip()
        topic = [word.strip() for word in (body.topic_context or []) if word.strip()]
        anchor_hash = sha256_json({"abstract": claim_text, "topic_context": topic})
        claim_type = "discussion"
    if manuscript_anchor is not None:
        claim_rule_version = "claim-rule-v3-manuscript-passage"
    elif evaluation_anchor is not None:
        claim_rule_version = "claim-rule-v2-evaluation-anchor"
    else:
        claim_rule_version = "claim-rule-v1"
    claim_id = stable_id("clm_", tenant_id, query_id, anchor_hash, claim_rule_version)
    if body.claim_scope is not None:
        scope, scope_source = body.claim_scope, "request"
    elif default_scope is not None:
        scope, scope_source = default_scope, "m0_fixture_demo_default"
    else:
        scope, scope_source = EvidenceScope(), "unknown"
    claim_payload = {
        "claim_text": claim_text,
        "topic_context": topic,
        "scope": scope.model_dump(),
        "input_type": body.input_type,
        "claim_type": claim_type,
    }
    if evaluation_anchor is not None:
        claim_payload["claim_rule_version"] = claim_rule_version
        claim_payload["evaluation_anchor"] = evaluation_anchor.model_dump()
    if manuscript_anchor is not None:
        claim_payload["manuscript_anchor"] = manuscript_anchor
    claim_hash = sha256_json(claim_payload)
    return ClaimContext(
        tenant_id=tenant_id,
        claim_id=claim_id,
        query_id=query_id,
        anchor_hash=anchor_hash,
        claim_rule_version=claim_rule_version,
        input_type=body.input_type,
        claim_type=claim_type,
        claim_text=claim_text,
        topic_context=topic,
        query_terms=words,
        manuscript_anchor=manuscript_anchor,
        scope=scope,
        evaluation_anchor=evaluation_anchor,
        claim_index=0,
        claim_hash=claim_hash,
        created_at=now,
    ), scope_source
