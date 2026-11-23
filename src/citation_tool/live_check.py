"""One opt-in acceptance probe using the service's real default model and public data."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

import httpx
from fastapi import FastAPI

from .config import LOCAL_ONLY_ADAPTER_MODE, Settings
from .entities import AgentRun, CandidateDiagnostic, SearchStateResponse

SOURCES = {
    "europepmc-online": "remote_api",
    "europepmc-snapshot": "local_snapshot",
    "abiotech-sample": "local_snapshot",
}
DEMO_DOI = "10.1007/s42994-024-00192-2"


class LiveCheckError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LiveEvidence:
    configured_model: str
    agent_model: str
    ranking_model: str
    sources_used: tuple[str, ...]
    ranked_count: int
    target_rank: int


def check_profile(settings: Settings) -> None:
    if settings.library_mode != "metadata":
        raise LiveCheckError("当前是模拟库；请取消 CITATION_LIBRARY_MODE=fixture。")
    if settings.candidate_pipeline != "agentic":
        raise LiveCheckError("当前未启用列表重排；请取消 CITATION_CANDIDATE_PIPELINE=pointwise。")
    if not settings.online_enabled:
        raise LiveCheckError("Europe PMC 在线库未开启；请取消 CITATION_EUROPEPMC_ONLINE=0。")
    if settings.auth_required:
        raise LiveCheckError("此检查针对匿名演示；当前仍要求访问令牌。")


def _error_code(error: dict | None) -> str:
    code = (error or {}).get("code", "")
    return f" ({code})" if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,72}", code) else ""


def _response_body(response: httpx.Response, stage: str) -> dict:
    try:
        data = response.json()
    except ValueError:
        data = None
    if not response.is_success:
        code = _error_code(data if isinstance(data, dict) else None)
        raise LiveCheckError(f"{stage}：HTTP {response.status_code}{code}")
    if not isinstance(data, dict):
        raise LiveCheckError(f"{stage}：服务未返回有效回执。")
    return data


def _verified_ranking(state: SearchStateResponse) -> tuple[CandidateDiagnostic, int]:
    if state.adapter_mode == LOCAL_ONLY_ADAPTER_MODE or any(
        lib.simulation for lib in state.libraries
    ):
        raise LiveCheckError("本轮进入模拟或仅本地备用模式，没有完整的真实 Agent 检索。")
    online = next((w for w in state.workers if w.library_id == "europepmc-online"), None)
    if online is None:
        raise LiveCheckError("本轮 Agent 未实际查询 Europe PMC 在线库。")
    if online.worker_state != "completed" or online.source_agent_status != "completed":
        raise LiveCheckError(f"Europe PMC 在线检索未完成{_error_code(online.last_error)}。")
    if online.candidates_seen == 0:
        raise LiveCheckError("Europe PMC 在线检索没有召回候选。")
    if state.session.search_state != "completed":
        ranking_failure = next(
            (
                r.failure_code or (r.score_review and r.score_review.failure_code)
                for r in state.candidate_rankings
                if r.failure_code or (r.score_review and r.score_review.failure_code)
            ),
            None,
        )
        code = _error_code({"code": ranking_failure}) or next(
            (_error_code(w.last_error) for w in state.workers if _error_code(w.last_error)), ""
        )
        raise LiveCheckError(f"检索或列表重排未完整结束{code}。")
    target = next((row for row in state.candidate_diagnostics if row.doi == DEMO_DOI), None)
    if target is None:
        raise LiveCheckError(f"未检索到公开验收样例 DOI {DEMO_DOI}。")
    ranking = next((r for r in state.candidate_rankings if r.claim_id == target.claim_id), None)
    candidates = {
        row.work_id: row
        for row in state.candidate_diagnostics
        if row.claim_id == target.claim_id and row.ranking_status != "ineligible"
    }
    if (
        ranking is None
        or ranking.status != "completed"
        or len(ranking.ordered_work_ids) != len(candidates)
        or set(ranking.ordered_work_ids) != set(candidates)
        or target.work_id not in candidates
    ):
        raise LiveCheckError("没有覆盖实际候选池的完整 LLM 榜单。")
    for rank, work_id in enumerate(ranking.ordered_work_ids, 1):
        item = candidates[work_id]
        reason = item.reason
        quote = item.reason_evidence_quote
        if (
            item.ranking_status != "ranked"
            or item.llm_rank != rank
            or item.pool_version != ranking.pool_version
            or item.ranking_version != ranking.ranking_version
            or reason is None
            or reason.status != "generated"
            or not (reason.text or "").strip()
            or not (reason.served_model or "").strip()
            or not quote
            or not (quote in (item.title or "") or quote in item.evidence_excerpt)
        ):
            raise LiveCheckError("榜单没有实际模型名次或与题名/摘要原文绑定的模型理由。")
    return target, len(candidates)


async def verify_live(app: FastAPI) -> LiveEvidence:
    settings: Settings = app.state.settings
    registry = {item.library_id: item.source_type for item in settings.libraries}
    if registry != SOURCES or any(item.simulation for item in settings.libraries):
        raise LiveCheckError("未注册演示需要的三个真实数据源。")
    if not app.state.agent_runner.available:
        raise LiveCheckError("未加载默认模型；请检查 CITATION_MODEL_CONFIG 和模型配置文件。")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        try:
            async with asyncio.timeout(settings.agent_timeout_ms / 1000 + 15):
                return await _run_probe(client, tuple(registry))
        except TimeoutError:
            raise LiveCheckError("真实演示验收到期；没有完成证明，不能视为通过。") from None


async def _run_probe(client: httpx.AsyncClient, sources: tuple[str, ...]) -> LiveEvidence:
    response = await client.post(
        "/api/v1/agent/runs",
        json={"input_type": "keywords", "keywords": ["lfy2"], "library_ids": sources},
    )
    run = AgentRun.model_validate(_response_body(response, "提交 Agent 检索"))
    while run.status in {"pending", "running"}:
        await asyncio.sleep(0.5)
        response = await client.get(f"/api/v1/agent/runs/{run.run_id}")
        run = AgentRun.model_validate(_response_body(response, "读取 Agent 状态"))
    if run.status != "completed" or not (run.summary or "").strip() or not run.served_model:
        raise LiveCheckError(f"未收到真实模型完成的助手答复{_error_code(run.error)}。")
    if not run.conversation_id or not run.queries:
        raise LiveCheckError("Agent 没有交付与会话关联的检索结果。")
    response = await client.get(f"/api/v1/conversations/{run.conversation_id}")
    conversation = _response_body(response, "读取会话")
    if not any(
        message["run_id"] == run.run_id
        and message["role"] == "assistant"
        and message["text"] == run.summary
        for message in conversation["messages"]
    ):
        raise LiveCheckError("Agent 的最终答复没有保存到会话。")

    errors = []
    for query in reversed(run.queries):
        route = f"/api/v1/searches/{query.created.query_id}"
        state = SearchStateResponse.model_validate(
            _response_body(await client.get(route), "读取检索结果")
        )
        try:
            target, ranked_count = _verified_ranking(state)
        except LiveCheckError as exc:
            errors.append(str(exc))
            continue
        break
    else:
        raise LiveCheckError("；".join(dict.fromkeys(errors)))

    replay = await client.get(f"{route}/events")
    events = [line for line in replay.text.splitlines() if line.startswith("event: ")]
    if (
        not replay.is_success
        or "event: candidate_reranked" not in events
        or events[-1:] != ["event: search_finalized"]
    ):
        raise LiveCheckError("没有可回放的重排与检索完成事件。")
    formats = _response_body(
        await client.get(
            f"{route}/citation-formats",
            params={"result_id": target.result_id, "result_version": target.pool_version},
        ),
        "获取样例论文的标准书目",
    )["formats"]
    if not {"apa", "vancouver"}.issubset(
        choice["format"] for choice in formats if choice.get("text")
    ):
        raise LiveCheckError("样例论文的 APA 和 Vancouver 书目未完整生成。")
    assert target.reason and target.reason.served_model and target.llm_rank
    return LiveEvidence(
        run.model,
        run.served_model,
        target.reason.served_model,
        tuple(worker.library_id for worker in state.workers),
        ranked_count,
        target.llm_rank,
    )
