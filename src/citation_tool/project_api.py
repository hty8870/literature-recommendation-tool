"""Same-origin project and source-approval API. No learning tool can call it."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, Path, Request
from pydantic import Field

from .account_api import _bounded_json, require_account
from .accounts import AccountError
from .entities import StrictModel
from .search import SearchService, library_info

if TYPE_CHECKING:
    from .learning import LearningLoop
    from .projects import Project, ProjectStore

PROJECT_ID = r"^(other|prj_[a-f0-9]{32})$"
APPLICATION_ID = r"^src_[a-f0-9]{32}$"


class CreateProject(StrictModel):
    name: str = Field(min_length=1, max_length=72)


class UpdateProject(StrictModel):
    revision: int = Field(ge=0)
    name: str | None = Field(default=None, min_length=1, max_length=72)
    share_sources: bool | None = None
    library_ids: list[str] | None = Field(default=None, min_length=1, max_length=32)
    learning_enabled: bool | None = None
    archived: bool | None = None


class SourceDecision(StrictModel):
    decision: Literal["approve", "decline", "retry", "close"]
    connector_version: str = Field(min_length=1, max_length=100)


def build_project_router(
    projects: ProjectStore, service: SearchService, learning: LearningLoop | None
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    def payload(project: Project) -> dict:
        data = asdict(project)
        data["libraries"] = [
            library_info(service.library_configs[key]) for key in project.available_library_ids
        ]
        return data

    @router.get("/projects")
    async def list_projects(request: Request) -> dict:
        account = require_account(request)
        items = await asyncio.to_thread(projects.list, account.tenant_id)
        return {"items": items, "learning_available": learning is not None}

    @router.post("/projects", status_code=201)
    async def create_project(request: Request) -> dict:
        account = require_account(request)
        body = await _bounded_json(request, CreateProject)
        project = await asyncio.to_thread(projects.create, account.tenant_id, body.name)
        return payload(project)

    @router.get("/projects/{project_id}")
    async def get_project(request: Request, project_id: str = Path(pattern=PROJECT_ID)) -> dict:
        account = require_account(request)
        return payload(await asyncio.to_thread(projects.get, account.tenant_id, project_id))

    @router.patch("/projects/{project_id}")
    async def update_project(request: Request, project_id: str = Path(pattern=PROJECT_ID)) -> dict:
        account = require_account(request)
        body = await _bounded_json(request, UpdateProject)
        if body.learning_enabled and learning is None:
            raise AccountError(503, "LEARNING_UNAVAILABLE", "后台数据源学习未启用。")
        project = await asyncio.to_thread(
            projects.update, account.tenant_id, project_id, **body.model_dump(exclude_none=True)
        )
        return payload(project)

    @router.get("/source-requests")
    async def get_requests(request: Request) -> dict:
        account = require_account(request)
        items = (
            await asyncio.to_thread(learning.requests.inbox, account.tenant_id) if learning else []
        )
        return {"items": items}

    @router.post("/source-requests/{application_id}/decision")
    async def decide(request: Request, application_id: str = Path(pattern=APPLICATION_ID)) -> dict:
        account = require_account(request)
        if learning is None:
            raise AccountError(503, "LEARNING_UNAVAILABLE", "后台数据源学习未启用。")
        body = await _bounded_json(request, SourceDecision)
        receipt = await asyncio.to_thread(
            learning.requests.decide,
            account.tenant_id,
            application_id,
            body.connector_version,
            body.decision,
        )
        learning.wake()
        return receipt

    return router
