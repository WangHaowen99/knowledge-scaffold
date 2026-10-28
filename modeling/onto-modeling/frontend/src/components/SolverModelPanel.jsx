import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  DeleteOutlined,
  DownOutlined,
  PlusOutlined,
  ReloadOutlined,
  RightOutlined,
} from '@ant-design/icons';
import {
  Alert,
  Button,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Switch,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../lib/api.js';
import {
  ADVANCED,
  BATCH_FIELDS,
  FINDING_TITLES,
  MAX_BATCHES,
  MAX_CASES,
  MAX_CASE_NAME,
  MAX_EXPECTED_CANDIDATES,
  MAX_QUESTION,
  MAX_TEMPLATE_TEXT,
  ROUTE_OPTIONS,
  SIDECAR_TABS,
  SOLVER_DIR,
  TEXT_PRESENTATION_FIELDS,
  VALIDATION_STATES,
  advancedText,
  caseBatches,
  casesText,
  collectMarks,
  diffContracts,
  disablePreview,
  draftFromView,
  draftKey,
  engineCapabilities,
  evaluatorCommand,
  evaluatorCommandBlocker,
  findProblemFamily,
  findingSource,
  isEnabled,
  isValidationActive,
  liveSaveBlocker,
  localProblems,
  locateFinding,
  newBatch,
  newCase,
  newDraftItem,
  parseCasesText,
  problemFamily,
  regenerate,
  renderedSidecars,
  routeCaseHints,
  setAdvanced,
  setCaseBatches,
  setEnabled,
  setEngineCapabilities,
  setField,
  setOperation,
  setOptionLabel,
  setProblemFamily,
  setSidecar,
  setTemplateParam,
  shortRevision,
  shownChecks,
  startBlocker,
  toManual,
  toPayload,
  unloadedDirectories,
  validationCaseRows,
  validationSourceOptions,
  validationState,
} from '../lib/solver-contract-model.js';
import CodeEditor from './cm/CodeEditor.jsx';

// 求解模型面板（DEV-FE-001）：ontology/solver/ 的唯一编辑入口。草稿整体提交，
// 后端按基线修订做乐观锁（409 冲突）与结构校验（400 带 findings）。编辑中的草稿停顿后
// 经即时检查接口按保存与发布的同一套规则检查（不落盘），Profile、评估器等语义问题保存前即可看到。

const VALIDATION_POLL_MS = 3000;
const LIVE_CHECK_DELAY_MS = 500;

const formatTime = (seconds) => (seconds ? new Date(seconds * 1000).toLocaleString() : '');

const STATE_TEXT = {
  none: '普通知识包不需要求解验证。',
  not_needed: '求解能力均已停用，发布不需要端到端验证。',
  required: '求解知识包发布前，须对当前修订完成一次经 Consumer 的端到端验证。',
  running: '当前修订的验证进行中（经 Consumer Agent 提问求解，可能需要数分钟），完成后自动刷新。',
  passed: 'Consumer 已返回当前修订的权威求解结果，可以发布。',
  stale: '求解契约、绑定的 Operation、评估器或声明式评估器引用的对象文档在通过验证后已修改，请对当前修订重新验证。',
  failed: '当前修订的验证未通过，按诊断修正后重新验证。',
};

const RECORD_STATUS = { queued: '排队中', running: '运行中', passed: '通过', failed: '未通过' };

// rejected：最近一次保存被拒；blocking：即时检查判定保存会被拒
const SOURCE_NOTES = { rejected: '保存被拒', blocking: '保存会被拒' };

function FindingRow({ finding, source, onLocate }) {
  const note = SOURCE_NOTES[source];
  return (
    <div className="solver-finding">
      <div className="solver-finding-head">
        <Tag color={note ? 'red' : 'error'}>
          {FINDING_TITLES[finding.code] || finding.code}
        </Tag>
        {finding.solver_key && <Typography.Text code>{finding.solver_key}</Typography.Text>}
        {note && <Typography.Text type="danger">{note}</Typography.Text>}
        <Button size="small" type="link" onClick={() => onLocate(finding)}>
          定位
        </Button>
      </div>
      <div className="solver-finding-msg">{finding.message}</div>
      {finding.hint && <div className="solver-finding-hint">{finding.hint}</div>}
      <div className="solver-meta">{findingSource(finding)}</div>
    </div>
  );
}

function DiffList({ diff, unloaded = [] }) {
  const rows = [
    ...diff.added.map((item) => ({
      key: `a-${item.uid}`,
      tag: <Tag color="green">新增</Tag>,
      text: `${item.solverKey || '（未命名）'} → ${item.operation || '（未绑定 Operation）'}`,
    })),
    ...diff.changed.map((item) => ({
      key: `c-${item.uid}`,
      tag: <Tag color="blue">修改</Tag>,
      text: `${item.renamedFrom ? `${item.renamedFrom} → ` : ''}${item.solverKey}：${item.files.join('、')}`,
    })),
    ...diff.removed.map((item) => ({
      key: `r-${item.solverKey}`,
      tag: <Tag color="red">移除</Tag>,
      text: `${item.solverKey}（${item.operation}）`,
    })),
    ...unloaded.map((key) => ({
      key: `u-${key}`,
      tag: <Tag color="red">移除</Tag>,
      text: `${SOLVER_DIR}${key}/（无法解析，整体保存会移除该目录）`,
    })),
  ];
  if (!rows.length) return <Typography.Text type="secondary">求解契约文件没有改动</Typography.Text>;
  return (
    <div className="solver-diff">
      {rows.map((row) => (
        <div key={row.key} className="solver-diff-row">
          {row.tag}
          <span>{row.text}</span>
        </div>
      ))}
    </div>
  );
}

function caseDetail(row) {
  // 未调用求解工具、入参不一致是 Agent 链路问题，先于求解结果展示
  if (row.toolCalled === false) return ['Agent 未调用求解工具', row.detail].filter(Boolean).join('：');
  const diff = row.argumentDiff.slice(0, 5).join('、');
  return [
    row.argumentsMatch === false ? `入参与用例不一致${diff ? `：${diff}` : ''}` : '',
    row.authoritative ? '权威结果' : '非权威结果',
    row.feasibility === 'infeasible' ? '全不可行' : '',
    row.candidateCount != null ? `候选 ${row.candidateCount} 个` : '',
    row.expected != null ? `预期 ${row.expected} 个` : '',
    row.code,
    row.passed ? '' : row.detail,
  ].filter(Boolean).join(' · ');
}

// 模板契约的用例入参：计划月份、装置与按加工顺序排列的批次（行序即 item_order）
function RouteCaseFields({ args, unitOptions, onChange }) {
  const source = args || {};
  const rows = caseBatches(source);
  const setRows = (next) => onChange(setCaseBatches(source, next));
  const setRow = (index, patch) => setRows(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  const move = (index, delta) => {
    const next = [...rows];
    [next[index], next[index + delta]] = [next[index + delta], next[index]];
    setRows(next);
  };
  const unit = String(source.diesel_unit_no ?? '');
  const unitChoices = unitOptions.map((value) => ({ value, label: value }));
  if (unit && !unitOptions.includes(unit)) unitChoices.push({ value: unit, label: `${unit}（不支持）` });
  const hints = routeCaseHints(source, unitOptions);
  return (
    <div className="solver-route-case">
      <div className="solver-case-fields">
        <div className="solver-case-field">
          <span>计划月份</span>
          <Input
            size="small"
            value={String(source.plan_month ?? '')}
            placeholder="YYYY-MM"
            maxLength={7}
            onChange={(event) => onChange({ ...source, plan_month: event.target.value })}
          />
        </div>
        <div className="solver-case-field">
          <span>装置编号</span>
          {unitOptions.length ? (
            <Select
              size="small"
              value={unit || undefined}
              placeholder="选择装置"
              options={unitChoices}
              popupMatchSelectWidth={false}
              onChange={(value) => onChange({ ...source, diesel_unit_no: value })}
            />
          ) : (
            <Input
              size="small"
              value={unit}
              placeholder="评估器未列出可选装置，直接填写"
              onChange={(event) => onChange({ ...source, diesel_unit_no: event.target.value })}
            />
          )}
        </div>
      </div>
      <div className="solver-batch-scroll">
        <table className="solver-batch-table">
          <thead>
            <tr>
              <th>顺序</th>
              {BATCH_FIELDS.map((field) => (
                <th key={field.key}>{field.label}</th>
              ))}
              <th aria-label="操作" />
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              // eslint-disable-next-line react/no-array-index-key
              <tr key={index}>
                <td className="solver-meta">{index + 1}</td>
                {BATCH_FIELDS.map((field) => (
                  <td key={field.key}>
                    <Input
                      size="small"
                      value={String(row[field.key] ?? '')}
                      onChange={(event) => setRow(index, { [field.key]: event.target.value })}
                    />
                  </td>
                ))}
                <td className="solver-batch-actions">
                  <Button
                    size="small"
                    type="text"
                    icon={<ArrowUpOutlined />}
                    title="上移"
                    disabled={index === 0}
                    onClick={() => move(index, -1)}
                  />
                  <Button
                    size="small"
                    type="text"
                    icon={<ArrowDownOutlined />}
                    title="下移"
                    disabled={index === rows.length - 1}
                    onClick={() => move(index, 1)}
                  />
                  <Button
                    size="small"
                    type="text"
                    icon={<DeleteOutlined />}
                    title="删除该批次"
                    onClick={() => setRows(rows.filter((_, i) => i !== index))}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Button
        size="small"
        type="dashed"
        icon={<PlusOutlined />}
        disabled={rows.length >= MAX_BATCHES}
        onClick={() => setRows([...rows, newBatch(rows)])}
      >
        添加批次
      </Button>
      {hints.length > 0 && <div className="solver-case-hint">{hints.join('；')}</div>}
    </div>
  );
}

function CaseCard({ item, operations, onChange, onRemove }) {
  const operation = operations.find((entry) => entry.solver_key === item.solver_key);
  const keyOptions = operations
    .filter((entry) => entry.enabled || entry.solver_key === item.solver_key)
    .map((entry) => ({ value: entry.solver_key, label: `${entry.operation}（${entry.solver_key}）` }));
  const batches = operation?.managed ? caseBatches(item.arguments).length : 0;
  // 换绑求解能力时，还没填入参的用例按新能力预置
  const changeKey = (solverKey) => {
    const target = operations.find((entry) => entry.solver_key === solverKey);
    const empty = !Object.keys(item.arguments || {}).length;
    onChange({ ...item, solver_key: solverKey, arguments: empty && target ? newCase(target).arguments : item.arguments });
  };
  const setExpected = (value) => {
    const next = { ...item };
    if (value == null) delete next.expected_candidate_count;
    else next.expected_candidate_count = value;
    onChange(next);
  };
  const setQuestion = (value) => {
    const next = { ...item };
    if (value) next.question = value;
    else delete next.question;
    onChange(next);
  };
  return (
    <div className="solver-case">
      <div className="solver-case-head">
        <Input
          size="small"
          value={String(item.name ?? '')}
          maxLength={MAX_CASE_NAME}
          placeholder="用例名称"
          onChange={(event) => onChange({ ...item, name: event.target.value })}
        />
        <Select
          size="small"
          value={item.solver_key || undefined}
          placeholder="选择求解能力"
          options={keyOptions}
          popupMatchSelectWidth={false}
          onChange={changeKey}
        />
        <Typography.Text type="secondary" className="solver-meta">
          id {item.id}
        </Typography.Text>
        <Button size="small" type="text" icon={<DeleteOutlined />} title="删除该用例" onClick={onRemove} />
      </div>
      {operation?.managed ? (
        <RouteCaseFields
          args={item.arguments}
          unitOptions={operation.unit_options || []}
          onChange={(args) => onChange({ ...item, arguments: args })}
        />
      ) : (
        <div className="solver-meta">
          该求解契约的附属文件为手动编辑，入参在「以 JSON 编辑」中按它的输入 Schema 填写。
        </div>
      )}
      <div className="solver-case-field solver-case-question">
        <span>提问</span>
        <Input.TextArea
          size="small"
          value={String(item.question ?? '')}
          maxLength={MAX_QUESTION}
          autoSize={{ minRows: 1, maxRows: 4 }}
          placeholder="留空时按求解能力与入参自动生成提问；填写则原样向 Consumer Agent 提问"
          onChange={(event) => setQuestion(event.target.value)}
        />
      </div>
      <div className="solver-case-field">
        <span>预期候选数</span>
        <InputNumber
          size="small"
          min={0}
          max={MAX_EXPECTED_CANDIDATES}
          precision={0}
          value={item.expected_candidate_count ?? null}
          placeholder={operation?.managed ? `默认 2×批次数${batches ? `（${2 * batches}）` : ''}` : '可选'}
          style={{ width: 180 }}
          onChange={setExpected}
        />
      </div>
    </div>
  );
}

// 端到端验证（DEV-FE-003）：用例与数据源在这里选，执行在后台经 modeling → Consumer；
// 只有 Consumer 返回的权威结果才算通过，记录随修订失效
function ValidationCard({ projectId, view, dirty, innerRef, onRecord, onReload }) {
  const state = validationState(view);
  const record = view.validation;
  const operations = useMemo(() => view.contract?.operations || [], [view]);
  const [saved, setSaved] = useState(null); // null：用例尚未读取
  const [text, setText] = useState('');
  const [editorKey, setEditorKey] = useState(0);
  const [sources, setSources] = useState([]);
  const [sourceId, setSourceId] = useState('');
  const [busy, setBusy] = useState('');
  const [showJson, setShowJson] = useState(false);

  const reset = (cases) => {
    setSaved(cases);
    setText(casesText(cases));
    setEditorKey((value) => value + 1);
  };

  useEffect(() => {
    let alive = true;
    Promise.all([
      api.solverValidationCases(projectId),
      api.evalDataSources(projectId).catch(() => ({ sources: [] })),
    ])
      .then(([body, sourceBody]) => {
        if (!alive) return;
        reset(body.cases || []);
        setSources(sourceBody.sources || []);
      })
      .catch((error) => alive && message.error(error.message));
    return () => {
      alive = false;
    };
  }, [projectId]);

  const parsed = useMemo(() => parseCasesText(text, operations), [text, operations]);
  const casesDirty = saved != null && text.trim() !== casesText(saved).trim();
  const blocker = saved == null ? '正在读取验证用例' : startBlocker(view, { dirty, casesDirty, cases: saved });
  const rows = validationCaseRows(record);
  const sourceOptions = useMemo(() => validationSourceOptions(sources), [sources]);

  const saveCases = async () => {
    setBusy('cases');
    try {
      reset((await api.saveSolverValidationCases(projectId, parsed.cases)).cases);
      message.success('验证用例已保存');
    } catch (error) {
      message.error(error.message);
    } finally {
      setBusy('');
    }
  };

  // 编辑器文本是唯一来源：表单改动写回文本，JSON 编辑器据 editorKey 重建
  const editCases = (next) => {
    setText(casesText(next));
    setEditorKey((value) => value + 1);
  };
  const cases = parsed.cases || [];
  const enabledOperations = operations.filter((operation) => operation.enabled);
  const jsonOpen = showJson || Boolean(parsed.error);

  // 优先给还没有用例的启用能力添加
  const addCase = () => {
    const covered = new Set(cases.map((item) => item.solver_key));
    const target = enabledOperations.find((operation) => !covered.has(operation.solver_key)) || enabledOperations[0];
    if (target) editCases([...cases, newCase(target, cases)]);
  };

  const start = async (allowUnbound = false) => {
    setBusy('start');
    try {
      const body = await api.startSolverValidation(projectId, {
        revision: view.revision,
        dataSourceId: sourceId,
        allowUnbound,
      });
      onRecord(body.validation);
      message.success('已发起端到端验证，完成后自动刷新');
    } catch (error) {
      if (error.code === 'binding_problems' && !allowUnbound) {
        Modal.confirm({
          title: '数据绑定未通过检查',
          content: `${error.message}。忽略后这些对象在本次验证中不绑定数据表。`,
          okText: '忽略并继续验证',
          cancelText: '取消',
          onOk: () => start(true),
        });
      } else {
        message.error(error.message);
        if (error.status === 409) onReload();
      }
    } finally {
      setBusy('');
    }
  };

  return (
    <div className="solver-validation" ref={innerRef}>
      <div className="solver-validation-head">
        <span className="solver-section-title">端到端验证</span>
        <Tag color={VALIDATION_STATES[state].color}>{VALIDATION_STATES[state].label}</Tag>
        <Typography.Text type="secondary" className="solver-meta">
          修订 {shortRevision(view.revision)}
        </Typography.Text>
      </div>
      <div className="solver-validation-msg">{STATE_TEXT[state]}</div>
      {record && (
        <div className="solver-meta">
          最近一次：{RECORD_STATUS[record.status] || record.status}
          {record.status === 'passed' && record.authoritative !== true ? '（非权威结果）' : ''}
          {record.candidate_count != null ? ` · 候选 ${record.candidate_count} 个` : ''}
          {record.message ? ` · ${record.message}` : ''}
          {record.error_code ? ` · ${record.error_code}` : ''}
          {record.completed_at ? ` · ${formatTime(record.completed_at)}` : ''}
        </div>
      )}
      {rows.length > 0 && (
        <div className="solver-validation-cases">
          {rows.map((row) => (
            <div key={row.id} className="solver-validation-case">
              <Tag color={row.passed ? 'success' : 'error'}>{row.passed ? '通过' : '未通过'}</Tag>
              <span title={row.question ? `提问：${row.question}` : '提问：按求解能力与入参自动生成'}>{row.name}</span>
              <Typography.Text code>{row.solverKey}</Typography.Text>
              <span className="solver-meta">{caseDetail(row)}</span>
            </div>
          ))}
        </div>
      )}
      {dirty && state !== 'none' && (
        <div className="solver-validation-msg">有未保存的修改：保存后修订改变，需要重新验证。</div>
      )}
      {state !== 'not_needed' && (
        <>
          <div className="solver-validation-head">
            <span className="solver-section-title">验证用例</span>
            {casesDirty && <Tag color="warning">未保存</Tag>}
            <Typography.Text type="secondary" className="solver-meta">
              模板生成的求解契约按表单填写批次；预期候选数留空时按 2×批次数断言。验证经 Consumer Agent 提问执行，并核对 Agent 是否调用对应求解工具、传入的入参是否与用例一致。用例只存于工作区，不随知识包导出。
            </Typography.Text>
          </div>
          {saved != null && !parsed.error && (
            <div className="solver-cases">
              {cases.map((item, index) => (
                <CaseCard
                  // eslint-disable-next-line react/no-array-index-key
                  key={index}
                  item={item}
                  operations={operations}
                  onChange={(next) => editCases(cases.map((entry, i) => (i === index ? next : entry)))}
                  onRemove={() => editCases(cases.filter((_, i) => i !== index))}
                />
              ))}
              {cases.length === 0 && <Typography.Text type="secondary">还没有验证用例</Typography.Text>}
            </div>
          )}
          {saved != null && jsonOpen && (
            <div className="solver-sidecar">
              <div className="solver-meta">
                每条用例：id、solver_key、arguments（按输入 Schema 填写），可选 name、question（向 Agent 的提问，留空自动生成）与 expected_candidate_count
              </div>
              <CodeEditor key={editorKey} value={text} onChange={setText} />
            </div>
          )}
          {parsed.error && casesDirty && <Alert type="error" showIcon message={parsed.error} />}
          {!parsed.error && parsed.unknown?.length > 0 && (
            <Alert type="warning" showIcon message={`没有启用的求解能力：${parsed.unknown.join('、')}`} />
          )}
          <Space wrap size={6}>
            <Button
              size="small"
              icon={<PlusOutlined />}
              onClick={addCase}
              disabled={saved == null || Boolean(parsed.error) || !enabledOperations.length || cases.length >= MAX_CASES}
            >
              添加用例
            </Button>
            <Button size="small" type="link" disabled={Boolean(parsed.error)} onClick={() => setShowJson((value) => !value)}>
              {jsonOpen ? '收起 JSON' : '以 JSON 编辑'}
            </Button>
            <Button size="small" disabled={!casesDirty} onClick={() => reset(saved)}>
              放弃用例修改
            </Button>
            <Button
              size="small"
              loading={busy === 'cases'}
              disabled={!casesDirty || Boolean(parsed.error)}
              onClick={saveCases}
            >
              保存用例
            </Button>
            <Select
              size="small"
              value={sourceId}
              options={sourceOptions}
              popupMatchSelectWidth={false}
              style={{ minWidth: 180 }}
              onChange={setSourceId}
            />
            <Button
              size="small"
              type="primary"
              loading={busy === 'start'}
              disabled={Boolean(blocker)}
              title={blocker || undefined}
              onClick={() => start(false)}
            >
              发起验证
            </Button>
          </Space>
          {blocker && saved != null && state !== 'running' && (
            <div className="solver-meta">{blocker}</div>
          )}
        </>
      )}
    </div>
  );
}

function UnitsEditor({ units, onChange }) {
  const rows = Array.isArray(units) ? units : [];
  const set = (index, patch) => onChange(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  return (
    <div className="solver-units">
      {rows.map((row, index) => (
        // eslint-disable-next-line react/no-array-index-key
        <Space key={index} size={6}>
          <Input
            size="small"
            placeholder="字段（英文名称）"
            value={row?.field ?? ''}
            onChange={(event) => set(index, { field: event.target.value })}
          />
          <Input
            size="small"
            placeholder="单位，如 km、t、元"
            value={row?.unit ?? ''}
            onChange={(event) => set(index, { unit: event.target.value })}
          />
          <Button
            size="small"
            type="text"
            icon={<DeleteOutlined />}
            title="删除该行"
            onClick={() => onChange(rows.filter((_, i) => i !== index))}
          />
        </Space>
      ))}
      <Button
        size="small"
        type="dashed"
        icon={<PlusOutlined />}
        onClick={() => onChange([...rows, { field: '', unit: '' }])}
      >
        添加单位
      </Button>
    </div>
  );
}

function MarkAlerts({ marks = [] }) {
  return (
    <>
      {marks.map((mark, index) => (
        // eslint-disable-next-line react/no-array-index-key
        <Alert key={index} type={mark.severity === 'warning' ? 'warning' : 'error'} showIcon message={mark.message} />
      ))}
    </>
  );
}

// 高级设置里的字段：模板契约默认收起，有问题或被定位到时自动展开
const ADVANCED_FIELDS = ['solver_key', 'decision_model', 'problem_family', 'engine_capabilities', 'units'];
const OPTION_LABELS = { OPTION_0: '选项一', OPTION_1: '选项二' };
const OPTION_EXAMPLES = { OPTION_0: '如 方案甲', OPTION_1: '如 方案乙' };
const TEXT_PRESENTATION_LABELS = { process_name: '工艺名称', stream_name: '物流名称' };
const TEXT_PRESENTATION_EXAMPLES = { process_name: '如 加工装置', stream_name: '如 中间物流' };

function ContractEditor({
  item,
  marks,
  operations,
  families,
  evaluatorOptions,
  preview,
  boundElsewhere,
  role,
  onRole,
  nonce,
  highlight,
  onChange,
  onRemove,
  onOpenFile,
  commandBlocker,
  onCommand,
}) {
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const status = (field, label, extra) => {
    const mark = marks.fields.get(field);
    return {
      label,
      extra,
      validateStatus: mark ? (mark.severity === 'warning' ? 'warning' : 'error') : undefined,
      help: mark?.message,
      className: highlight === field ? 'solver-field-hit' : undefined,
    };
  };
  const key = item.binding.solver_key || '<solver_key>';
  const managed = item.managed;
  // 目录外的取值（导入包里的旧名称、未注册问题族或能力）照样列出并标注，便于看清再改
  const familyName = problemFamily(item.binding);
  const family = findProblemFamily(families, familyName);
  const familyOptions = families.map((entry) => ({ value: entry.id, label: entry.id }));
  if (familyName && !families.some((entry) => entry.id === familyName)) {
    familyOptions.push({ value: familyName, label: `${familyName}（${family ? '旧名称' : '未注册'}）` });
  }
  const capabilities = engineCapabilities(item.binding);
  const supported = family?.capabilities || [];
  const capabilityOptions = [
    ...supported.map((name) => ({ value: name, label: name })),
    ...capabilities
      .filter((name) => !supported.includes(name))
      .map((name) => ({ value: name, label: `${name}（不支持）` })),
  ];
  const evaluatorPath = item.params.evaluator_path || '';
  const evaluatorChoices = evaluatorOptions.map((option) => ({ value: option.path, label: option.file }));
  if (evaluatorPath && !evaluatorOptions.some((option) => option.path === evaluatorPath)) {
    evaluatorChoices.push({ value: evaluatorPath, label: `${evaluatorPath}（不存在）` });
  }
  const advancedHit = ADVANCED_FIELDS.some((field) => marks.fields.has(field) || highlight === field);
  const showAdvanced = !managed || advancedOpen || advancedHit;
  const evaluatorLink = item.evaluator && (
    <Typography.Link onClick={() => onOpenFile?.(item.evaluator)}>{item.evaluator}</Typography.Link>
  );
  // 评估器由助手生成（只含取数映射与参数的声明式评估器），用户不必手写
  const evaluatorAction = onCommand && (
    <Space wrap size={8}>
      <Button
        size="small"
        disabled={Boolean(commandBlocker)}
        title={commandBlocker || undefined}
        onClick={() => onCommand(evaluatorCommand(item))}
      >
        让助手生成评估器
      </Button>
      <span className="solver-meta">
        {commandBlocker || '助手按已保存的契约生成或修正声明式评估器并自检，完成后需重新验证'}
      </span>
    </Space>
  );
  return (
    <div className="solver-detail">
      <div className="solver-detail-head">
        <span className="solver-section-title">{item.binding.solver_key || '新的求解契约'}</span>
        <Popconfirm
          title="移除该求解契约？"
          description={`保存后删除 ${SOLVER_DIR}${key}/，Operation 文档本身不受影响。`}
          okText="移除"
          okButtonProps={{ danger: true }}
          onConfirm={onRemove}
        >
          <Button size="small" danger icon={<DeleteOutlined />}>
            移除
          </Button>
        </Popconfirm>
      </div>
      <Form layout="vertical" size="small" className="solver-form">
        <div className="solver-form-grid">
          <Form.Item {...status('operation', '绑定的 Operation')}>
            <Select
              id="solver-field-operation"
              showSearch
              value={item.binding.operation || undefined}
              placeholder="选择 Operation"
              options={operations.map((name) => ({
                value: name,
                label: boundElsewhere.has(name) ? `${name}（已绑定）` : name,
                disabled: boundElsewhere.has(name),
              }))}
              onChange={(value) => onChange((it) => setOperation(it, value))}
            />
          </Form.Item>
          {managed && (
            <Form.Item {...status('title', '结果标题', '求解结果展示时的标题')}>
              <Input
                id="solver-field-title"
                maxLength={MAX_TEMPLATE_TEXT}
                value={item.params.title}
                placeholder="留空则不设标题"
                onChange={(event) => onChange((it) => setTemplateParam(it, 'title', event.target.value))}
              />
            </Form.Item>
          )}
        </div>
        {managed && (
          <>
            <Form.Item
              {...status('display_mapping', '两个选项的显示名称', '每个批次在两个选项中二选一，求解结果按这里的名称展示')}
            >
              <div className="solver-form-grid">
                {ROUTE_OPTIONS.map((option, index) => (
                  <Input
                    key={option}
                    id={index === 0 ? 'solver-field-display_mapping' : undefined}
                    prefix={<span className="solver-meta">{OPTION_LABELS[option]}</span>}
                    maxLength={MAX_TEMPLATE_TEXT}
                    value={item.params.display_mapping[option]}
                    placeholder={OPTION_EXAMPLES[option]}
                    onChange={(event) => onChange((it) => setOptionLabel(it, option, event.target.value))}
                  />
                ))}
              </div>
            </Form.Item>
            <Form.Item
              {...status(
                'text_presentation',
                '结果文字（可选）',
                'Consumer 按「<装置编号><工艺名称>的 N 月度生产规划」逐批写出品种、批次量、天数与「<物流名称>→<选项显示名称>」；两项须同时填写，都留空则不生成',
              )}
            >
              <div className="solver-form-grid">
                {TEXT_PRESENTATION_FIELDS.map((name, index) => (
                  <Input
                    key={name}
                    id={index === 0 ? 'solver-field-text_presentation' : undefined}
                    prefix={<span className="solver-meta">{TEXT_PRESENTATION_LABELS[name]}</span>}
                    maxLength={MAX_TEMPLATE_TEXT}
                    value={item.params[name] || ''}
                    placeholder={TEXT_PRESENTATION_EXAMPLES[name]}
                    onChange={(event) => onChange((it) => setTemplateParam(it, name, event.target.value))}
                  />
                ))}
              </div>
            </Form.Item>
            <Form.Item
              {...status(
                'evaluator_path',
                '评估器',
                '留空时自动查找 ontology/scripts/ 下的评估器（自带计算代码的旧式或助手生成的声明式）；有多个时须指定',
              )}
            >
              <Select
                id="solver-field-evaluator_path"
                allowClear
                value={evaluatorPath || undefined}
                placeholder="自动查找"
                options={evaluatorChoices}
                onChange={(value) => onChange((it) => setTemplateParam(it, 'evaluator_path', value || ''))}
              />
              {evaluatorLink && <div className="solver-meta">已保存的修订解析到：{evaluatorLink}</div>}
              {evaluatorAction}
            </Form.Item>
          </>
        )}
        <Form.Item
          {...status(
            'enabled',
            '对外提供求解能力',
            '停用后契约仍随知识包保存，但导出包 index 不再列出、Consumer 不再调度，发布也不再要求验证。',
          )}
        >
          <Switch
            id="solver-field-enabled"
            checked={isEnabled(item.binding)}
            checkedChildren="启用"
            unCheckedChildren="停用"
            onChange={(checked) => onChange((it) => setEnabled(it, checked))}
          />
        </Form.Item>
        {managed && (
          <Button
            size="small"
            type="link"
            className="solver-advanced-toggle"
            icon={showAdvanced ? <DownOutlined /> : <RightOutlined />}
            disabled={advancedHit}
            onClick={() => setAdvancedOpen((value) => !value)}
          >
            高级设置（求解标识、Decision Model、问题族、引擎能力、字段单位）
          </Button>
        )}
        {showAdvanced && (
          <>
            <div className="solver-form-grid">
              <Form.Item
                {...status('solver_key', '求解标识（solver_key）', '导出时求解 Operation 以此命名；小写字母开头，只含小写字母、数字、下划线')}
              >
                <Input
                  id="solver-field-solver_key"
                  value={item.binding.solver_key ?? ''}
                  placeholder="如 route_plan"
                  onChange={(event) => onChange((it) => setField(it, 'solver_key', event.target.value))}
                />
              </Form.Item>
              <Form.Item {...status('decision_model', 'Decision Model ID', 'model.yaml 写了 id 时须与此一致')}>
                <Input
                  id="solver-field-decision_model"
                  value={item.binding.decision_model ?? ''}
                  onChange={(event) => onChange((it) => setField(it, 'decision_model', event.target.value))}
                />
              </Form.Item>
              <Form.Item
                {...status('problem_family', '问题族（engine_requirements.problem_family）', '从平台注册目录选择；Consumer 能力目录按它匹配求解引擎')}
              >
                <Select
                  id="solver-field-problem_family"
                  value={familyName || undefined}
                  placeholder="选择问题族"
                  options={familyOptions}
                  onChange={(value) => onChange((it) => setProblemFamily(it, value))}
                />
              </Form.Item>
            </div>
            <Form.Item
              {...status('engine_capabilities', '引擎能力（engine_requirements.capabilities）', '只能选问题族注册目录列出的能力；Consumer 运行时还会核对引擎实际提供这些能力')}
            >
              <Select
                id="solver-field-engine_capabilities"
                mode="multiple"
                value={capabilities}
                placeholder={family ? '选择需要的能力' : '先选择已注册的问题族'}
                options={capabilityOptions}
                onChange={(values) => onChange((it) => setEngineCapabilities(it, values))}
              />
            </Form.Item>
            <Form.Item {...status('units', '字段单位', '求解事实按这些单位解释；对象文档里没有单位时在这里补充')}>
              <UnitsEditor
                units={item.binding.units}
                onChange={(units) => onChange((it) => setField(it, 'units', units))}
              />
            </Form.Item>
          </>
        )}
        {!managed && (
          <Form.Item label="评估器">
            {evaluatorLink || (
              <Typography.Text type="secondary">
                {item.origin
                  ? '未解析到评估器，见上方检查结果'
                  : '保存后自动解析：ontology/scripts/ 下的旧式或声明式评估器'}
              </Typography.Text>
            )}
            {evaluatorAction}
          </Form.Item>
        )}
      </Form>
      {!managed && (
        <Alert
          type="info"
          showIcon
          message="附属文件为手动编辑：保存时按原文写入。按模板重新生成后改回表单填写，手动修改会被覆盖。"
        />
      )}
      <Tabs
        size="small"
        activeKey={role}
        onChange={onRole}
        tabBarExtraContent={
          managed ? (
            <Popconfirm
              title="转为手动编辑？"
              description="以当前生成结果为起点按原文编辑五份附属文件，表单里的选项名称、标题和评估器不再生效。"
              okText="转为手动编辑"
              disabled={!preview}
              onConfirm={() => onChange((it) => toManual(it, preview))}
            >
              <Button size="small" type="link" disabled={!preview}>
                转为手动编辑
              </Button>
            </Popconfirm>
          ) : (
            <Popconfirm
              title="按模板重新生成？"
              description="保存时五份附属文件按模板参数重新生成，手动修改会被覆盖。"
              okText="重新生成"
              okButtonProps={{ danger: true }}
              onConfirm={() => onChange(regenerate)}
            >
              <Button size="small" type="link">
                按模板重新生成
              </Button>
            </Popconfirm>
          )
        }
        items={[...SIDECAR_TABS.map((tab) => {
          const tabMarks = marks.roles.get(tab.role) || [];
          return {
            key: tab.role,
            label: (
              <span>
                {tab.label}
                {tabMarks.length > 0 && <span className="solver-tab-dot" />}
              </span>
            ),
            children: (
              <div className="solver-sidecar">
                <div className="solver-meta">
                  {SOLVER_DIR}
                  {key}/{tab.file}
                  {managed && ' · 按表单参数生成，只读'}
                </div>
                <MarkAlerts marks={tabMarks} />
                {managed ? (
                  preview ? (
                    <pre className="solver-preview">{preview[tab.role] ?? ''}</pre>
                  ) : (
                    <div className="solver-meta">正在生成预览…</div>
                  )
                ) : (
                  <CodeEditor
                    key={`${item.uid}:${tab.role}:${nonce}`}
                    value={item.sidecars[tab.role] ?? ''}
                    onChange={(text) => onChange((it) => setSidecar(it, tab.role, text))}
                  />
                )}
              </div>
            ),
          };
        }), {
          key: ADVANCED,
          label: (
            <span>
              高级字段
              {marks.roles.has(ADVANCED) && <span className="solver-tab-dot" />}
            </span>
          ),
          children: (
            <div className="solver-sidecar">
              <div className="solver-meta">
                {SOLVER_DIR}
                {key}/binding.yaml 中表单以外的字段（声明的能力与策略、Operation frontmatter、导入保留的扩展字段），以
                JSON 编辑
              </div>
              <MarkAlerts marks={marks.roles.get(ADVANCED)} />
              <CodeEditor
                key={`${item.uid}:${ADVANCED}:${nonce}:${item.advancedRev || 0}`}
                value={advancedText(item.binding)}
                onChange={(text) => onChange((it) => setAdvanced(it, text))}
              />
            </div>
          ),
        }]}
      />
    </div>
  );
}

const EMPTY_MARKS = { fields: new Map(), roles: new Map(), count: 0 };

export default function SolverModelPanel({ project, versions = [], onOpenFile, onWorkspaceChanged, onCommand }) {
  const [view, setView] = useState(null);
  const [loadError, setLoadError] = useState('');
  const [items, setItems] = useState([]);
  const [selected, setSelected] = useState(null);
  const [role, setRole] = useState('entry');
  const [nonce, setNonce] = useState(0); // CodeEditor 只在挂载时读 value：载入新内容时据此重建
  const [highlight, setHighlight] = useState('');
  const [rejected, setRejected] = useState([]);
  const [confirming, setConfirming] = useState(false);
  const [summary, setSummary] = useState('');
  const [saving, setSaving] = useState(false);
  const [conflict, setConflict] = useState(null); // 冲突时服务端的最新 view
  const [live, setLive] = useState(null); // 草稿的即时检查结果 { key, rejected, findings, rendered }
  const [liveError, setLiveError] = useState('');
  const validationRef = useRef(null);

  const apply = useCallback((next, keepKey) => {
    const drafts = draftFromView(next);
    setView(next);
    setItems(drafts);
    setRejected([]);
    setHighlight('');
    setNonce((value) => value + 1);
    setSelected((prev) => {
      const wanted = keepKey ? `saved:${keepKey}` : prev;
      return drafts.some((item) => item.uid === wanted) ? wanted : drafts[0]?.uid ?? null;
    });
  }, []);

  const load = useCallback(async () => {
    try {
      apply(await api.solverContract(project.id));
      setLoadError('');
    } catch (error) {
      setLoadError(error.message);
    }
  }, [project.id, apply]);

  const baseOperations = useMemo(() => view?.contract?.operations || [], [view]);
  const available = useMemo(() => view?.available_operations || [], [view]);
  const families = useMemo(() => view?.problem_families || [], [view]);
  const diff = useMemo(() => diffContracts(baseOperations, items), [baseOperations, items]);
  const problems = useMemo(() => localProblems(items, available, families), [items, available, families]);
  const errors = problems.filter((problem) => problem.severity === 'error');
  const unloaded = useMemo(() => unloadedDirectories(view), [view]);
  const dirty = diff.dirty;
  const payloadKey = useMemo(() => draftKey(items), [items]);
  const shown = useMemo(() => shownChecks({ dirty, live, rejected, view }), [dirty, live, rejected, view]);
  const liveFresh = Boolean(live && live.key === payloadKey);
  const marks = useMemo(
    () => collectMarks(items, problems, [...shown.rejected, ...shown.findings]),
    [items, problems, shown.rejected, shown.findings],
  );
  const saveBlocker = liveSaveBlocker(live, payloadKey);
  const canSave = (dirty || unloaded.length > 0) && errors.length === 0 && !saveBlocker;
  const shownCount = shown.rejected.length + shown.findings.length;
  const pending = shown.draft
    ? [...marks.values()].reduce((sum, entry) => sum + entry.count, 0)
    : problems.length;

  // 草稿停顿后即时检查；只保留与最新草稿对应的请求，草稿与已保存一致时直接看已保存修订的检查
  useEffect(() => {
    if (!dirty) {
      setLive(null);
      setLiveError('');
      return undefined;
    }
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const body = await api.checkSolverContract(project.id, JSON.parse(payloadKey), {
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        setLive({
          key: payloadKey,
          rejected: body.rejected || [],
          findings: body.findings || [],
          rendered: body.rendered || [],
        });
        setLiveError('');
      } catch (error) {
        if (!controller.signal.aborted) setLiveError(error.message);
      }
    }, LIVE_CHECK_DELAY_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [dirty, payloadKey, project.id]);

  // 工作区有新版本（助手改了绑定的 Operation / 评估器、导入了知识包）且没有草稿时跟随刷新
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty || items.some((item) => item.advancedError); // 未生效的高级字段文本也算草稿
  const head = `${versions.length}:${versions[0]?.commit || ''}`;
  useEffect(() => {
    if (!dirtyRef.current) load();
  }, [load, head]);

  useEffect(() => {
    if (!dirty) return undefined;
    const guard = (event) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', guard);
    return () => window.removeEventListener('beforeunload', guard);
  }, [dirty]);

  // 验证在后台执行：排队 / 运行中时轮询当前修订的记录；结束后无草稿则整体刷新（门禁随之更新）
  const validating = isValidationActive(view?.validation);
  useEffect(() => {
    if (!validating) return undefined;
    let alive = true;
    const timer = setInterval(async () => {
      try {
        const latest = await api.solverValidationLatest(project.id);
        if (!alive) return;
        if (!isValidationActive(latest.validation) && !dirtyRef.current) {
          load();
          return;
        }
        setView((prev) =>
          prev && prev.revision === latest.revision ? { ...prev, validation: latest.validation } : prev,
        );
      } catch {
        /* 网络抖动：下一轮重试 */
      }
    }, VALIDATION_POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [validating, project.id, load]);

  const recordStarted = (record) => setView((prev) => ({ ...prev, validation: record }));

  const update = (uid, change) =>
    setItems((list) => list.map((item) => (item.uid === uid ? change(item) : item)));

  const add = () => {
    const bound = new Set(items.map((item) => item.binding.operation));
    const operation = available.find((name) => !bound.has(name)) || '';
    const item = newDraftItem(view.template, operation, items);
    setItems((list) => [...list, item]);
    setSelected(item.uid);
    setRole('entry');
  };

  const remove = (uid) => {
    const rest = items.filter((item) => item.uid !== uid);
    setItems(rest);
    setSelected(rest[0]?.uid ?? null);
  };

  const discard = () =>
    Modal.confirm({
      title: '放弃未保存的修改？',
      content: '草稿将恢复为当前已保存的求解契约。',
      okText: '放弃修改',
      okButtonProps: { danger: true },
      cancelText: '继续编辑',
      onOk: () => apply(view),
    });

  const locate = (finding) => {
    const target = locateFinding(finding, items);
    if (!target) return;
    if (target.file) {
      onOpenFile?.(target.file);
      return;
    }
    if (target.validation) {
      validationRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }
    setSelected(target.uid);
    if (target.role) setRole(target.role);
    setHighlight(target.field || '');
    if (target.field) {
      requestAnimationFrame(() => document.getElementById(`solver-field-${target.field}`)?.focus?.());
    }
  };

  const save = async (baseRevision) => {
    const keepKey = String(items.find((item) => item.uid === selected)?.binding.solver_key || '').trim();
    setSaving(true);
    try {
      const saved = await api.saveSolverContract(project.id, toPayload(items), baseRevision, summary);
      apply(saved, keepKey);
      setConfirming(false);
      setConflict(null);
      setSummary('');
      message.success(saved.commit ? '求解契约已保存并记录迭代' : '求解契约没有变化');
      if (saved.commit) onWorkspaceChanged?.();
    } catch (error) {
      setConfirming(false);
      if (error.status === 409) {
        try {
          setConflict(await api.solverContract(project.id));
        } catch (reloadError) {
          message.error(reloadError.message);
        }
      } else if (error.data?.findings?.length) {
        setRejected(error.data.findings);
        message.error(`保存被拒绝：${error.message}`);
        locate(error.data.findings[0]);
      } else {
        message.error(error.message);
      }
    } finally {
      setSaving(false);
    }
  };

  if (!view) {
    return loadError ? (
      <Empty description={loadError}>
        <Button onClick={load}>重试</Button>
      </Empty>
    ) : (
      <div className="solver-loading">
        <Spin />
      </div>
    );
  }

  const current = items.find((item) => item.uid === selected);
  const currentIndex = items.findIndex((item) => item.uid === selected);
  // 模板条目的附属文件预览：用与当前草稿对应的即时检查生成结果，参数没改过时用已保存的文件
  const preview = current?.managed
    ? renderedSidecars(current, liveFresh ? live.rendered?.[currentIndex] : null)
    : null;
  const lost = disablePreview(diff);
  const validation = validationState(view);
  const conflictTheirs = conflict ? diffContracts(baseOperations, draftFromView(conflict)) : null;
  const conflictMine = conflict ? diffContracts(conflict.contract.operations, items) : null;

  return (
    <div className="solver-panel">
      <div className="solver-head">
        <Space wrap size={8}>
          <span className="solver-title">求解模型</span>
          {view.enabled ? (
            <Tag color={VALIDATION_STATES[validation].color}>{VALIDATION_STATES[validation].label}</Tag>
          ) : (
            <Tag>普通知识包</Tag>
          )}
          {dirty && <Tag color="warning">未保存</Tag>}
          {view.revision && (
            <Typography.Text type="secondary" className="solver-meta" title={view.revision}>
              修订 {shortRevision(view.revision)}
            </Typography.Text>
          )}
        </Space>
        <Space wrap size={6}>
          {(items.length > 0 || view.enabled) && (
            <Button size="small" icon={<PlusOutlined />} disabled={!available.length} onClick={add}>
              添加求解契约
            </Button>
          )}
          <Button size="small" disabled={!dirty} onClick={discard}>
            放弃修改
          </Button>
          <Button size="small" icon={<ReloadOutlined />} title="重新读取" disabled={dirty} onClick={load} />
          <Button
            size="small"
            type="primary"
            disabled={!canSave}
            title={errors.length ? `还有 ${errors.length} 个问题需要先修正` : saveBlocker || undefined}
            onClick={() => setConfirming(true)}
          >
            保存
          </Button>
        </Space>
      </div>

      {!view.enabled && items.length === 0 ? (
        <Empty className="solver-empty" description="当前是普通知识包，没有求解契约">
          <Typography.Paragraph type="secondary" className="solver-empty-text">
            为某个 Operation 启用求解契约后，知识包导出为求解知识包（schema 0.2），Consumer
            按契约调用求解器；发布前须对当前修订完成一次端到端验证。移除全部契约即回到普通知识包。
          </Typography.Paragraph>
          <Button type="primary" disabled={!available.length} onClick={add}>
            启用求解契约
          </Button>
          {!available.length && (
            <div className="solver-meta">先完成「操作提取」，再为 Operation 启用求解契约</div>
          )}
        </Empty>
      ) : (
        <>
          {diff.becomesSolverPackage && (
            <Alert
              type="info"
              showIcon
              message="保存后知识包导出为求解知识包（schema 0.2），发布前须完成一次端到端验证。"
            />
          )}
          {diff.revertsToPlain && (
            <Alert type="info" showIcon message="已移除全部求解契约：保存后知识包回到普通知识包（schema 0.1）。" />
          )}
          {shown.draft ? (
            <div className="solver-findings">
              <span className="solver-section-title">实时契约检查（{shownCount}）</span>
              <div className="solver-meta">
                {!liveFresh && !liveError && '检查中… '}
                按保存与发布的同一套规则检查未保存的草稿；标「保存会被拒」的须先修正，其余保存后发布检查仍会拦截
              </div>
              {shown.rejected.map((finding, index) => (
                // eslint-disable-next-line react/no-array-index-key
                <FindingRow key={`blk-${index}`} finding={finding} source="blocking" onLocate={locate} />
              ))}
              {shown.findings.map((finding, index) => (
                // eslint-disable-next-line react/no-array-index-key
                <FindingRow key={`dft-${index}`} finding={finding} source="draft" onLocate={locate} />
              ))}
              {liveFresh && shownCount === 0 && items.length > 0 && (
                <Typography.Text type="success">草稿未发现契约问题。</Typography.Text>
              )}
            </div>
          ) : (
            shownCount > 0 && (
              <div className="solver-findings">
                <span className="solver-section-title">发布检查（{shownCount}）</span>
                {shown.rejected.map((finding, index) => (
                  // eslint-disable-next-line react/no-array-index-key
                  <FindingRow key={`rej-${index}`} finding={finding} source="rejected" onLocate={locate} />
                ))}
                {shown.findings.map((finding, index) => (
                  // eslint-disable-next-line react/no-array-index-key
                  <FindingRow key={`srv-${index}`} finding={finding} source="server" onLocate={locate} />
                ))}
              </div>
            )
          )}
          {dirty && liveError && (
            <div className="solver-meta">即时检查暂不可用：{liveError}；保存时仍按后端规则完整校验。</div>
          )}
          {view.enabled && (
            <ValidationCard
              projectId={project.id}
              view={view}
              dirty={dirty}
              innerRef={validationRef}
              onRecord={recordStarted}
              onReload={() => !dirtyRef.current && load()}
            />
          )}
          <div className="solver-body">
            <div className="solver-list">
              {items.map((item) => {
                const changed = diff.changed.some((entry) => entry.uid === item.uid);
                const count = (marks.get(item.uid) || EMPTY_MARKS).count;
                return (
                  <button
                    type="button"
                    key={item.uid}
                    className={`solver-list-item${item.uid === selected ? ' on' : ''}`}
                    onClick={() => setSelected(item.uid)}
                  >
                    <span className="solver-list-key">{item.binding.solver_key || '（未命名）'}</span>
                    <span className="solver-list-op">{item.binding.operation || '未绑定 Operation'}</span>
                    <span className="solver-list-tags">
                      {!isEnabled(item.binding) && <Tag>已停用</Tag>}
                      {!item.managed && <Tag>手动编辑</Tag>}
                      {item.origin == null && <Tag color="green">新增</Tag>}
                      {changed && <Tag color="blue">已修改</Tag>}
                      {count > 0 && <Tag color="error">{count} 个问题</Tag>}
                    </span>
                  </button>
                );
              })}
              {items.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有求解契约" />}
            </div>
            {current ? (
              <ContractEditor
                key={current.uid}
                item={current}
                marks={marks.get(current.uid) || EMPTY_MARKS}
                operations={available}
                families={families}
                evaluatorOptions={view.evaluator_options || []}
                preview={preview}
                boundElsewhere={
                  new Set(items.filter((item) => item.uid !== current.uid).map((item) => item.binding.operation))
                }
                role={role}
                onRole={setRole}
                nonce={nonce}
                highlight={highlight}
                onChange={(change) => update(current.uid, change)}
                onRemove={() => remove(current.uid)}
                onOpenFile={onOpenFile}
                commandBlocker={evaluatorCommandBlocker(current, { dirty })}
                onCommand={onCommand}
              />
            ) : (
              <div className="solver-detail" />
            )}
          </div>
        </>
      )}

      <Modal
        title="保存求解契约"
        open={confirming}
        onCancel={() => setConfirming(false)}
        okText="保存"
        confirmLoading={saving}
        onOk={() => save(view.revision)}
        destroyOnHidden
      >
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <DiffList diff={diff} unloaded={unloaded} />
          {lost.length > 0 && (
            <Alert
              type="warning"
              showIcon
              message="停用预览：以下求解能力保存后不再对外提供"
              description={lost
                .map((item) => `${item.solverKey}（${item.reason === 'removed' ? '移除' : '停用'}）`)
                .join('、')}
            />
          )}
          {diff.revertsToPlain && <Alert type="info" showIcon message="保存后知识包回到普通知识包（schema 0.1）。" />}
          {validation === 'passed' && (
            <Alert type="info" showIcon message="保存会改变求解契约修订，当前的验证结果对新修订不再有效。" />
          )}
          {pending > 0 && (
            <Alert
              type="warning"
              showIcon
              message={`还有 ${pending} 个提示未处理，保存后发布检查会继续拦截`}
            />
          )}
          <Input
            maxLength={80}
            placeholder="改动说明（可选，写进迭代说明）"
            value={summary}
            onChange={(event) => setSummary(event.target.value)}
          />
        </Space>
      </Modal>

      <Modal
        title="求解契约已被修改"
        open={!!conflict}
        onCancel={() => setConflict(null)}
        destroyOnHidden
        footer={[
          <Button key="cancel" onClick={() => setConflict(null)}>
            稍后处理
          </Button>,
          <Button
            key="theirs"
            onClick={() => {
              apply(conflict);
              setConflict(null);
            }}
          >
            载入最新（放弃我的修改）
          </Button>,
          <Popconfirm
            key="mine"
            title="用我的草稿覆盖最新内容？"
            description="对方的改动会被整体替换。"
            okText="覆盖保存"
            okButtonProps={{ danger: true }}
            onConfirm={() => save(conflict.revision)}
          >
            <Button type="primary" danger loading={saving} disabled={errors.length > 0 || Boolean(saveBlocker)}>
              覆盖保存
            </Button>
          </Popconfirm>,
        ]}
      >
        {conflict && (
          <Space direction="vertical" size={10} style={{ width: '100%' }}>
            <Typography.Text>
              打开面板之后，求解契约或它引用的内容被修改了（导入知识包、其他人保存，或助手改了绑定的
              Operation / 评估器）。
            </Typography.Text>
            <div>
              <div className="solver-section-title">对方的改动</div>
              {conflictTheirs.dirty ? (
                <DiffList diff={conflictTheirs} />
              ) : (
                <Typography.Text type="secondary">
                  求解契约文件没有变化，变化来自绑定的 Operation、评估器或它引用的对象文档；覆盖保存不会丢失它们。
                </Typography.Text>
              )}
            </div>
            <div>
              <div className="solver-section-title">覆盖保存将写入</div>
              <DiffList diff={conflictMine} />
            </div>
          </Space>
        )}
      </Modal>
    </div>
  );
}
