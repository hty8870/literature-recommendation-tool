import { formatAuthors } from './labels.js';

export function primarySource(rec) {
  return Array.isArray(rec.sources) && rec.sources.length ? rec.sources[0] : null;
}

export function resultTitle(rec) {
  const source = primarySource(rec);
  return rec.title || rec.work?.canonical_title || rec.paper?.title || source?.title || '标题未随推荐返回';
}

export function resultMetadata(rec) {
  const source = primarySource(rec);
  return {
    authors: formatAuthors(rec),
    title: resultTitle(rec),
    year: rec.year || rec.work?.canonical_year || rec.paper?.year || source?.year || null,
    venue: rec.venue || rec.journal || rec.container_title || rec.work?.venue || null,
    doi: rec.doi || rec.work?.doi_normalized || source?.doi || null,
  };
}

export function formatCitationInfo(rec, libraryNames = new Map()) {
  const metadata = resultMetadata(rec);
  const details = [];
  if (metadata.year) details.push(`(${metadata.year})`);
  if (metadata.venue) details.push(metadata.venue);
  if (metadata.doi) details.push(`DOI: ${metadata.doi}`);
  const authors = metadata.authors.length ? `${metadata.authors.join(', ')}. ` : '';
  const suffix = details.length ? `. ${details.join('. ')}` : '';
  const sourceNames = (rec.sources || []).map((source) => source.display_name || source.library_id);
  for (const id of rec.library_ids || []) sourceNames.push(libraryNames.get(id) || id);
  const sources = [...new Set(sourceNames.filter(Boolean))];
  const sourceLine = sources.length ? `\n来源库：${sources.join('；')}` : '';
  const isCandidate = Object.hasOwn(rec, 'candidate_score') || Object.hasOwn(rec, 'evidence_excerpt');
  const simulation = rec.simulation === true || (rec.simulation == null && !isCandidate)
    ? '【本地模拟数据，请勿用于真实引用】\n' : '';
  return `${simulation}${authors}${metadata.title}${suffix}${sourceLine}`;
}
