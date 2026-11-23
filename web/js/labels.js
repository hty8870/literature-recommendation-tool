// 中文标签与展示格式化。所有面向用户的固定文案集中在这里，避免各视图各说一套。

export const SEARCH_STATE = {
  created: '已创建',
  running: '检索中',
  draining: '收尾中',
  finalizing: '定稿中',
  completed: '已完成',
  partial: '部分完成',
  failed: '失败',
  cancelled: '已取消',
};

export const AGENT_STATE = {
  pending: '等待 Agent 启动',
  running: 'Agent 正在自主决策',
  completed: 'Agent 已完成',
  failed: 'Agent 未完成',
  cancelled: 'Agent 已取消',
};

export const WORKER_STATE = {
  pending: '等待中',
  running: '运行中',
  draining: '收尾中',
  completed: '已完成',
  failed: '失败',
  timed_out: '已超时',
  cancelled: '已取消',
};

export const RELATIONSHIP = {
  support: '支持',
  complement: '补充',
  contrast: '对比',
  insufficient: '证据不足',
};

export const VERIFICATION = {
  pending: '核验中',
  verified: '已核验',
  insufficient: '证据不足',
  error: '核验出错',
};

export const EVIDENCE_ROLE = {
  primary_result: '主要结果',
  method: '方法',
  background: '背景',
  review_secondary: '二手综述',
  correction_notice: '更正声明',
};

export const SCOPE_MATCH = {
  match: '范围一致',
  partial: '部分覆盖',
  mismatch: '范围不匹配',
  unknown: '范围未知',
};

export const CLAIM_TYPE = {
  background: '背景',
  method: '方法',
  result: '结果',
  discussion: '讨论',
  topic: '主题',
};

export const REVOKE_REASON = {
  retracted: '论文撤稿',
  entitlement_revoked: '授权撤回',
  evidence_invalidated: '证据失效',
  manuscript_deleted: '稿件已删除',
};

export const TERMINATION_REASON = {
  all_completed: '全部 Worker 完成',
  deadline_reached: '到达截止时间',
  sufficient_results: '结果已充分',
  user_requested: '用户请求定稿',
  checkpoint_requested: '检查点',
  cancelled: '用户取消',
  coordinator_failed: '协调器失败',
  budget_exhausted: '预算耗尽',
};

export const SNAPSHOT_KIND = {
  checkpoint: '检查点',
  final: '最终快照',
  supplemental: '补充版本',
};

export const SOURCE_ROLE = {
  publisher_pdf: '出版社 PDF',
  publisher_api: '出版社 API',
  metadata_api: '元数据 API',
  review_synthesis: '综述合成',
  other: '其他来源',
};

export const SCOPE_FIELDS = {
  species: '物种',
  system: '系统',
  condition: '条件',
  dose: '剂量',
  method: '方法',
  population: '人群',
  free_text: '限定',
};

export function formatAuthors(rec) {
  if (!rec) return [];
  const raw = rec.authors || (rec.work && rec.work.canonical_authors) || (rec.paper && rec.paper.authors) || [];
  const values = typeof raw === 'string' ? raw.split(/\s*,\s*/) : raw;
  if (!Array.isArray(values)) return [];
  return values
    .map((author) => {
      if (typeof author === 'string') return author;
      if (!author || typeof author !== 'object') return '';
      if (author.family && author.given) return `${author.family} ${author.given}`;
      return author.family || author.name || author.full_name || '';
    })
    .filter(Boolean);
}


export const CONNECTION = {
  idle: '未连接',
  connecting: '连接中',
  live: '实时连接',
  reconnecting: '重连中',
  closed: '已关闭',
  exhausted: '重连失败',
  error: '连接错误',
};

export function label(table, value) {
  if (value == null || value === '') return '—';
  return table[value] || String(value);
}

export function groupLabel(group) {
  if (!group) return '未分组';
  if (group === 'evidence_only') return '证据已通过 · 分数不可比';
  if (group.includes('bootstrap')) return '启动排序组';
  if (group.startsWith('ssg:global-rerank')) return '统一重排组';
  if (group.startsWith('ssg:source-calibrated')) return '来源校准组';
  return group;
}

export function groupScaleNote(group, recs = []) {
  if (group === 'evidence_only') return '可引用，但分数不可比；不显示名次';
  const rec = recs.find((item) => item.display_score != null) || recs[0] || {};
  if (rec.calibration_status === 'bootstrap' || rec.score_kind === 'bootstrap') {
    return '启动排序，未概率校准；只在组内比较';
  }
  if (rec.calibration_status === 'calibrated') return '已校准；只在组内比较';
  if (rec.calibration_status === 'uncalibrated') return '未校准；只在组内比较';
  return '仅在组内比较，不跨组、不跨 claim';
}

export function formatScore(score) {
  if (typeof score !== 'number' || Number.isNaN(score)) return null;
  return score.toFixed(1);
}

export function formatDuration(ms) {
  if (typeof ms !== 'number' || ms < 0) return '—';
  if (ms < 1000) return `${Math.round(ms)} 毫秒`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} 秒`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} 分 ${Math.round(seconds % 60)} 秒`;
}

export function shortHash(value, head = 12) {
  if (!value) return '—';
  const text = String(value).replace(/^sha256:/, '');
  return text.length <= head + 1 ? text : `${text.slice(0, head)}…`;
}

export function formatClock(iso) {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  return date.toLocaleTimeString('zh-CN', { hour12: false });
}

export function formatDateTime(iso) {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  return date.toLocaleString('zh-CN', { hour12: false });
}

export function listOrDash(values) {
  if (!Array.isArray(values) || values.length === 0) return '—';
  return values.join(' / ');
}

export function isScopedGroupComparable(rec) {
  return Boolean(rec && rec.citable && rec.rankable && rec.rankable_group && rec.rankable_group !== 'evidence_only');
}

export function calibrateText(rec) {
  if (!rec) return '';
  if (rec.rankable_group === 'evidence_only' || !rec.rankable) {
    return '证据已通过，分数不可比';
  }
  if (rec.calibration_status === 'bootstrap' || rec.score_kind === 'bootstrap') {
    return '启动排序，未概率校准';
  }
  if (rec.calibration_status === 'calibrated') return '已校准';
  return '未校准';
}
