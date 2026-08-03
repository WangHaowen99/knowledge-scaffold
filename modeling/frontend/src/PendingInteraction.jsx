import React, { useEffect, useMemo, useState } from 'react';
import ArtifactDiff from './ArtifactDiff.jsx';
import { safeUiError } from './revision-state.js';
import { repairTargetCandidatesFrom, withRepairTarget } from './repairTargetChoice.js';
import { isSurfacedAsBlocker } from './completionBlocker.js';

const TYPE_TITLE = {
  question: '需要确认',
  change_plan: '修改方案',
  decision: '请选择',
  artifact: '内容审阅',
  finding: '问题处理',
  knowledge: '规则确认',
  flowchart: '流程图确认',
  scenario: '场景检查',
};

const ACTION_LABEL = { add: '新增', added: '新增', create: '新增', modify: '修改', modified: '修改', update: '修改', remove: '删除', delete: '删除', deleted: '删除' };
const TARGET_LABEL = { object: '对象', operation: '操作', rule: '规则', metric: '指标', action: '动作', script: '脚本', file: '文件' };
const FINDING_LEVEL = { error: '错误', warning: '警告', info: '提示', 错误: '错误', 警告: '警告', 提示: '提示' };
const RESULT_LABEL = { passed: '通过', pass: '通过', partial: '部分通过', failed: '未通过', fail: '未通过', error: '出错' };
const TYPE_ALIASES = {
  question_batch: 'question',
  artifact_review: 'artifact',
  finding_review: 'finding',
  knowledge_conflict: 'knowledge',
  flowchart_review: 'flowchart',
  scenario_review: 'scenario',
};

const styles = {
  shell: { minWidth: 0, color: '#101828' },
  header: { paddingBottom: 10, borderBottom: '1px solid #eaecf0' },
  eyebrow: { color: '#667085', fontSize: 11.5 },
  title: { margin: '3px 0 0', fontSize: 15, lineHeight: 1.45, overflowWrap: 'anywhere' },
  description: { margin: '6px 0 0', color: '#475467', fontSize: 12.5, lineHeight: 1.6, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' },
  section: { padding: '12px 0', borderBottom: '1px solid #eaecf0' },
  sectionTitle: { marginBottom: 7, color: '#344054', fontSize: 12.5, fontWeight: 700, lineHeight: 1.5 },
  hint: { color: '#667085', fontSize: 11.5, lineHeight: 1.5 },
  option: { display: 'flex', alignItems: 'flex-start', gap: 8, padding: '6px 0', color: '#344054', fontSize: 12.5, lineHeight: 1.45, cursor: 'pointer' },
  optionText: { minWidth: 0, overflowWrap: 'anywhere' },
  optionNote: { display: 'block', marginTop: 2, color: '#667085', fontSize: 11.5 },
  input: { width: '100%', boxSizing: 'border-box', border: '1px solid #d0d5dd', borderRadius: 6, padding: '8px 9px', color: '#101828', background: '#fff', font: 'inherit', fontSize: 12.5, resize: 'vertical' },
  rows: { borderTop: '1px solid #eaecf0' },
  row: { display: 'grid', gridTemplateColumns: 'auto minmax(0, 1fr)', gap: 8, padding: '8px 0', borderBottom: '1px solid #eaecf0' },
  tag: { alignSelf: 'start', borderRadius: 4, padding: '2px 6px', background: '#f2f4f7', color: '#475467', fontSize: 11, fontWeight: 650 },
  rowTitle: { color: '#344054', fontSize: 12.5, fontWeight: 650, overflowWrap: 'anywhere' },
  rowText: { marginTop: 3, color: '#667085', fontSize: 12, lineHeight: 1.5, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' },
  changeCurrent: { display: 'block', marginTop: 4, padding: '6px 8px', borderRadius: 6, border: '1px solid #eaecf0', background: '#f9fafb', color: '#475467', fontSize: 11.5, lineHeight: 1.5, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' },
  changeCurrentLabel: { display: 'block', marginBottom: 3, color: '#98a2b3', fontSize: 10.5, fontStyle: 'normal' },
  content: { padding: 10, maxHeight: 260, overflow: 'auto', border: '1px solid #eaecf0', borderRadius: 6, background: '#f9fafb', color: '#344054', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 11.5, lineHeight: 1.55, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' },
  footer: { display: 'flex', alignItems: 'center', gap: 8, paddingTop: 12, flexWrap: 'wrap' },
  button: { padding: '5px 12px', fontSize: 12 },
  quietButton: { marginLeft: 'auto', padding: '5px 10px', fontSize: 11.5 },
  error: { marginTop: 10, padding: '7px 9px', borderRadius: 6, background: '#fef3f2', color: '#b42318', fontSize: 12 },
  processing: { display: 'flex', gap: 8, alignItems: 'center', marginBottom: 10, padding: '8px 10px', borderRadius: 8, border: '1px solid #dce7f6', background: '#f7fbff', color: '#185da7', fontSize: 12.5 },
  repairTarget: { marginTop: 10, padding: '10px 11px', borderRadius: 8, border: '1px solid #dce7f6', background: '#f7fbff' },
  repairTargetTitle: { marginBottom: 8, color: '#185da7', fontSize: 12.5, fontWeight: 650 },
  repairTargetOption: { display: 'flex', gap: 8, alignItems: 'baseline', marginBottom: 6, fontSize: 12.5, cursor: 'pointer' },
  repairTargetKind: { marginLeft: 7, color: '#748195', fontSize: 11, fontStyle: 'normal' },
  repairTargetSubmit: { marginTop: 4 },
  state: { padding: '18px 4px', textAlign: 'center', color: '#667085', fontSize: 13 },
};

const asArray = (value) => (Array.isArray(value) ? value : []);
const asText = (value) => {
  if (value == null) return '';
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) return value.map(asText).filter(Boolean).join('\n');
  return String(value.text ?? value.summary ?? value.description ?? value.name ?? JSON.stringify(value, null, 2));
};

const optionValue = (option, index) => typeof option === 'object'
  ? String(option.value ?? option.id ?? option.key ?? index)
  : String(option);

const optionLabel = (option) => typeof option === 'object'
  ? asText(option.label ?? option.name ?? option.text ?? option.value)
  : asText(option);

const optionNote = (option) => {
  if (typeof option !== 'object') return '';
  const label = optionLabel(option).replace(/\s+/g, ' ').trim();
  const note = asText(option.description ?? option.note).replace(/\s+/g, ' ').trim();
  return note && note !== label ? note : '';
};

const isControlledRepair = (payload) => payload?.purpose === 'controlled_repair_expert';

function friendlyRepairOption(option, index) {
  const source = `${optionLabel(option)} ${optionNote(option)}`;
  let label = optionLabel(option);
  let description = optionNote(option);
  if (label === '采用校验建议') {
    description = '按当前发现的问题调整规则，具体技术修改由系统完成。';
  } else if (/(?:查询|核对|验证).*(?:实际值|数据|证据)|补充数据证据/u.test(source)) {
    label = '先核对实际数据';
    description = '先确认相关记录是否真的一致，确认后再继续计算。';
  } else if (/(?:若|如果).*(?:不等|不同)|LogicTankID|vgoLineDestination|按.*(?:罐|对象).*(?:匹配|路由|取值)/iu.test(source)) {
    label = '按对应储罐分别取值';
    description = '如果记录不一致，就按每个储罐对应的数据分别计算。';
  } else if (/(?:参数|显式输入|枚举)/u.test(source)) {
    label = '由使用者明确填写';
    description = '把这个不确定内容变成必填选项，使用时由业务人员确认。';
  } else if (/(?:专家规则|记录专家|专家确认)/u.test(source)) {
    label = '按专家确认的规则处理';
    description = '将专家确认的结论作为同类场景的通用规则。';
  } else if (/(?:保守|不作推断|停止|暂不)/u.test(source)) {
    label = '资料不足时暂停自动处理';
    description = '遇到不确定情况时不自行猜测，直接提示人工确认。';
  } else if (/[A-Za-z][A-Za-z0-9_]{2,}|\b(?:content|assert|script|table)\b/iu.test(source)) {
    label = `处理方式 ${index + 1}`;
    description = '选择后系统会按该方案调整规则，技术细节无需专家处理。';
  }
  return typeof option === 'object' ? { ...option, label, description } : { value: option, label, description };
}

function friendlyRepairQuestion(question, payload) {
  if (!isControlledRepair(payload)) return question;
  const generatedOptions = asArray(payload.finding?.expertOptions).map((option) => ({
    label: asText(option?.label),
    description: asText(option?.description),
    value: asText(option?.decision),
  })).filter((option) => option.label && option.description && option.value);
  return {
    ...question,
    q: payload.finding?.userSuggestion || '请选择最符合实际业务的处理方式：',
    options: (generatedOptions.length ? generatedOptions : asArray(question.options)).map(friendlyRepairOption),
    otherLabel: '其他处理方式',
    otherPlaceholder: '请用业务语言说明应该怎么处理',
  };
}

function interactionPresentation(interaction) {
  const payload = interaction?.payload || {};
  if (!isControlledRepair(payload)) {
    return {
      title: interaction?.title || payload.title,
      description: interaction?.description || payload.description,
    };
  }
  const category = asText(payload.finding?.category);
  const friendlyCategory = /(?:无证据|证据与假设|证据边界)/u.test(category)
    ? '资料依据不足'
    : category || '这条业务规则';
  const description = asText(payload.finding?.userMessage).trim()
    || (friendlyCategory === '资料依据不足'
      ? '现有资料还不足以证明这条业务规则一定成立。如果直接使用，可能导致计算或判断不准确。'
      : '这条业务规则还需要您确认实际处理方式。');
  return { title: `请确认“${friendlyCategory}”怎么处理`, description };
}

const changeId = (change, index) => String(change.id ?? change.changeId ?? change.change_id ?? index);
const interactionType = (value) => TYPE_ALIASES[value] || value;

function legacyRepairOptions(payload) {
  if (payload.purpose !== 'controlled_repair_expert') return [];
  const suggestion = asText(payload.finding?.suggestion).trim();
  if (!suggestion) return [];
  const branches = suggestion
    .replace(/^[^：:\n]{0,24}[：:]/, '')
    .replace(/(?:建议|请)与业务确认后修正[。.]?\s*$/u, '')
    .split(/[；;\n]+/u)
    .map((item) => item.trim().replace(/[。.]$/u, ''))
    .filter((item) => /^(?:若|如果).+?(?:，|,)?则.+/u.test(item));
  if (branches.length < 2) {
    const normalized = suggestion
      .replace(/^[^：:\n]{0,24}[：:]/, '')
      .replace(/(?:建议|请)与业务确认后修正[。.]?\s*$/u, '')
      .trim();
    return normalized ? [{
      label: '采用校验建议',
      value: normalized,
      description: normalized,
    }] : [];
  }
  return branches.slice(0, 4).map((branch) => {
    const matched = branch.match(/^(?:若|如果)\s*(.+?)(?:，|,)?则\s*(.+)$/u);
    return {
      label: matched?.[1]?.trim() || branch,
      value: branch,
      description: matched?.[2]?.trim() || branch,
    };
  });
}

function questionsFrom(payload, interaction) {
  const source = asArray(payload.questions);
  if (source.length) {
    const options = legacyRepairOptions(payload);
    if (options.length >= 1 && String(source[0]?.kind ?? '') === 'text') {
      return [{
        ...source[0],
        kind: 'single',
        q: '请选择应采用的业务口径；没有合适项时再填写“其他”。',
        allowOther: true,
        options,
      }, ...source.slice(1)].map((question) => friendlyRepairQuestion(question, payload));
    }
    return source.map((question) => friendlyRepairQuestion(question, payload));
  }
  return [{
    id: 'answer',
    q: payload.prompt || interaction.title || '请填写',
    kind: payload.kind || payload.inputType || payload.input_type || 'text',
    options: payload.options,
    required: payload.required,
    allowSkip: payload.allowSkip,
  }];
}

function initialDraft(interaction) {
  const payload = interaction?.payload || {};
  const type = interactionType(interaction?.type);
  if (type === 'question') {
    const answers = {};
    for (const question of questionsFrom(payload, interaction)) {
      const id = String(question.id ?? question.key ?? Object.keys(answers).length);
      const defaultValue = question.defaultValue ?? question.default_value ?? question.selected;
      const defaults = Array.isArray(defaultValue) ? defaultValue : defaultValue == null ? [] : [defaultValue];
      answers[id] = {
        selected: defaults.map(String),
        other: asText(question.other),
        skipped: false,
      };
    }
    return { answers };
  }
  if (type === 'change_plan') {
    return { selected: asArray(payload.changes).map(changeId), note: '' };
  }
  if (type === 'decision') {
    return { value: asText(payload.defaultValue ?? payload.default_value), custom: '', note: '' };
  }
  if (type === 'knowledge') {
    return {
      action: asText(payload.defaultAction ?? payload.default_action),
      statement: [asText(payload.existing || payload.current), asText(payload.statement)].filter(Boolean).join('\n'),
      key: '',
      note: '',
    };
  }
  return { note: '', acknowledgeWarnings: false };
}

function NoteField({ value, onChange, placeholder = '补充说明（可选）' }) {
  return (
    <textarea
      rows={2}
      style={styles.input}
      value={value || ''}
      placeholder={placeholder}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

function ActionButton({ primary = false, children, ...props }) {
  return <button type="button" className={primary ? 'btn btn-p' : 'btn'} style={styles.button} {...props}>{children}</button>;
}

function QuestionForm({ interaction, draft, setDraft, busy, submit }) {
  const payload = interaction.payload || {};
  const questions = questionsFrom(payload, interaction);

  const update = (id, patch) => setDraft((current) => ({
    ...current,
    answers: {
      ...current.answers,
      [id]: { selected: [], other: '', skipped: false, ...current.answers?.[id], ...patch },
    },
  }));

  const toggle = (id, value, multiple) => {
    const answer = draft.answers?.[id] || { selected: [] };
    const selected = multiple
      ? (answer.selected.includes(value) ? answer.selected.filter((item) => item !== value) : [...answer.selected, value])
      : [value];
    update(id, { selected, skipped: false, ...(!multiple && value !== '__other__' ? { other: '' } : {}) });
  };

  const complete = questions.every((question, index) => {
    if (question.required === false) return true;
    const id = String(question.id ?? question.key ?? index);
    const answer = draft.answers?.[id];
    return answer?.skipped || answer?.selected?.length || answer?.other?.trim();
  });

  const send = () => {
    const answers = questions.map((question, index) => {
      const questionId = String(question.id ?? question.key ?? index);
      const answer = draft.answers?.[questionId] || { selected: [], other: '', skipped: false };
      return {
        questionId,
        selected: answer.selected.filter((value) => value !== '__other__'),
        other: (!asArray(question.options).length || answer.selected.includes('__other__'))
          ? answer.other.trim() || undefined
          : undefined,
        skipped: Boolean(answer.skipped),
      };
    });
    submit({ answers });
  };

  return (
    <>
      {questions.map((question, index) => {
        const id = String(question.id ?? question.key ?? index);
        const answer = draft.answers?.[id] || { selected: [], other: '', skipped: false };
        const options = asArray(question.options);
        const kind = String(question.kind ?? question.inputType ?? question.input_type ?? (options.length ? 'single' : 'text'));
        const multiple = ['multiple', 'multi', 'checkbox'].includes(kind);
        const allowSkip = question.allowSkip ?? question.allow_skip ?? payload.allowSkip ?? payload.allow_skip ?? false;
        const allowOther = question.allowOther ?? question.allow_other ?? false;
        const processSelection = id === 'selected_processes';
        const otherLabel = asText(question.otherLabel ?? question.other_label)
          || (processSelection ? '补充其他流程' : '其他');
        const otherPlaceholder = asText(question.otherPlaceholder ?? question.other_placeholder)
          || (processSelection ? '请输入补充说明，下一步再确认流程名称和细节' : '请输入');
        return (
          <fieldset key={id} style={{ ...styles.section, margin: 0, borderLeft: 0, borderRight: 0, borderTop: 0 }} disabled={busy}>
            <legend style={styles.sectionTitle}>{index + 1}. {asText(question.q ?? question.prompt ?? question.text ?? '请填写')}</legend>
            {(question.reason || question.help) && <div style={styles.hint}>{asText(question.reason || question.help)}</div>}
            {options.map((option, optionIndex) => {
              const value = optionValue(option, optionIndex);
              return (
                <label key={value} style={styles.option}>
                  <input
                    type={multiple ? 'checkbox' : 'radio'}
                    name={`question-${interaction.id}-${id}`}
                    checked={answer.selected.includes(value)}
                    onChange={() => toggle(id, value, multiple)}
                  />
                  <span style={styles.optionText}>
                    {optionLabel(option)}
                    {optionNote(option) && <span style={styles.optionNote}>{optionNote(option)}</span>}
                  </span>
                </label>
              );
            })}
            {allowOther && options.length > 0 && (
              <label style={styles.option}>
                <input
                  type={multiple ? 'checkbox' : 'radio'}
                  name={`question-${interaction.id}-${id}`}
                  checked={answer.selected.includes('__other__')}
                  onChange={() => toggle(id, '__other__', multiple)}
                />
                <span>{otherLabel}</span>
              </label>
            )}
            {(!options.length || answer.selected.includes('__other__')) && (
              <textarea
                rows={id === 'goal_text' ? 5 : 2}
                style={styles.input}
                data-question-id={id}
                value={answer.other}
                placeholder={id === 'goal_text'
                  ? '请描述要解决的问题、使用者、典型场景和期望结果'
                  : otherPlaceholder}
                onChange={(event) => update(id, { other: event.target.value, skipped: false })}
              />
            )}
            {allowSkip && (
              <button
                type="button"
                className="btn"
                style={{ ...styles.button, marginTop: 7 }}
                onClick={() => update(id, answer.skipped ? { skipped: false } : { selected: [], other: '', skipped: true })}
              >
                {answer.skipped ? '重新填写' : '跳过此题'}
              </button>
            )}
          </fieldset>
        );
      })}
      <div style={styles.footer}>
        <ActionButton primary disabled={busy || !complete} onClick={send}>{busy ? '提交中...' : '提交答案'}</ActionButton>
      </div>
    </>
  );
}

function ChangePlan({ interaction, draft, setDraft, busy, submit }) {
  const payload = interaction.payload || {};
  const changes = asArray(payload.changes);
  const selected = new Set(draft.selected || []);
  const toggle = (id) => setDraft((current) => ({
    ...current,
    selected: selected.has(id) ? current.selected.filter((item) => item !== id) : [...current.selected, id],
  }));

  return (
    <>
      {payload.summary && <div style={styles.section}>{asText(payload.summary)}</div>}
      <div style={styles.rows}>
        {changes.map((change, index) => {
          const id = changeId(change, index);
          return (
            <label key={id} style={{ ...styles.row, cursor: 'pointer' }}>
              <input type="checkbox" checked={selected.has(id)} disabled={busy || change.required === true} onChange={() => toggle(id)} />
              <span>
                <span style={styles.rowTitle}>
                  {ACTION_LABEL[change.action] || '修改'} · {TARGET_LABEL[change.targetKind ?? change.target_kind] || '内容'} {asText(change.targetKey ?? change.target_key ?? change.path)}
                </span>
                {/* 修改类先给出当前内容：不看见现在是什么样，「怎么改」无从判断 */}
                {change.current && (
                  <span style={styles.changeCurrent}>
                    <em style={styles.changeCurrentLabel}>当前内容</em>
                    {asText(change.current)}
                  </span>
                )}
                <span style={styles.rowText}>
                  {change.current ? '计划这样改：' : ''}{asText(change.description ?? change.summary)}
                </span>
              </span>
            </label>
          );
        })}
      </div>
      {asArray(payload.risks).length > 0 && (
        <div style={styles.section}>
          <div style={styles.sectionTitle}>注意事项</div>
          {asArray(payload.risks).map((risk, index) => <div key={index} style={styles.rowText}>{asText(risk)}</div>)}
        </div>
      )}
      <div style={styles.section}><NoteField value={draft.note} onChange={(note) => setDraft((current) => ({ ...current, note }))}
        placeholder={payload.purpose === 'package_chat' ? '补充说明；调整方案时请填写修改意见' : undefined} /></div>
      <div style={styles.footer}>
        <ActionButton primary disabled={busy || !selected.size} onClick={() => submit({ action: 'approve', selectedChangeIds: [...selected], note: draft.note.trim() || undefined })}>
          {busy ? '提交中...' : `确认修改${selected.size ? `（${selected.size} 项）` : ''}`}
        </ActionButton>
        {payload.purpose === 'package_chat' && (
          <ActionButton disabled={busy || !draft.note.trim()} onClick={() => submit({ action: 'revise', note: draft.note.trim() })}>调整方案</ActionButton>
        )}
        <ActionButton disabled={busy} onClick={() => submit({ action: 'reject', note: draft.note.trim() || undefined })}>不修改</ActionButton>
      </div>
    </>
  );
}

function Decision({ interaction, draft, setDraft, busy, submit }) {
  const payload = interaction.payload || {};
  const options = asArray(payload.options);
  const allowCustom = payload.allowCustom ?? payload.allow_custom ?? !options.length;
  const selected = draft.value;
  return (
    <>
      {payload.prompt && <div style={styles.sectionTitle}>{asText(payload.prompt)}</div>}
      <div style={styles.section}>
        {options.map((option, index) => {
          const value = optionValue(option, index);
          return (
            <label key={value} style={styles.option}>
              <input type="radio" name={`decision-${interaction.id}`} checked={selected === value} disabled={busy} onChange={() => setDraft((current) => ({ ...current, value }))} />
              <span style={styles.optionText}>{optionLabel(option)}{optionNote(option) && <span style={styles.optionNote}>{optionNote(option)}</span>}</span>
            </label>
          );
        })}
        {allowCustom && (
          <textarea rows={2} style={styles.input} value={draft.custom || ''} placeholder="其他选择" disabled={busy} onChange={(event) => setDraft((current) => ({ ...current, value: '', custom: event.target.value }))} />
        )}
      </div>
      <div style={styles.section}><NoteField value={draft.note} onChange={(note) => setDraft((current) => ({ ...current, note }))} /></div>
      <div style={styles.footer}>
        <ActionButton primary disabled={busy || !(selected || draft.custom?.trim())} onClick={() => submit({ value: selected || draft.custom.trim(), note: draft.note.trim() || undefined })}>
          {busy ? '提交中...' : '确认'}
        </ActionButton>
      </div>
    </>
  );
}

function ReviewContent({ payload, onOpenFile }) {
  return (
    <>
      {(payload.name || payload.path || payload.summary) && (
        <div style={styles.section}>
          {(payload.name || payload.path) && <div style={styles.sectionTitle}>{asText(payload.name || payload.path)}</div>}
          {payload.summary && <div style={styles.rowText}>{asText(payload.summary)}</div>}
        </div>
      )}
      {payload.diff && <div style={styles.section}><ArtifactDiff diff={payload.diff} onOpenFile={onOpenFile} /></div>}
      {payload.content && <pre style={styles.content}>{asText(payload.content)}</pre>}
    </>
  );
}

function ThreeWayReview({ interaction, draft, setDraft, busy, submit, mode, onOpenFile }) {
  const payload = interaction.payload || {};
  const labels = mode === 'finding'
    ? { accept: '采用建议', revise: '请调整', reject: '忽略' }
    : mode === 'scenario' || mode === 'flowchart'
      ? { accept: '通过', revise: '需调整', reject: '不通过' }
      : { accept: '通过', revise: '请修改', reject: '不采用' };
  const actions = mode === 'finding'
    ? { accept: 'apply', revise: 'revise', reject: 'dismiss' }
    : { accept: 'accept', revise: 'revise', reject: 'reject' };

  return (
    <>
      {mode === 'artifact' && <ReviewContent payload={payload} onOpenFile={onOpenFile} />}
      {mode === 'finding' && (
        <>
          <div style={styles.section}>
            <span style={styles.tag}>{FINDING_LEVEL[payload.severity] || '提示'}</span>
            {payload.target && <div style={{ ...styles.sectionTitle, marginTop: 7 }}>{asText(payload.target)}</div>}
            <div style={styles.rowText}>{asText(payload.message)}</div>
          </div>
          {payload.suggestion && <div style={styles.section}><div style={styles.sectionTitle}>建议</div><div style={styles.rowText}>{asText(payload.suggestion)}</div></div>}
          {payload.diff && <div style={styles.section}><ArtifactDiff diff={payload.diff} onOpenFile={onOpenFile} /></div>}
        </>
      )}
      {mode === 'scenario' && <ScenarioDetails payload={payload} />}
      {mode === 'flowchart' && <FlowchartReviewDetails payload={payload} />}
      <div style={styles.section}>
        <NoteField value={draft.note} onChange={(note) => setDraft((current) => ({ ...current, note }))} placeholder={mode === 'finding' ? '处理说明（可选）' : '修改要求（可选）'} />
      </div>
      <div style={styles.footer}>
        <ActionButton primary disabled={busy} onClick={() => submit({ action: actions.accept, note: draft.note.trim() || undefined })}>{busy ? '提交中...' : labels.accept}</ActionButton>
        <ActionButton disabled={busy || !draft.note.trim()} onClick={() => submit({ action: actions.revise, note: draft.note.trim() })}>{labels.revise}</ActionButton>
        <ActionButton disabled={busy || (mode !== 'artifact' && !draft.note.trim())} onClick={() => submit({ action: actions.reject, note: draft.note.trim() || undefined })}>{labels.reject}</ActionButton>
      </div>
    </>
  );
}

function FlowchartDraftReview({ draft, setDraft, busy, submit, flowchartDraftState }) {
  const revision = flowchartDraftState?.draftRevision;
  const dirty = Boolean(flowchartDraftState?.dirty);
  const saving = flowchartDraftState?.saveStatus === 'saving';
  const loading = Boolean(flowchartDraftState?.loading);
  const diagramCount = Number(flowchartDraftState?.diagramCount ?? 0);
  const warnings = asArray(flowchartDraftState?.warnings);
  const warningsAcknowledged = warnings.length === 0 || Boolean(draft.acknowledgeWarnings);
  const canSubmit = Number.isInteger(revision) && revision > 0
    && diagramCount > 0
    && !dirty && !saving && !loading && !flowchartDraftState?.error && warningsAcknowledged;
  const status = loading
    ? '正在加载流程图草稿…'
    : dirty
      ? '中间画布还有未保存修改，请先保存草稿。'
      : diagramCount < 1
        ? '当前没有业务流程，请新建并保存至少一个流程后再提交。'
        : Number.isInteger(revision) ? `流程图草稿 v${revision} 已保存` : '流程图草稿尚未保存';
  return (
    <>
      <div style={styles.section}>
        <div style={styles.sectionTitle}>提交流程图</div>
        <div style={styles.rowText}>{status}</div>
        {warnings.length > 0 && (
          <div style={{ marginTop: 9 }}>
            <div style={styles.hint}>提交前请逐条检查以下非阻断提示：</div>
            <ul aria-label="流程图警告" style={{ margin: '7px 0', paddingLeft: 20 }}>
              {warnings.map((warning, index) => (
                <li key={asText(warning?.code) || index} style={styles.rowText}>
                  {asText(warning?.message ?? warning)}
                </li>
              ))}
            </ul>
            <label style={styles.option}>
              <input type="checkbox" data-flowchart-warning-ack
                checked={Boolean(draft.acknowledgeWarnings)}
                onChange={(event) => setDraft((current) => ({
                  ...current,
                  acknowledgeWarnings: event.target.checked,
                }))} />
              <span style={styles.optionText}>我已检查并接受以上提示</span>
            </label>
          </div>
        )}
      </div>
      <div style={styles.footer}>
        <ActionButton primary disabled={busy || !canSubmit}
          onClick={() => submit({
            action: 'accept',
            draftRevision: revision,
            acknowledgeWarnings: warnings.length > 0 ? true : undefined,
          })}>
          {busy ? '提交中...' : '提交全部已保存流程图'}
        </ActionButton>
      </div>
      {flowchartDraftState?.error && <div role="alert" style={styles.error}>{flowchartDraftState.error}</div>}
    </>
  );
}

function FlowchartReviewDetails({ payload }) {
  const diagrams = asArray(payload.diagrams);
  return (
    <div style={styles.section}>
      <div style={styles.sectionTitle}>待确认流程</div>
      <div style={styles.rows}>
        {diagrams.map((diagram, index) => {
          const steps = asArray(diagram.nodes)
            .filter((node) => node?.kind === 'action' || node?.kind === 'decision');
          return (
            <div key={asText(diagram.id) || index} style={styles.row}>
              <span style={styles.tag}>{index + 1}</span>
              <div>
                <div style={styles.rowTitle}>{asText(diagram.title) || `流程 ${index + 1}`}</div>
                <div style={styles.rowText}>{steps.map((step, stepIndex) => `${stepIndex + 1}. ${asText(step.label)}`).join('\n')}</div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function ScenarioDetails({ payload }) {
  const rows = [
    ['输入', payload.input],
    ['预期', payload.expected],
    ['结果', payload.actual ?? payload.output],
  ].filter(([, value]) => value !== undefined && value !== null && value !== '');
  return (
    <>
      {(payload.name || payload.status) && (
        <div style={styles.section}>
          {payload.name && <div style={styles.sectionTitle}>{asText(payload.name)}</div>}
          {payload.status && <span style={styles.tag}>{RESULT_LABEL[payload.status] || '待确认'}</span>}
        </div>
      )}
      {rows.map(([label, value]) => <div key={label} style={styles.section}><div style={styles.sectionTitle}>{label}</div><div style={styles.rowText}>{asText(value)}</div></div>)}
      {asArray(payload.steps).length > 0 && (
        <div style={styles.section}>
          <div style={styles.sectionTitle}>过程</div>
          {asArray(payload.steps).map((step, index) => <div key={index} style={styles.rowText}>{index + 1}. {asText(step)}</div>)}
        </div>
      )}
      {asArray(payload.checks).length > 0 && (
        <div style={styles.section}>
          <div style={styles.sectionTitle}>推演变体</div>
          <div style={styles.rows}>{asArray(payload.checks).map((check, index) => (
            <div key={`${asText(check.kind)}-${index}`} style={styles.row}>
              <span style={styles.tag}>{index + 1}</span>
              <div>
                <div style={styles.rowTitle}>{asText(check.title)}</div>
                <div style={styles.rowText}>输入：{asText(check.input)}{`\n`}预期：{asText(check.expected)}</div>
              </div>
            </div>
          ))}</div>
        </div>
      )}
      {payload.coverage && (
        <div style={styles.section}>
          <div style={styles.sectionTitle}>知识覆盖</div>
          <div style={styles.rowText}>{[
            ...asArray(payload.coverage.rules).map((item) => `规则 ${asText(item)}`),
            ...asArray(payload.coverage.metrics).map((item) => `指标 ${asText(item)}`),
            ...asArray(payload.coverage.scripts).map((item) => `脚本 ${asText(item)}`),
          ].join(' · ') || '当前操作未引用额外规则、指标或脚本'}</div>
        </div>
      )}
    </>
  );
}

function KnowledgeChoice({ interaction, draft, setDraft, busy, submit }) {
  const payload = interaction.payload || {};
  const options = asArray(payload.options).length ? payload.options : [
    { value: 'merge', label: '合并' },
    { value: 'replace', label: '替换原内容' },
    { value: 'add', label: '都保留' },
    { value: 'cancel', label: '取消' },
  ];
  return (
    <>
      {payload.statement && <div style={styles.section}><div style={styles.sectionTitle}>新内容</div><div style={styles.rowText}>{asText(payload.statement)}</div></div>}
      {(payload.existing || payload.current) && <div style={styles.section}><div style={styles.sectionTitle}>已有内容</div><div style={styles.rowText}>{asText(payload.existing || payload.current)}</div></div>}
      <div style={styles.section}>
        {options.map((option, index) => {
          const value = optionValue(option, index);
          return (
            <label key={value} style={styles.option}>
              <input type="radio" name={`knowledge-${interaction.id}`} checked={draft.action === value} disabled={busy} onChange={() => setDraft((current) => ({ ...current, action: value }))} />
              <span style={styles.optionText}>{optionLabel(option)}{optionNote(option) && <span style={styles.optionNote}>{optionNote(option)}</span>}</span>
            </label>
          );
        })}
      </div>
      {draft.action === 'merge' && (
        <div style={styles.section}>
          <div style={styles.sectionTitle}>合并内容</div>
          <textarea rows={5} style={styles.input} value={draft.statement || ''}
            onChange={(event) => setDraft((current) => ({ ...current, statement: event.target.value }))} />
        </div>
      )}
      {draft.action === 'add' && (
        <div style={styles.section}>
          <div style={styles.sectionTitle}>新名称</div>
          <input style={styles.input} value={draft.key || ''} placeholder="填写名称"
            onChange={(event) => setDraft((current) => ({ ...current, key: event.target.value }))} />
        </div>
      )}
      <div style={styles.section}><NoteField value={draft.note} onChange={(note) => setDraft((current) => ({ ...current, note }))} /></div>
      <div style={styles.footer}>
        <ActionButton primary disabled={busy || !draft.action
          || (draft.action === 'merge' && !draft.statement?.trim())
          || (draft.action === 'add' && !draft.key?.trim())}
          onClick={() => submit({
            action: draft.action,
            statement: draft.action === 'merge' ? draft.statement.trim() : undefined,
            key: draft.action === 'add' ? draft.key.trim() : undefined,
            note: draft.note.trim() || undefined,
          })}>{busy ? '提交中...' : '确认'}</ActionButton>
      </div>
    </>
  );
}

/**
 * Props:
 * - interaction: { id, type, status, title?, description?, baseRevision?, payload }
 * - onSubmit(interactionId, response): sends one structured response
 * - onSkip(interactionId): optional whole-item skip handler
 * - onOpenFile(path): optional link used by content and finding reviews
 * Responses use { answers }, { action, note? }, or { value, note? } by type.
 */
export default function PendingInteraction({
  interaction,
  busy = false,
  error = '',
  onSubmit,
  onSkip,
  onOpenFile,
  flowchartDraftState = null,
  activeOperation = null,
}) {
  const [draft, setDraft] = useState(() => initialDraft(interaction));
  const [localBusy, setLocalBusy] = useState(false);
  const [localError, setLocalError] = useState('');
  // 提交被「涉及多个资源」挡下时，后端带回的候选目标与那次待重发的回答
  const [repairTargets, setRepairTargets] = useState([]);
  const [repairTarget, setRepairTarget] = useState('');
  const [retryResponse, setRetryResponse] = useState(null);
  const payload = interaction?.payload || {};
  const type = interactionType(interaction?.type);
  const presentation = interactionPresentation(interaction);
  // 后端正在处理时按钮一律禁用：留着能点只会让人撞出「项目正在处理中」
  const working = busy || localBusy || Boolean(activeOperation);
  const closed = useMemo(() => ['answered', 'resolved', 'completed', 'done', 'cancelled'].includes(interaction?.status), [interaction?.status]);

  useEffect(() => {
    setDraft(initialDraft(interaction));
    setLocalError('');
    setRepairTargets([]);
    setRepairTarget('');
    setRetryResponse(null);
  }, [interaction?.id]);

  useEffect(() => {
    setDraft((current) => ({ ...current, acknowledgeWarnings: false }));
  }, [flowchartDraftState?.draftRevision]);

  const submit = async (response) => {
    if (!interaction || !onSubmit || working) return;
    setLocalBusy(true);
    setLocalError('');
    try {
      await onSubmit(interaction.id, withRepairTarget(response, repairTarget));
      setRepairTargets([]);
      setRepairTarget('');
      setRetryResponse(null);
    } catch (cause) {
      // 「涉及多个资源、先说改哪一个」——后端已经把候选算好了，就地渲染成选项，
      // 否则用户只能看着红字反复提交同一份答案
      const candidates = repairTargetCandidatesFrom(cause);
      if (candidates.length) {
        setRepairTargets(candidates);
        setRepairTarget((current) => current || candidates[0].value);
        setRetryResponse(response);
      }
      // 已经渲染成可操作卡片的错误不再重复成一行红字
      if (!isSurfacedAsBlocker(cause)) setLocalError(safeUiError(cause, '提交失败，请重试'));
    } finally {
      setLocalBusy(false);
    }
  };

  const skip = async () => {
    if (onSkip) {
      setLocalBusy(true);
      setLocalError('');
      try {
        await onSkip(interaction.id);
      } catch (cause) {
        setLocalError(safeUiError(cause, '提交失败，请重试'));
      } finally {
        setLocalBusy(false);
      }
      return;
    }
    await submit({ action: 'skip' });
  };

  if (!interaction) return <div style={styles.state}>暂无待处理事项</div>;

  return (
    <section style={styles.shell} aria-label="待确认事项">
      <header style={styles.header}>
        <div style={styles.eyebrow}>{TYPE_TITLE[type] || '待确认'}</div>
        <h3 style={styles.title}>{presentation.title || TYPE_TITLE[type] || '请确认'}</h3>
        {presentation.description && <p style={styles.description}>{presentation.description}</p>}
      </header>

      {closed && <div style={styles.state}>已处理</div>}
      {/* 长耗时操作全程在请求里跑，期间待办仍是 pending。这时候给出可提交的表单，
          用户点了必然撞租约报「项目正在处理中」——如实说明正在处理即可。 */}
      {!closed && activeOperation && (
        <div style={styles.processing} role="status">
          <span className="spinner" />
          <span>已提交，正在{activeOperation.operation}。完成后这里会自动更新，不需要重复提交。</span>
        </div>
      )}
      {!closed && type === 'question' && <QuestionForm interaction={interaction} draft={draft} setDraft={setDraft} busy={working} submit={submit} />}
      {!closed && type === 'change_plan' && <ChangePlan interaction={interaction} draft={draft} setDraft={setDraft} busy={working} submit={submit} />}
      {!closed && type === 'decision' && <Decision interaction={interaction} draft={draft} setDraft={setDraft} busy={working} submit={submit} />}
      {!closed && type === 'artifact' && <ThreeWayReview interaction={interaction} draft={draft} setDraft={setDraft} busy={working} submit={submit} mode="artifact" onOpenFile={onOpenFile} />}
      {!closed && type === 'finding' && <ThreeWayReview interaction={interaction} draft={draft} setDraft={setDraft} busy={working} submit={submit} mode="finding" onOpenFile={onOpenFile} />}
      {!closed && type === 'knowledge' && <KnowledgeChoice interaction={interaction} draft={draft} setDraft={setDraft} busy={working} submit={submit} />}
      {!closed && type === 'flowchart' && flowchartDraftState && (
        <FlowchartDraftReview draft={draft} setDraft={setDraft} busy={working}
          submit={submit} flowchartDraftState={flowchartDraftState} />
      )}
      {!closed && type === 'flowchart' && !flowchartDraftState && <ThreeWayReview interaction={interaction} draft={draft} setDraft={setDraft} busy={working} submit={submit} mode="flowchart" onOpenFile={onOpenFile} />}
      {!closed && type === 'scenario' && <ThreeWayReview interaction={interaction} draft={draft} setDraft={setDraft} busy={working} submit={submit} mode="scenario" onOpenFile={onOpenFile} />}
      {!closed && !TYPE_TITLE[type] && <div style={styles.state}>暂不支持此确认项</div>}

      {!closed && (payload.allowSkip ?? payload.allow_skip) && type !== 'question' && (
        <div style={styles.footer}>
          <button type="button" className="btn" style={styles.quietButton} disabled={working} onClick={skip}>跳过</button>
        </div>
      )}
      {(error || localError) && <div role="alert" style={styles.error}>{safeUiError(error || localError, '提交失败，请重试')}</div>}
      {!closed && repairTargets.length > 0 && (
        <div style={styles.repairTarget}>
          <div style={styles.repairTargetTitle}>请选择本次要修改哪一个知识资源</div>
          {repairTargets.map((candidate) => (
            <label key={candidate.value} style={styles.repairTargetOption}>
              <input type="radio" name="repair-target" value={candidate.value}
                checked={repairTarget === candidate.value} disabled={working}
                onChange={() => setRepairTarget(candidate.value)} />
              <span>
                <strong>{candidate.targetKey}</strong>
                <em style={styles.repairTargetKind}>{candidate.targetKind}</em>
              </span>
            </label>
          ))}
          <button type="button" className="btn btn-p" style={styles.repairTargetSubmit}
            disabled={working || !repairTarget}
            onClick={() => { void submit(retryResponse ?? { answers: [] }); }}>
            用这个资源重新提交
          </button>
        </div>
      )}
    </section>
  );
}
