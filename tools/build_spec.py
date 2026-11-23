"""Rebuild the DOCX mirror through the installed docx skill, never the reverse."""

import os
import shutil
import subprocess
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    skill = Path(
        os.environ.get("KIMI_DOCX_SKILL_DIR", Path.home() / ".kimi-code" / "skills" / "docx")
    )
    wrapper = skill / "scripts" / "docx"
    if not wrapper.is_file():
        raise SystemExit(f"docx skill not found: {wrapper}")
    bash = shutil.which("bash")
    if not bash:
        raise SystemExit("Git Bash is required by the local docx skill wrapper.")
    output = root / "docs" / "spec" / "文献推荐工具_AGENT_SPEC.docx"
    environment = os.environ.copy()
    virtual_env = environment.pop("VIRTUAL_ENV", None)
    environment.pop("PYTHONHOME", None)
    if virtual_env:
        env_path = Path(virtual_env)
        excluded = {(env_path / name).as_posix().casefold() for name in ("Scripts", "bin")}
        environment["PATH"] = os.pathsep.join(
            item
            for item in environment.get("PATH", "").split(os.pathsep)
            if Path(item).as_posix().casefold() not in excluded
        )
    subprocess.run(
        [bash, str(wrapper), "build", str(root / "tools" / "build_agent_spec.js"), str(output)],
        check=True,
        cwd=root,
        env=environment,
    )


if __name__ == "__main__":
    main()
