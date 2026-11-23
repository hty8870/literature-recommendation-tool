"""Check specification traceability and relative links without external services."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs" / "spec" / "文献推荐工具_AGENT_SPEC.md"


def cells(line: str) -> list[str]:
    return [part.strip() for part in line.strip().strip("|").split("|")]


def main() -> None:
    source = SPEC.read_text(encoding="utf-8")
    chunks = source.split("```json")[1:]
    for index, chunk in enumerate(chunks, 1):
        try:
            json.loads(chunk.split("```", 1)[0])
        except json.JSONDecodeError as error:
            raise AssertionError(f"JSON example {index}: {error}") from error

    definitions = re.findall(r"^\| \*\*(AT-[A-Z]+-\d{3})\*\* \|", source, re.MULTILINE)
    if len(definitions) != len(set(definitions)):
        raise AssertionError("Duplicate acceptance scenario IDs")
    matrix = source.split("## 18.", 1)[1].split("\n## ", 1)[0]
    missing = sorted(set(definitions) - set(re.findall(r"AT-[A-Z]+-\d{3}", matrix)))
    if missing:
        raise AssertionError(f"Acceptance scenarios absent from traceability matrix: {missing}")

    requirement_ids: list[str] = []
    in_code = False
    for line in matrix.splitlines():
        if line.startswith("```"):
            in_code = not in_code
        if not in_code and line.startswith("|"):
            columns = cells(line)
            if re.fullmatch(r"[A-Z]+-\d{3}", columns[0]):
                requirement_ids.append(columns[0])
    if len(requirement_ids) != len(set(requirement_ids)):
        raise AssertionError("Duplicate requirement IDs in the traceability matrix")

    for document in (ROOT / "README.md", *ROOT.glob("docs/**/*.md")):
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
            if "://" in target or target.startswith("#"):
                continue
            if not (document.parent / target.split("#", 1)[0]).exists():
                raise AssertionError(f"Broken relative link in {document.name}: {target}")

    print(
        f"PASS: {len(requirement_ids)} unique requirements; "
        f"{len(definitions)} traced acceptance scenarios; {len(chunks)} JSON examples."
    )


if __name__ == "__main__":
    main()
