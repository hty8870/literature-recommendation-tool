import fs from 'node:fs';
import crypto from 'node:crypto';

const read = name => JSON.parse(fs.readFileSync(name, 'utf8'));
const sha = name => crypto.createHash('sha256').update(fs.readFileSync(name)).digest('hex');
const suite = read('tasks.json');
const tasks = {...suite, cases: suite.cases ?? suite.tasks};
delete tasks.tasks;
const corpus = read('corpus.json');
const excluded = new Set(read('excluded-target-ids.json').work_ids);
const records = new Map(corpus.records.map(r => [r.work_id, r]));
const issues = [];
const leakage = [];
const perCase = [];
const roleFields = ['required_ids', 'background_ids', 'forbidden_ids'];
const check = (ok, message) => { if (!ok) issues.push(message); };
const sameKeys = (obj, keys) => JSON.stringify(Object.keys(obj).sort()) === JSON.stringify([...keys].sort());
const nchar = s => Array.from(s).length;
const normalize = s => s.normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');
const plantCases = new Set(['v4-14', 'v4-16']);
const required = new Set();
const allRoles = new Set();
const quoteLengths = [];
const inputHashes = new Set();
check(sameKeys(tasks, ['suite_id','corpus_path','stability_case_ids','repetitions','tuning_case_ids','development_case_ids','cases']), 'suite keys');
check(tasks.suite_id === 'astra-independent-abstract-v4', 'suite_id');
check(tasks.corpus_path === 'data/runtime/quality-corpus/large-corpus.json', 'corpus_path');
check(tasks.cases.length === 16, 'case count');
check(new Set(tasks.cases.map(c => c.case_id)).size === 16, 'unique case ids');
check(records.size === corpus.records.length, 'duplicate corpus work ids');
check(tasks.tuning_case_ids.length === 0 && tasks.development_case_ids.length === 0, 'tuning/development');
check(tasks.repetitions === 3, 'repetitions');
check(tasks.stability_case_ids.length === 4 && new Set(tasks.stability_case_ids).size === 4, 'stability count');
const roleCounts = {required:0, background:0, forbidden:0};
for (const c of tasks.cases) {
  const tag = c.case_id;
  check(sameKeys(c, ['case_id','slice','baseline_query','request','expect']), `${tag}: case fields`);
  check(typeof c.slice === 'string' && c.slice.length > 0, `${tag}: slice`);
  const r = c.request;
  const e = c.expect;
  let input = '';
  if (r.input_type === 'keywords') {
    check(sameKeys(r,['input_type','keywords']), `${tag}: keywords fields`);
    check(Array.isArray(r.keywords) && r.keywords.length > 0 && r.keywords.every(s => typeof s === 'string' && s.length > 0), `${tag}: keywords values`);
    input = r.keywords.join(/[\u3400-\u9fff]/u.test(r.keywords.join('')) ? '；' : '; ');
  } else {
    check(r.input_type === 'abstract', `${tag}: input_type`);
    check(sameKeys(r,['input_type','abstract']), `${tag}: abstract fields`);
    check(typeof r.abstract === 'string' && r.abstract.length > 0, `${tag}: abstract value`);
    input = r.abstract;
  }
  check(c.baseline_query === input, `${tag}: baseline/request condition preservation`);
  const ih = crypto.createHash('sha256').update(input).digest('hex');
  check(!inputHashes.has(ih), `${tag}: duplicate base input`);
  inputHashes.add(ih);
  check(!/10\.\d{4,9}\/\S+|work:|manuscript_id/i.test(input), `${tag}: forbidden identifier leakage`);
  check(sameKeys(e,['required_ids','background_ids','forbidden_ids','gold_note','evidence_anchors']), `${tag}: expect fields`);
  check(typeof e.gold_note === 'string' && e.gold_note.length > 0, `${tag}: gold_note`);
  const seen = new Set();
  for (const field of roleFields) {
    const role = field.replace('_ids','');
    check(Array.isArray(e[field]), `${tag}: ${field} array`);
    for (const id of e[field]) {
      check(!seen.has(id), `${tag}: role overlap or duplicate: ${id}`);
      seen.add(id);
      check(records.has(id), `${tag}: unknown role id: ${id}`);
      check(!excluded.has(id), `${tag}: excluded role id: ${id}`);
      check(e.evidence_anchors.some(a => a.work_id === id && a.role === role), `${tag}: missing role anchor: ${id}`);
      if (role === 'required') required.add(id);
      allRoles.add(id);
      roleCounts[role]++;
    }
  }
  for (const a of e.evidence_anchors) {
    check(sameKeys(a,['work_id','role','field','quote','supports']), `${tag}: anchor fields`);
    check(['required','background','forbidden'].includes(a.role), `${tag}: anchor role`);
    check(e[`${a.role}_ids`]?.includes(a.work_id), `${tag}: anchor unlabelled/mismatched role`);
    check(['title','abstract'].includes(a.field), `${tag}: anchor field`);
    check(typeof a.quote === 'string' && nchar(a.quote) >= 24, `${tag}: short quote: ${a.work_id}`);
    check(records.get(a.work_id)?.[a.field]?.includes(a.quote), `${tag}: quote mismatch: ${a.work_id}: ${a.quote}`);
    check(typeof a.supports === 'string' && a.supports.length > 0, `${tag}: missing supports`);
    quoteLengths.push(nchar(a.quote));
  }
  for (const id of e.required_ids) {
    const doc = records.get(id);
    if (!doc) continue;
    const text = input.toLowerCase();
    const fullTitle = text.includes(doc.title.toLowerCase()) || normalize(input).includes(normalize(doc.title));
    const fullAbstract = text.includes(doc.abstract.toLowerCase());
    const anchorCopied = e.evidence_anchors.filter(a => a.work_id === id).some(a => text.includes(a.quote.toLowerCase()));
    let maxRun = 0;
    let example = '';
    for (let pos=0; pos<input.length; pos++) {
      if (!/[A-Za-z]/.test(input[pos])) continue;
      const prefix=input.slice(pos,pos+24).toLowerCase();
      if (prefix.length<24 || !doc.abstract.toLowerCase().includes(prefix)) continue;
      let end=pos+24;
      while(end<input.length && doc.abstract.toLowerCase().includes(input.slice(pos,end+1).toLowerCase())) end++;
      if(end-pos>maxRun){maxRun=end-pos;example=input.slice(pos,end);}
    }
    leakage.push({case_id:tag,work_id:id,full_title_copied:fullTitle,full_abstract_copied:fullAbstract,core_anchor_copied:anchorCopied,longest_exact_english_run_ge24:maxRun,example});
    check(!fullTitle && !fullAbstract && !anchorCopied, `${tag}: direct title/abstract/anchor leakage: ${id}`);
  }
  perCase.push({case_id:tag,input_type:r.input_type,chinese_input:/[\u3400-\u9fff]/u.test(input),non_plant_agriculture:!plantCases.has(tag),required_count:e.required_ids.length,background_count:e.background_ids.length,forbidden_count:e.forbidden_ids.length,anchor_count:e.evidence_anchors.length,roles:[...seen]});
}
for(const id of tasks.stability_case_ids) check(tasks.cases.some(c=>c.case_id===id), `unknown stability id ${id}`);
const positive=perCase.filter(c=>c.required_count>0).length;
const gaps=perCase.filter(c=>c.required_count===0).length;
const multi=perCase.filter(c=>c.required_count>1).map(c=>c.case_id);
const chinese=perCase.filter(c=>c.chinese_input).length;
const nonplant=perCase.filter(c=>c.non_plant_agriculture).length;
check(positive===14 && gaps===2, 'positive/gap counts');
check(multi.length>=2, 'multi-target count');
check(chinese>=4 && nonplant>=12, 'language/domain coverage');
check(new Set(perCase.map(c=>c.input_type)).size===2, 'input coverage');
const stability=perCase.filter(c=>tasks.stability_case_ids.includes(c.case_id));
check(new Set(stability.map(c=>c.input_type)).size===2, 'stability input coverage');
const total=tasks.cases.length + tasks.stability_case_ids.length*(tasks.repetitions-1);
check(total===24, 'total instances');
const gap15 = read('gap-15-screening.json');
const gap16 = read('gap-16-screening.json');
const sorted = ids => [...ids].sort();
const equalIds = (a,b) => JSON.stringify(sorted(a)) === JSON.stringify(sorted(b));
const txt = r => r.title + '\n' + r.abstract;
const matches15 = (name,r) => new RegExp(gap15.patterns[name], 'i').test(txt(r));
const ids15 = name => gap15.sets[name].work_ids || ids15(gap15.sets[name].work_ids_ref);
for(const [name,s] of Object.entries(gap15.sets)) {
  const found = corpus.records.filter(r => name === 'clinical_pubtype'
    ? ['E','L','R'].some(p=>matches15(p,r)) && r.pub_types.some(p=>/Clinical Trial|Randomized Controlled/i.test(p))
    : s.expression.split(' AND ').every(p=>matches15(p,r))).map(r=>r.work_id);
  check(found.length===s.count && equalIds(found,ids15(name)), `gap15: set ${name}`);
}
for(const s of gap16.searches) {
  const found = corpus.records.filter(r => s.mode === 'record_and'
    ? s.patterns.every(p=>[r.title,r.abstract].some(t=>new RegExp(p,s.flags).test(t)))
    : (s.mode === 'abstract_only' ? [r.abstract] : [r.title,r.abstract]).some(t=>new RegExp(s.pattern,s.flags).test(t))).map(r=>r.work_id);
  check(found.length===s.match_count && equalIds(found,s.matched_work_ids), `gap16: search ${s.id}`);
}
for(const c of tasks.cases) {
  for(const field of roleFields) for(const id of c.expect[field]) {
    const doc=records.get(id);
    check(!normalize(c.baseline_query).includes(normalize(doc.title)), `${c.case_id}: labelled full title leak ${id}`);
  }
}
const gapAudit={gap15_sets_verified:Object.keys(gap15.sets).length,gap16_searches_verified:gap16.searches.length,screening_universe:corpus.records.length,exclusions_removed_before_screening:0};
console.log(JSON.stringify({
  scope:'local_author_checks_not_independent_final_review',
  status:issues.length?'issues_found':'no_local_structural_issues',
  counts:{corpus_records:corpus.records.length,excluded_ids:excluded.size,base_cases:tasks.cases.length,positive_cases:positive,gap_cases:gaps,required_unique:required.size,role_assignments:roleCounts,role_unique:allRoles.size,anchors:quoteLengths.length,min_quote_characters:Math.min(...quoteLengths),multi_target_cases:multi,non_plant_agriculture_cases:nonplant,chinese_inputs:chinese,keywords:perCase.filter(c=>c.input_type==='keywords').length,abstract:perCase.filter(c=>c.input_type==='abstract').length,stability_cases:stability.length,repetitions:tasks.repetitions,instances_per_system:total},
  hashes:Object.fromEntries(['AUTHORING_BRIEF.md','corpus.json','excluded-target-ids.json','tasks.json','gap-15-screening.json','gap-16-screening.json','validate-authoring.mjs'].map(name=>[name,sha(name)])),
  gap_screening:gapAudit,
  issues,
  leakage,
  per_case:perCase,
  arithmetic:{cyp2d6_confirmation_percent:24/25*100,ldct_lung_cancer_relative_reduction_percent:(1-0.79)*100,ldct_all_cause_relative_reduction_percent:(1-0.95)*100,ngf_groups_total:8+2,co_rate_ratio:27.2/10.3,percentage_addition_for_wue:'not_performed_missing_common_reference'},
  semantic_caveat:'领域归类、多目标必要性和科学边界由作者逐案判断；字符串与计数检查不能认证科学正确性。没有运行被测系统。'
},null,2));
process.exitCode=issues.length?1:0;
