from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import uvicorn

if TYPE_CHECKING:
    from fastapi import FastAPI

    from .live_check import LiveEvidence


def _checked_app(root: Path, *, live: bool) -> FastAPI:
    from .api import create_app
    from .config import PROJECT_ROOT
    from .live_check import check_profile
    from .runtime import settings_from_env

    settings = replace(
        settings_from_env(),
        db_path=root / "app.db",
        auth_db_path=root / "auth.db",
        learning_enabled=False,
    )
    if live:
        check_profile(settings)
    for asset in ("index.html", "js/main.js"):
        if not (PROJECT_ROOT / "web" / asset).is_file():
            raise ValueError(f"missing web asset: {asset}; run from the source checkout")
    return create_app(settings)


def check_startup() -> tuple[str, bool]:
    async def check(root: Path) -> tuple[str, bool]:
        app = _checked_app(root, live=False)
        async with app.router.lifespan_context(app):
            sources = ", ".join(library.library_id for library in app.state.settings.libraries)
            return sources, app.state.agent_runner.available

    with TemporaryDirectory(prefix="citation-tool-check-") as root:
        return asyncio.run(check(Path(root)))


def check_live() -> LiveEvidence:
    from .live_check import verify_live

    async def check(root: Path) -> LiveEvidence:
        app = _checked_app(root, live=True)
        async with app.router.lifespan_context(app):
            return await verify_live(app)

    with TemporaryDirectory(prefix="citation-tool-live-check-") as root:
        return asyncio.run(check(Path(root)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="citation-tool", description="Start the service.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument("--check", action="store_true", help="check startup without upstream calls")
    checks.add_argument(
        "--check-live", action="store_true", help="verify real Agent delivery using a public query"
    )
    args = parser.parse_args(argv)
    if args.check_live:
        print("正在联网验收 lfy2：将调用真实模型和 Europe PMC，不写入服务数据库。", flush=True)
        try:
            evidence = check_live()
        except (OSError, RuntimeError, ValueError) as exc:
            parser.exit(1, f"真实演示验收未通过：{exc}\n")
        print(
            f"真实演示验收通过：Agent={evidence.agent_model}，LLM 重排={evidence.ranking_model}，"
            f"实际查询源={', '.join(evidence.sources_used)}，{evidence.ranked_count} 条已排候选；"
            f"样例论文第 {evidence.target_rank} 名，已有推荐理由、流式事件和标准书目。"
        )
        return 0
    if args.check:
        try:
            sources, model_configured = check_startup()
        except (OSError, RuntimeError, ValueError) as exc:
            parser.exit(1, f"Startup check failed: {exc}\n")
        print(f"Startup check passed. Sources: {sources}")
        configured = "yes" if model_configured else "no"
        print(f"Default model configured: {configured}. No upstream calls.")
        return 0
    uvicorn.run(
        "citation_tool.api:create_app", factory=True, host=args.host, port=args.port, workers=1
    )
    return 0
