from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn


def check_startup() -> tuple[str, bool]:
    from .api import create_app
    from .config import PROJECT_ROOT
    from .runtime import settings_from_env

    async def check(root: Path) -> tuple[str, bool]:
        settings = replace(
            settings_from_env(),
            db_path=root / "app.db",
            auth_db_path=root / "auth.db",
            learning_enabled=False,
        )
        for asset in ("index.html", "js/main.js"):
            if not (PROJECT_ROOT / "web" / asset).is_file():
                raise ValueError(f"missing web asset: {asset}; run from the source checkout")
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            sources = ", ".join(library.library_id for library in app.state.settings.libraries)
            return sources, app.state.agent_runner.available

    with TemporaryDirectory(prefix="citation-tool-check-") as root:
        return asyncio.run(check(Path(root)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="citation-tool", description="Start the service.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--check", action="store_true", help="check startup without listening")
    args = parser.parse_args(argv)
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
