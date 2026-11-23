"""Read a published, explicitly licensed Research Square version without resolving links."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Literal

RESEARCH_SQUARE_HOST = "www.researchsquare.com"
RESEARCH_SQUARE_VERSION = re.compile(
    r"10\.21203/rs\.3\.(rs-[1-9]\d{0,11})/v([1-9]\d{0,3})", re.ASCII
)
RESEARCH_SQUARE_PATH = re.compile(r"/article/rs-[1-9]\d{0,11}/v[1-9]\d{0,3}", re.ASCII)
RESEARCH_SQUARE_NORMALIZATION = "research-square-paragraphs-v1"
MAX_RESEARCH_SQUARE_BYTES = 4_000_000
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
_NEXT_DATA_TAG = '<script id="__NEXT_DATA__" type="application/json">'
_IGNORED_TAGS = {"script", "style", "template", "figure", "table", "ol", "ul", "svg", "iframe"}
_BLOCKS = {"p", "h2", "h3", "h4", "h5", "h6"}
_EXCLUDED = re.compile(
    r"\b(abstract|key message|reference\w*|declaration\w*|acknowledg\w*|supplement\w*|"
    r"funding|author contributions?|conflicts? of interest|data availability)\b",
    re.I,
)


class PreprintError(ValueError):
    def __init__(self, kind: Literal["identity", "license", "withdrawn", "unavailable"]) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class PreprintParagraph:
    text: str
    section_path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PublicPreprint:
    title: str
    authors: tuple[tuple[str, str], ...]
    version_date: datetime
    is_review: bool
    paragraphs: tuple[PreprintParagraph, ...]


def research_square_url(doi: str | None) -> str | None:
    match = RESEARCH_SQUARE_VERSION.fullmatch(doi or "")
    if not match:
        return None
    identity, version = match.groups()
    return f"https://{RESEARCH_SQUARE_HOST}/article/{identity}/v{version}"


class _BodyParagraphs(HTMLParser):
    def __init__(self, header: str) -> None:
        super().__init__(convert_charrefs=True)
        self.header = header
        self.headings: list[tuple[int, str]] = []
        self.ignored: list[str] = []
        self.block: str | None = None
        self.chunks: list[str] = []
        self.paragraphs: list[PreprintParagraph] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _IGNORED_TAGS:
            self.ignored.append(tag)
        elif not self.ignored and tag in _BLOCKS:
            self.block = tag
            self.chunks = []
        elif not self.ignored and self.block and tag == "br":
            self.chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if self.ignored:
            if tag == self.ignored[-1]:
                self.ignored.pop()
            return
        if self.block != tag:
            return
        text = " ".join("".join(self.chunks).split())
        self.block = None
        self.chunks = []
        if not text:
            return
        if tag == "p":
            path = (self.header, *(heading for _, heading in self.headings))
            if not any(_EXCLUDED.search(heading) for heading in path):
                self.paragraphs.append(PreprintParagraph(text, path))
        else:
            level = int(tag[1])
            self.headings = [(depth, title) for depth, title in self.headings if depth < level]
            self.headings.append((level, text[:500]))

    def handle_data(self, data: str) -> None:
        if self.block and not self.ignored:
            self.chunks.append(data)


def parse_preprint(body: bytes, doi: str) -> PublicPreprint:
    match = RESEARCH_SQUARE_VERSION.fullmatch(doi)
    if match is None:
        raise PreprintError("identity")
    identity, version = match.groups()
    if len(body) > MAX_RESEARCH_SQUARE_BYTES:
        raise PreprintError("unavailable")
    try:
        html = body.decode("utf-8", errors="strict")
        if html.count(_NEXT_DATA_TAG) != 1:
            raise PreprintError("unavailable")
        _, _, tail = html.partition(_NEXT_DATA_TAG)
        data = json.loads(tail.split("</script>", 1)[0])
        query, props = data["query"], data["props"]["pageProps"]
        record = props["initialData"]
        if (
            query["identity"] != identity
            or query["version"] != [f"v{version}"]
            or props["version"] != f"v{version}"
            or record["identity"] != identity
            or record["doi"].casefold() != doi
        ):
            raise PreprintError("identity")
        if record["isWithdrawnOrRetracted"] is True:
            raise PreprintError("withdrawn")
        if record["isWithdrawnOrRetracted"] is not False or record["published"] is not True:
            raise PreprintError("unavailable")
        version_rows = [row for row in record["nonDraftVersions"] if row["code"] == int(version)]
        if len(version_rows) != 1 or version_rows[0]["doi"].casefold() != doi:
            raise PreprintError("identity")
        if version_rows[0]["status"] != "published":
            raise PreprintError("unavailable")
        license_info = record["license"]
        if (
            license_info["name"].strip().casefold() != "cc by 4.0"
            or license_info["url"] != LICENSE_URL
        ):
            raise PreprintError("license")
        date = datetime.fromisoformat(version_rows[0]["date"])
        title = record["manuscriptTitle"].strip()
        authors = tuple(
            (
                author["lastName"].strip(),
                " ".join(
                    part.strip()
                    for part in (author.get("firstName"), author.get("middleName"))
                    if part
                ),
            )
            for author in record["authors"]
        )
        if not title or not authors or not all(name for name, _ in authors):
            raise PreprintError("unavailable")
        paragraphs = []
        for section in record["fulltext"]:
            header = section["header"].strip()
            if not header or _EXCLUDED.search(header):
                continue
            parser = _BodyParagraphs(header[:500])
            parser.feed(section["content"])
            parser.close()
            paragraphs.extend(parser.paragraphs)
        if not paragraphs:
            raise PreprintError("unavailable")
        return PublicPreprint(
            title, authors, date, "review" in record["articleType"].casefold(), tuple(paragraphs)
        )
    except PreprintError:
        raise
    except (AttributeError, IndexError, KeyError, RecursionError, TypeError, ValueError) as exc:
        raise PreprintError("unavailable") from exc
