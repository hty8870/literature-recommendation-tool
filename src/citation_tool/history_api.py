"""Account-scoped history, without exposing the anonymous workspace as a directory."""

from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from pydantic import Field

from .account_api import require_account
from .accounts import AccountError
from .agent import AgentRunner
from .citation_texts import CitationTextService
from .entities import StrictModel
from .store import Store


class RenameConversation(StrictModel):
    title: str = Field(min_length=1, max_length=120)


def _cursor(value: str | None) -> tuple[str, str] | None:
    if value is None:
        return None
    timestamp, _, conversation_id = value.partition("~")
    try:
        parsed = datetime.fromisoformat(timestamp)
        if parsed.tzinfo is None or not conversation_id or "~" in conversation_id:
            raise ValueError
    except ValueError:
        raise AccountError(
            400, "HISTORY_CURSOR_INVALID", "历史记录分页已失效，请重新打开。"
        ) from None
    return timestamp, conversation_id


def build_history_router(
    store: Store, agent: AgentRunner, citations: CitationTextService
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/conversations")

    @router.get("")
    async def list_history(
        request: Request,
        limit: int = Query(20, ge=1, le=50),
        before: str | None = Query(None, max_length=120),
        project_id: str | None = Query(None, pattern=r"^(other|prj_[a-f0-9]{32})$"),
    ) -> dict:
        account = require_account(request)
        rows = await asyncio.to_thread(
            store.list_conversations,
            account.tenant_id,
            limit=limit + 1,
            before=_cursor(before),
            project_id=project_id,
        )
        page = rows[:limit]
        last = page[-1] if page else None
        return {
            "items": page,
            "next_cursor": f"{last['updated_at']}~{last['conversation_id']}"
            if last and len(rows) > limit
            else None,
        }

    @router.patch("/{conversation_id}")
    async def rename(request: Request, conversation_id: str, body: RenameConversation) -> dict:
        account = require_account(request)
        title = " ".join(body.title.split())
        if not title or not title.isprintable():
            raise AccountError(400, "CONVERSATION_TITLE_INVALID", "请输入有效的对话标题。")
        await asyncio.to_thread(
            store.rename_conversation, account.tenant_id, conversation_id, title
        )
        return {"conversation_id": conversation_id, "title": title}

    @router.delete("/{conversation_id}", status_code=204)
    async def delete(request: Request, conversation_id: str) -> Response:
        account = require_account(request)
        scope = await asyncio.to_thread(
            store.conversation_purge_scope, account.tenant_id, conversation_id
        )
        if agent.has_live_runs(scope["run_ids"]) or agent.service.coordinator.has_live_searches(
            scope["query_ids"]
        ):
            raise AccountError(
                409, "CONVERSATION_BUSY", "对话仍在处理。请停止本轮或等它完成后删除。"
            )
        await citations.purge_queries(account.tenant_id, set(scope["query_ids"]))
        await asyncio.to_thread(store.purge_linked_data, account.tenant_id, **scope)
        return Response(status_code=204)

    return router
