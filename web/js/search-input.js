export const DEMO_SCOPE = {
  species: ['Glycine max'],
  system: ['greenhouse', 'field'],
  condition: [],
  dose: [],
  method: ['expression profiling', 'association analysis'],
  population: [],
  free_text: '',
};

export const INPUT_MODES = {
  keywords: {
    label: '关键词',
    placeholder: '输入研究主题，例如：LFY2, soybean floral development',
    help: '多个关键词可用逗号分隔；Enter 发送。',
  },
  abstract: {
    label: 'Abstract',
    placeholder: '粘贴论文摘要，系统将围绕其中的论述寻找相关文献',
    help: '输入英文或中文摘要；Ctrl / Command + Enter 发送。',
  },
  full_text: {
    label: '全文',
    placeholder: '',
    help: '选择 PDF 或 DOCX 后会上传解析；解析完成后再发送。',
  },
};

const MANUSCRIPT_TYPES = {
  '.pdf': 'application/pdf',
  '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
};

export function manuscriptContentType(name) {
  const extension = String(name || '').toLowerCase().match(/\.[^.]+$/)?.[0];
  return MANUSCRIPT_TYPES[extension] || null;
}

export class InputError extends Error {
  constructor(message, code = 'INVALID_INPUT') {
    super(message);
    this.name = 'InputError';
    this.code = code;
  }
}

export function terms(value) {
  return String(value || '').split(/[,，、;；\n]+/).map((item) => item.trim()).filter(Boolean).slice(0, 12);
}

export function libraryDisplayName(library) {
  return String(library.display_name || library.library_id).replace(/[（(].*$/, '').trim();
}

export function libraryPillLabel(library) {
  const name = libraryDisplayName(library);
  return Number.isInteger(library.record_count)
    ? `${name} · ${library.record_count.toLocaleString('en-US')}`
    : name;
}

export const DEFAULT_UPLOAD_BYTES = 50 * 1024 * 1024;

export function uploadLimitLabel(maxBytes) {
  return `${Math.floor(maxBytes / 1024 / 1024 * 10) / 10} MB`;
}

export function validateManuscriptFile(file, maxBytes = DEFAULT_UPLOAD_BYTES) {
  if (!file) return '请先选择一个 PDF 或 DOCX 文件。';
  if (!manuscriptContentType(file.name)) return '仅支持 PDF 或 DOCX；旧版 .doc 请另存为 DOCX。';
  if (!Number.isFinite(file.size) || file.size <= 0) return '文件为空或无法读取文件大小。';
  if (file.size > maxBytes) return `文件超过 ${uploadLimitLabel(maxBytes)}，请选择较小的文件。`;
  return null;
}

export function buildSearchPayload({ mode, value, libraries, selectedLibraryIds, demoScopeEnabled, manuscript = null }) {
  if (!INPUT_MODES[mode]) throw new InputError('未知的检索模式。');
  const selected = (libraries || []).filter((library) => selectedLibraryIds.has(library.library_id));
  if (!selected.length) throw new InputError('请至少选择一个数据源。', 'NO_LIBRARY_SELECTED');
  if (mode === 'full_text') {
    if (!manuscript?.manuscript_id) {
      throw new InputError('请先上传全文文件并等待解析完成。', 'MANUSCRIPT_REQUIRED');
    }
    if (manuscript.parse_status !== 'ready') {
      throw new InputError('全文仍在解析，完成后才能开始检索。', 'MANUSCRIPT_NOT_READY');
    }
    return {
      input_type: 'full_text',
      manuscript_id: manuscript.manuscript_id,
      library_ids: selected.map((library) => library.library_id),
    };
  }
  const payload = {
    input_type: mode,
    library_ids: selected.map((library) => library.library_id),
    options: {
      library_delays_ms: Object.fromEntries(selected.map((library) => [library.library_id, library.default_delay_ms])),
    },
  };
  if (mode === 'keywords') {
    const keywords = terms(value);
    if (!keywords.length) throw new InputError('请输入至少一个关键词。', 'EMPTY_QUERY');
    payload.keywords = keywords;
    payload.topic_context = keywords;
  } else {
    const abstract = String(value || '').trim();
    if (!abstract) throw new InputError('请粘贴需要检索的 Abstract。', 'EMPTY_QUERY');
    payload.abstract = abstract;
  }
  if (demoScopeEnabled && selected.every((library) => library.simulation !== false)) {
    payload.claim_scope = DEMO_SCOPE;
  }
  return payload;
}
