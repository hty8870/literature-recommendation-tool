// 对话消息的纯数据模型。消息正文一律保留为普通字符串，渲染层只用 textContent。

const TERMINAL = new Set(['completed', 'failed', 'cancelled']);

export function normalizeConversationMessages(raw) {
  if (!Array.isArray(raw)) return [];
  const seen = new Set();
  const rows = [];
  raw.forEach((item, index) => {
    if (!item || typeof item !== 'object') return;
    const role = item.role === 'user' ? 'user' : item.role === 'assistant' ? 'assistant' : null;
    if (!role) return;
    const content = typeof item.content === 'string'
      ? item.content : typeof item.text === 'string' ? item.text : '';
    const id = String(item.message_id ?? item.seq ?? `server-${index + 1}`);
    if (seen.has(id)) return;
    seen.add(id);
    rows.push({
      message_id: id,
      role,
      content,
      run_id: typeof item.run_id === 'string' && item.run_id ? item.run_id : null,
      query_ids: Array.isArray(item.query_ids)
        ? [...new Set(item.query_ids.filter((value) => typeof value === 'string' && value))]
        : [],
      seq: Number.isFinite(Number(item.seq)) ? Number(item.seq) : null,
      _index: index,
    });
  });
  if (rows.every((row) => numberOrNull(row.seq) != null)) {
    rows.sort((a, b) => a.seq - b.seq || a._index - b._index);
  }
  for (const row of rows) delete row._index;
  return rows;
}

function numberOrNull(value) {
  return Number.isFinite(value) ? value : null;
}

export function failureMessage(error, fallback = '请求失败。') {
  if (!error) return fallback;
  if (typeof error === 'string') return error;
  const code = typeof error.code === 'string' ? error.code : '';
  const message = typeof error.message === 'string' ? error.message : '';
  return [code, message].filter(Boolean).join('：') || fallback;
}

export function runProgressText(run, hasQuery) {
  if (run?.citation_preparation?.status === 'waiting') return '正在准备引用建议…';
  return hasQuery ? '正在检索并整理结果…' : 'Agent 正在处理本轮请求…';
}

export function citationPreparationNote(run) {
  const preparation = run?.citation_preparation;
  if (run?.status !== 'completed' || preparation?.status !== 'partial') return '';
  if (preparation.unfinished_batches > 0) return '部分引用建议尚未完成，可在结果中查看或补生成。';
  const ready = Number.isInteger(preparation.ready) ? `${preparation.ready} 条已生成，` : '';
  return `引用建议：${ready}${preparation.unavailable} 条未生成。书目信息仍可单独复制。`;
}

export function runChatFallback(run) {
  if (!run || typeof run.run_id !== 'string' || !TERMINAL.has(run.status)) return null;
  const base = {
    message_id: `run-status-${run.run_id}`,
    run_id: run.run_id,
    query_ids: [],
    derived: true,
  };
  if (run.status === 'cancelled') {
    return {
      ...base,
      role: 'assistant',
      kind: 'status',
      level: 'warn',
      content: '本轮已取消；可以继续发送新消息。',
    };
  }
  if (run.status === 'failed') {
    return {
      ...base,
      role: 'assistant',
      kind: 'status',
      level: 'error',
      content: failureMessage(run.error, 'Agent 未能完成本轮；可以继续发送新消息重试。'),
    };
  }
  if (typeof run.summary === 'string' && run.summary.trim()) {
    return {
      ...base,
      role: 'assistant',
      kind: 'assistant',
      content: run.summary,
    };
  }
  return {
    ...base,
    role: 'assistant',
    kind: 'status',
    level: 'warn',
    content: '本轮已结束，但没有返回文字说明；可以继续追问或重新检索。',
  };
}
