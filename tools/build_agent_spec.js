import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  AlignmentType, BorderStyle, Document, Footer, Header, HeadingLevel,
  ImportedXmlComponent, Packer, PageBreak, PageNumber, Paragraph,
  ShadingType, Table, TableCell, TableLayoutType, TableRow, TextRun,
  WidthType, convertInchesToTwip,
} from "docx";

const base = path.dirname(fileURLToPath(import.meta.url));
const markdownPath = path.join(base, "..", "docs", "spec", "文献推荐工具_AGENT_SPEC.md");
const args = process.argv.slice(2);
const checkOnly = args.includes("--check");
const outputPath = args.find((arg) => arg !== "--check");

if (!checkOnly && !outputPath) throw new Error("Usage: node build_agent_spec.js /absolute/output.docx\n       node build_agent_spec.js --check");
if (outputPath && !path.isAbsolute(outputPath)) throw new Error("Output path must be absolute");
if (outputPath && !/\.docx$/i.test(outputPath)) throw new Error("Output path must end with .docx");
if (outputPath && path.resolve(outputPath) === path.resolve(markdownPath)) throw new Error("Output path must not be the Markdown source");
if (!fs.existsSync(markdownPath)) throw new Error(`Markdown source not found: ${markdownPath}`);

const markdown = fs.readFileSync(markdownPath, "utf8").replace(/^\uFEFF/, "");

function splitFrontMatter(source) {
  const lines = source.split(/\r?\n/);
  if (lines[0]?.trim() !== "---") return { meta: {}, body: source };
  const end = lines.findIndex((line, index) => index > 0 && line.trim() === "---");
  if (end < 0) throw new Error(`Front matter is missing a closing --- in ${markdownPath}`);
  const meta = {};
  for (const line of lines.slice(1, end)) {
    const match = line.match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
    if (!match) continue;
    meta[match[1]] = match[2].trim().replace(/^(["'])(.*)\1$/, "$2");
  }
  return { meta, body: lines.slice(end + 1).join("\n") };
}

function extractCover(body, meta) {
  const lines = body.split(/\r?\n/);
  let index = 0;
  while (index < lines.length && !lines[index].trim()) index += 1;
  let title = "";
  const heading = lines[index]?.match(/^#\s+(.+)$/);
  if (heading) {
    title = heading[1].trim();
    index += 1;
  }
  return {
    title: title || meta.title || "文献推荐工具 Agent 实施规格",
    subtitle: [meta.spec_version ? `V${meta.spec_version}` : "", meta.status || ""].filter(Boolean).join(" · "),
    body: lines.slice(index).join("\n"),
  };
}

const { meta, body } = splitFrontMatter(markdown);
const cover = extractCover(body, meta);

const bodyFont = { ascii: "Aptos", hAnsi: "Aptos", cs: "Aptos", eastAsia: "SimSun" };
const headingFont = { ascii: "Aptos Display", hAnsi: "Aptos Display", cs: "Aptos Display", eastAsia: "Microsoft YaHei" };
const monoFont = { ascii: "Consolas", hAnsi: "Consolas", cs: "Consolas", eastAsia: "Microsoft YaHei" };
const palette = { dark: "20343A", primary: "27636B", secondary: "56767B", accent: "A96F3B", border: "CDDADB", fill: "EAF2F2", soft: "F5F8F8", white: "FFFFFF", muted: "68787C" };
const run = (text, options = {}) => new TextRun({ text, font: bodyFont, size: 21, color: palette.dark, ...options });
const para = (children, options = {}) => new Paragraph({ spacing: { after: 135, line: 315 }, ...options, children: Array.isArray(children) ? children : [children] });
const bodyPara = (text) => para(inlineRuns(text), { indent: { firstLine: convertInchesToTwip(0.31) } });
function inlineRuns(text, options = {}) {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean);
  return parts.map((part) => {
    if (part.startsWith("**") && part.endsWith("**")) return run(part.slice(2, -2), { bold: true, ...options });
    if (part.startsWith("`") && part.endsWith("`")) return run(part.slice(1, -1), { font: monoFont, color: palette.primary, ...options });
    return run(part, options);
  });
}
const headingStyles = {
  1: { level: HeadingLevel.HEADING_1, size: 29, before: 320, after: 150, line: 320 },
  2: { level: HeadingLevel.HEADING_2, size: 24, before: 220, after: 110, line: 300 },
  3: { level: HeadingLevel.HEADING_3, size: 22, before: 200, after: 100, line: 295 },
  4: { level: HeadingLevel.HEADING_4, size: 21, before: 180, after: 90, line: 290 },
  5: { level: HeadingLevel.HEADING_4, size: 20, before: 160, after: 80, line: 285 },
  6: { level: HeadingLevel.HEADING_4, size: 20, before: 160, after: 80, line: 285 },
};
const headingPara = (text, level) => {
  const style = headingStyles[level] || headingStyles[4];
  return para(inlineRuns(text, { bold: true, font: headingFont, size: style.size, color: level === 1 ? palette.primary : palette.dark }), {
    heading: style.level,
    spacing: { before: style.before, after: style.after, line: style.line },
    keepNext: true,
  });
};
const spacer = () => para(run(""), { spacing: { after: 50, line: 100 } });
const pageBreak = () => new Paragraph({ children: [new PageBreak()] });
const tableWidth = 9300;
function tableColumnWidths(count) {
  if (count === 2) return [2450, 6850];
  if (count === 3) return [1750, 3750, 3800];
  if (count === 4) return [1400, 3000, 1900, 3000];
  const width = Math.floor(tableWidth / count);
  return Array.from({ length: count }, (_, index) => index === count - 1 ? tableWidth - width * (count - 1) : width);
}
function makeTable(headers, rows) {
  const columnCount = Math.max(headers.length, ...rows.map((row) => row.length));
  const normalizedHeaders = Array.from({ length: columnCount }, (_, index) => headers[index] ?? "");
  const normalizedRows = rows.map((row) => Array.from({ length: columnCount }, (_, index) => row[index] ?? ""));
  const widths = tableColumnWidths(columnCount);
  const makeCell = (text, header = false, rowIndex = 0, cellIndex = 0) => new TableCell({
    children: [para(inlineRuns(text, { size: header ? 18 : 17, bold: header, color: header ? palette.white : palette.dark }), { spacing: { after: 35, line: 250 } })],
    width: { size: widths[cellIndex], type: WidthType.DXA },
    margins: { top: 90, bottom: 90, left: 105, right: 105 },
    shading: { type: ShadingType.CLEAR, fill: header ? palette.primary : rowIndex % 2 === 0 ? palette.soft : palette.white },
    borders: {
      top: { style: BorderStyle.SINGLE, size: 3, color: header ? palette.primary : palette.border },
      bottom: { style: BorderStyle.SINGLE, size: 3, color: header ? palette.primary : palette.border },
      left: { style: BorderStyle.SINGLE, size: 3, color: header ? palette.white : palette.border },
      right: { style: BorderStyle.SINGLE, size: 3, color: header ? palette.white : palette.border },
    },
  });
  return new Table({
    width: { size: 100, type: WidthType.PERCENTAGE }, columnWidths: widths, layout: TableLayoutType.FIXED,
    rows: [
      new TableRow({ tableHeader: true, cantSplit: true, children: normalizedHeaders.map((h, i) => makeCell(h, true, 0, i)) }),
      ...normalizedRows.map((row, ri) => new TableRow({ cantSplit: true, children: row.map((c, ci) => makeCell(c, false, ri, ci)) })),
    ],
  });
}
function codeTable(text) {
  return new Table({
    width: { size: 100, type: WidthType.PERCENTAGE }, columnWidths: [9300], layout: TableLayoutType.FIXED,
    rows: [new TableRow({ cantSplit: true, children: [new TableCell({
      width: { size: 9300, type: WidthType.DXA }, shading: { type: ShadingType.CLEAR, fill: "F1F5F5" },
      margins: { top: 130, bottom: 130, left: 150, right: 150 },
      borders: { left: { style: BorderStyle.SINGLE, size: 12, color: palette.primary }, top: { style: BorderStyle.SINGLE, size: 3, color: palette.border }, bottom: { style: BorderStyle.SINGLE, size: 3, color: palette.border }, right: { style: BorderStyle.SINGLE, size: 3, color: palette.border } },
      children: text.split("\n").map((line) => para(run(line || " ", { font: monoFont, size: 17 }), { spacing: { after: 0, line: 235 } })),
    })] })],
  });
}
function parseTable(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}
function markdownBlocks(source) {
  const lines = source.split("\n");
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i += 1; continue; }
    if (line.trim() === "---") { i += 1; continue; }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) { blocks.push({ type: "heading", level: heading[1].length, text: heading[2].trim() }); i += 1; continue; }
    if (line.startsWith("```")) {
      const code = []; i += 1;
      while (i < lines.length && !lines[i].startsWith("```")) { code.push(lines[i]); i += 1; }
      if (i < lines.length) i += 1;
      blocks.push({ type: "code", text: code.join("\n") }); continue;
    }
    if (line.startsWith("|") && i + 1 < lines.length && /^\|?[\s:|-]+\|?$/.test(lines[i + 1])) {
      const headers = parseTable(line);
      const rows = [];
      let j = i + 2;
      while (j < lines.length && lines[j].startsWith("|")) { rows.push(parseTable(lines[j])); j += 1; }
      blocks.push({ type: "table", headers, rows }); i = j; continue;
    }
    if (/^[-*]\s+/.test(line)) { blocks.push({ type: "bullet", text: line.replace(/^[-*]\s+/, "") }); i += 1; continue; }
    if (/^\d+\.\s+/.test(line)) { blocks.push({ type: "number", text: line }); i += 1; continue; }
    if (/^>\s?/.test(line)) {
      const quote = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) { quote.push(lines[i].replace(/^>\s?/, "")); i += 1; }
      const paragraphs = quote.join("\n").split(/\n\s*\n/).map((text) => text.replace(/\n/g, " ").trim()).filter(Boolean);
      blocks.push({ type: "quote", paragraphs }); continue;
    }
    const paragraph = [line.trim()]; i += 1;
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|\||[-*]\s|\d+\.\s|>\s?|---\s*$)/.test(lines[i])) {
      paragraph.push(lines[i].trim()); i += 1;
    }
    blocks.push({ type: "p", text: paragraph.join(" ") });
  }
  return blocks;
}

const blocks = markdownBlocks(cover.body);
const tocEntries = blocks
  .filter((block) => block.type === "heading" && block.level <= 2)
  .map((block) => ({ title: block.text, level: block.level }));

if (checkOnly) {
  const counts = blocks.reduce((result, block) => {
    result[block.type] = (result[block.type] || 0) + 1;
    return result;
  }, {});
  console.log(JSON.stringify({
    source: markdownPath,
    metaKeys: Object.keys(meta),
    title: cover.title,
    subtitle: cover.subtitle,
    tocEntries: tocEntries.length,
    blocks: counts,
  }, null, 2));
  process.exit(0);
}

const xmlEscape = (value) => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
const tocRunProperties = `<w:rPr><w:rFonts w:ascii="Aptos" w:hAnsi="Aptos" w:eastAsia="Microsoft YaHei" w:cs="Aptos"/><w:color w:val="${palette.dark}"/><w:sz w:val="21"/></w:rPr>`;
const toc = (entries) => {
  const cached = entries.map(({ title, level }) => {
    const indent = Math.max(0, level - 1) * 360;
    return `<w:p><w:pPr><w:pStyle w:val="TOC${level}"/><w:spacing w:after="60" w:line="300" w:lineRule="auto"/><w:ind w:left="${indent}"/></w:pPr><w:r>${tocRunProperties}<w:t xml:space="preserve">${xmlEscape(title)}</w:t></w:r></w:p>`;
  }).join("");
  return ImportedXmlComponent.fromXmlString(`<w:sdt xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:sdtPr><w:alias w:val="目录"/></w:sdtPr><w:sdtContent><w:p><w:r><w:fldChar w:fldCharType="begin" w:dirty="true"/><w:instrText xml:space="preserve"> TOC \\o &quot;1-2&quot; \\h \\z \\u </w:instrText><w:fldChar w:fldCharType="separate"/></w:r></w:p>${cached}<w:p><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p></w:sdtContent></w:sdt>`).root[0];
};

const titleParts = cover.title.match(/^(.+?)\s+(Agent.*)$/);
const titleLead = titleParts ? titleParts[1] : cover.title;
const titleTail = titleParts ? titleParts[2] : "";
const metaRows = Object.entries(meta).filter(([key]) => key !== "title");
const headerText = meta.spec_version ? `${cover.title} · V${meta.spec_version}` : cover.title;

const children = [
  para(run(titleLead, { bold: true, font: headingFont, size: 46, color: palette.primary }), { heading: HeadingLevel.TITLE, alignment: AlignmentType.CENTER, spacing: { before: 1250, after: titleTail ? 80 : 200, line: 480 } }),
  ...(titleTail ? [para(run(titleTail, { bold: true, font: headingFont, size: 34, color: palette.dark }), { alignment: AlignmentType.CENTER, spacing: { after: 260 } })] : []),
  ...(cover.subtitle
    ? [para(inlineRuns(cover.subtitle, { font: headingFont, size: 22, color: palette.accent }), { alignment: AlignmentType.CENTER, spacing: { after: 500 } })]
    : [para(run(""), { spacing: { after: 500 } })]),
  ...(metaRows.length ? [makeTable(["规格项", "内容"], metaRows), spacer()] : []),
  pageBreak(),
  para(run("目录", { bold: true, font: headingFont, size: 32, color: palette.primary }), { spacing: { before: 100, after: 100 } }),
  para(run("目录由 Word 动态域生成，缓存仅含章节标题、不含页码；在 Word/WPS 中更新域后显示实际页码。", { size: 18, color: palette.muted, italics: true }), { spacing: { after: 150 } }),
  toc(tocEntries),
  pageBreak(),
];

for (const block of blocks) {
  if (block.type === "heading") children.push(headingPara(block.text, block.level));
  if (block.type === "p") children.push(bodyPara(block.text));
  if (block.type === "bullet") children.push(para(inlineRuns(block.text), { bullet: { level: 0 }, spacing: { after: 75, line: 300 } }));
  if (block.type === "number") children.push(para(inlineRuns(block.text), { indent: { left: 360, hanging: 0 }, spacing: { after: 75, line: 300 } }));
  if (block.type === "quote") for (const text of block.paragraphs) children.push(para(inlineRuns(text, { italics: true }), { indent: { left: 360 }, border: { left: { style: BorderStyle.SINGLE, size: 12, color: palette.accent, space: 8 } }, spacing: { after: 90, line: 315 } }));
  if (block.type === "table") { children.push(makeTable(block.headers, block.rows)); children.push(spacer()); }
  if (block.type === "code") { children.push(codeTable(block.text)); children.push(spacer()); }
}

const header = new Header({ children: [para(run(headerText, { font: headingFont, size: 18, color: palette.secondary }), { alignment: AlignmentType.RIGHT, border: { bottom: { style: BorderStyle.SINGLE, size: 4, color: palette.border, space: 4 } } })] });
const footer = new Footer({ children: [para(new TextRun({ children: [PageNumber.CURRENT], font: bodyFont, size: 18, color: palette.muted }), { alignment: AlignmentType.CENTER })] });
const emptyHeader = new Header({ children: [para(run(""), { spacing: { after: 0 } })] });
const emptyFooter = new Footer({ children: [para(run(""), { spacing: { after: 0 } })] });
const doc = new Document({
  creator: "Kimi Code", title: cover.title, description: meta.description || cover.subtitle || cover.title,
  features: { updateFields: true },
  styles: { default: { document: { run: { font: bodyFont, size: 21, color: palette.dark }, paragraph: { spacing: { after: 135, line: 315 } } } } },
  sections: [{
    properties: { titlePage: true, page: { size: { width: 11906, height: 16838 }, margin: { top: 1134, bottom: 1134, left: 1276, right: 1276, header: 560, footer: 560 } } },
    headers: { first: emptyHeader, default: header }, footers: { first: emptyFooter, default: footer }, children,
  }],
});
fs.mkdirSync(path.dirname(outputPath), { recursive: true });
fs.writeFileSync(outputPath, await Packer.toBuffer(doc));
