import fs from "node:fs";
import path from "node:path";
import { Document, Packer, Paragraph, TextRun } from "docx";

const output = process.argv[2];
if (!output || !path.isAbsolute(output)) throw new Error("An absolute DOCX output is required");

const input = path.join(path.dirname(output), "paper.txt");
const run = (text, options = {}) => new TextRun({ text, font: "Times New Roman", size: 24, ...options });
const para = (children, options = {}) => new Paragraph({
  spacing: { after: 120, line: 300 }, ...options,
  children: Array.isArray(children) ? children : [children],
});
const lines = fs.readFileSync(input, "utf8").trimEnd().split("\n");
if (!lines.length || lines.some((line) => !/^P\d{4} \[/.test(line))) {
  throw new Error("Supplied attachment must contain canonical passage lines");
}
const doc = new Document({ sections: [{ children: lines.map((line) => para(run(line))) }] });
fs.writeFileSync(output, await Packer.toBuffer(doc), { flag: "wx" });
