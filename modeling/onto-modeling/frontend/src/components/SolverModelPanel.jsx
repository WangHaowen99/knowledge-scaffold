import {
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
import { setWorkspaceDirty } from '../lib/workspaceDirty.js';
import { openEvaluation, prefetchEvaluationEntry } from '../lib/evaluation.js';
import {
  PACKAGE_PUBLISHED,
  SOLVER_VALIDATION_STATES,
  currentRevisionValidation,
  releaseSolverValidation,
  solverEvaluationTarget,
} from '../lib/packageEvaluation.js';
import {
  ADVANCED,
  FINDING_TITLES,
  MAX_TEMPLATE_TEXT,
  ROUTE_OPTIONS,
  SIDECAR_TABS,
  SOLVER_DIR,
  TEXT_PRESENTATION_FIELDS,
  VALIDATION_STATES,
  advancedText,
  collectMarks,
  diffContracts,
  disablePreview,
  draftFromView,
  draftKey,
  engineCapabilities,
  evaluatorCommandPlan,
  findProblemFamily,
  findingSource,
  isEnabled,
  liveSaveBlocker,
  localProblems,
  locateFinding,
  newDraftItem,
  problemFamily,
  regenerate,
  renderedSidecars,
  setAdvanced,
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
  toManual,
  toPayload,
  unloadedDirectories,
  validationState,
} from '../lib/solver-contract-model.js';
import CodeEditor from './cm/CodeEditor.jsx';

// 求解模型面板（DEV-FE-001）：ontology/solver/ 的唯一编辑入口。草稿整体提交，
// 后端按基线修订做乐观锁（409 冲突）与结构校验（400 带 findings）。编辑中的草稿停顿后
// 经即时检查接口按保存与发布的同一套规则检查（不落盘），Profile、评估器等语义问题保存前即可看到。

const LIVE_CHECK_DELAY_MS = 500;

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

// 求解验证在发布后的独立评测中进行（ADR solver-validation-unified-entry）：这里只显示当前修订是否已发布并通过
// 验证，并提供去评测与导出旧验证用例的入口；用例在评测页面的求解验证集中维护
function ValidationStatusCard({ projectId, view, dirty, active, reloadKey }) {
  const [status, setStatus] = useState(null);
  const [exporting, setExporting] = useState(false);
  const [published, setPublished] = useState(0);
  // 在本面板打开时从左侧发布：面板仍处于激活状态，需按发布通知重读，否则「去评测」仍指向发布前的版本
  useEffect(() => {
    const onPublished = (event) => {
      if (event.detail?.projectId === projectId) setPublished((value) => value + 1);
    };
    window.addEventListener(PACKAGE_PUBLISHED, onPublished);
    return () => window.removeEventListener(PACKAGE_PUBLISHED, onPublished);
  }, [projectId]);
  useEffect(() => {
    if (!active) return undefined;
    let alive = true;
    api
      .packageEvaluations(projectId)
      .then((value) => alive && setStatus(value))
      .catch(() => alive && setStatus({ configured: false }));
    return () => {
      alive = false;
    };
  }, [projectId, active, reloadKey, published]);
  // 点击「去评测」时要立即打开窗口：先取好评测入口地址
  useEffect(() => {
    prefetchEvaluationEntry().catch(() => {});
  }, []);
  if (validationState(view) !== 'after_publish') return null;
  const current = currentRevisionValidation(status, view.revision);
  const latest = status?.current_release_id ? releaseSolverValidation(status, status.current_release_id) : null;
  const target = solverEvaluationTarget(status, view.revision, dirty);
  let text = '正在读取求解验证状态。';
  if (dirty) text = '有未保存的修改：保存并发布新版本后，再对新版本发起求解验证。';
  else if (status?.configured === false) text = '评测服务不可用或未配置，暂时读不到求解验证状态。';
  else if (current.state === 'passed') text = `当前修订已在 ${current.version} 通过求解验证。`;
  else if (current.state === 'published') text = '当前修订已发布，尚未通过求解验证：在评测页面对该版本发起求解验证。';
  else if (status) text = '当前修改尚未发布和验证：发布新版本后，在「已发布版本（知识包评测）」中发起求解验证。';
  const evaluate = () =>
    openEvaluation(target.release, { runType: 'solver' }).catch((error) => message.error(error.message));
  // 确定按钮的点击里同步打开评测窗口；不返回 Promise，弹窗随即关闭
  const confirmStale = () =>
    Modal.confirm({
      title: '将评测不含当前修改的旧版本',
      content: `${dirty ? '有未保存的修改' : '当前修改尚未发布'}，评测页面将对最近发布的版本 ${target.version} 发起求解验证，结果不代表当前修改；要验证当前修改，请先${dirty ? '保存并' : ''}发布新版本。`,
      centered: true,
      okText: '仍去评测',
      cancelText: '取消',
      onOk: () => {
        evaluate();
      },
    });
  const exportCases = async () => {
    setExporting(true);
    try {
      const { cases = [] } = await api.solverValidationCases(projectId);
      if (!cases.length) {
        message.info('建模侧没有保存过验证用例');
        return;
      }
      const url = URL.createObjectURL(new Blob([`${JSON.stringify({ cases }, null, 2)}\n`], { type: 'application/json' }));
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = 'solver-validation-cases.json';
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      message.error(error.message);
    } finally {
      setExporting(false);
    }
  };
  return (
    <div className="solver-validation">
      <div className="solver-validation-head">
        <span className="solver-section-title">求解验证</span>
        <Tag color={current.state === 'passed' && !dirty ? 'success' : 'warning'}>
          {current.state === 'passed' && !dirty ? '当前修订已验证' : '待发布后验证'}
        </Tag>
        <Typography.Text type="secondary" className="solver-meta">
          修订 {shortRevision(view.revision)}
        </Typography.Text>
      </div>
      <div className="solver-validation-msg">{text}</div>
      {latest && (
        <div className="solver-meta">
          最近发布版本 {status.current_version}：{SOLVER_VALIDATION_STATES[latest.state].label}
          {latest.reason ? ` · ${latest.reason}` : ''}
        </div>
      )}
      <Space wrap size={6}>
        <Button size="small" type="primary" disabled={!target} onClick={target?.stale ? confirmStale : evaluate}>
          去评测
        </Button>
        <Button size="small" loading={exporting} onClick={exportCases}>
          导出验证用例（JSON）
        </Button>
      </Space>
      <Typography.Text type="secondary" className="solver-meta">
        求解验证集在评测页面维护，可导入此处导出的旧验证用例。发布不再要求先通过验证，发布时需确认求解能力尚未验证。
      </Typography.Text>
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
  evaluatorPlan,
  onEvaluator,
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
    evaluatorChoices.push({ value: evaluatorPath, label: `${evaluatorPath}（不存在或不是评估器）` });
  }
  const advancedHit = ADVANCED_FIELDS.some((field) => marks.fields.has(field) || highlight === field);
  const showAdvanced = !managed || advancedOpen || advancedHit;
  const evaluatorLink = item.evaluator && (
    <Typography.Link onClick={() => onOpenFile?.(item.evaluator)}>{item.evaluator}</Typography.Link>
  );
  // 评估器由助手生成（只含取数映射与参数的声明式评估器），用户不必手写；有未保存的修改时先保存
  const evaluatorAction = onEvaluator && evaluatorPlan && (
    <Space wrap size={8}>
      <Button
        size="small"
        type="primary"
        disabled={Boolean(evaluatorPlan.blocker)}
        title={evaluatorPlan.blocker || undefined}
        onClick={onEvaluator}
      >
        让助手生成评估器
      </Button>
      <span className="solver-meta">
        {evaluatorPlan.blocker
          || (evaluatorPlan.needsSave
            ? '有未保存的修改：点击后先保存求解契约，助手再按保存后的契约生成或修正声明式评估器并自检，完成后需重新验证'
            : '助手按已保存的契约生成或修正声明式评估器并自检，完成后需重新验证')}
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

export default function SolverModelPanel({
  project,
  versions = [],
  active = true,
  onOpenFile,
  onWorkspaceChanged,
  onCommand,
}) {
  const [view, setView] = useState(null);
  const [loadError, setLoadError] = useState('');
  const [reloadKey, setReloadKey] = useState(0); // 刷新按钮：验证状态区据此重读
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
  const [afterSave, setAfterSave] = useState(''); // 保存成功后发给会话的命令（让助手生成评估器）
  const [live, setLive] = useState(null); // 草稿的即时检查结果 { key, rejected, findings, rendered }
  const [liveError, setLiveError] = useState('');

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
  const saveTitle = errors.length ? `还有 ${errors.length} 个问题需要先修正` : saveBlocker;
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
  useEffect(() => {
    setWorkspaceDirty('solver-editor', dirtyRef.current);
    return () => setWorkspaceDirty('solver-editor', false);
  }, [dirty, items]);
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
    // 旧的端到端验证记录不再参与发布检查，没有可定位的字段
    if (target.validation) return;
    setSelected(target.uid);
    if (target.role) setRole(target.role);
    setHighlight(target.field || '');
    if (target.field) {
      requestAnimationFrame(() => document.getElementById(`solver-field-${target.field}`)?.focus?.());
    }
  };

  const save = async (baseRevision) => {
    const keepKey = String(items.find((item) => item.uid === selected)?.binding.solver_key || '').trim();
    const command = afterSave;
    setSaving(true);
    try {
      const saved = await api.saveSolverContract(project.id, toPayload(items), baseRevision, summary);
      apply(saved, keepKey);
      setConfirming(false);
      setConflict(null);
      setSummary('');
      message.success(saved.commit ? '求解契约已保存并记录迭代' : '求解契约没有变化');
      if (saved.commit) onWorkspaceChanged?.();
      // 保存失败（被拒、冲突、出错）不发命令，处理后需重新点击
      if (command) onCommand?.(command);
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
      setAfterSave('');
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
  const evaluatorPlan = current ? evaluatorCommandPlan(current, { dirty, saveBlocker: saveTitle }) : null;
  // 先保存再发命令：走同一个保存确认框，保存成功后由 save 发出
  const generateEvaluator = () => {
    if (!evaluatorPlan || evaluatorPlan.blocker) return;
    if (!evaluatorPlan.needsSave) {
      onCommand?.(evaluatorPlan.command);
      return;
    }
    setAfterSave(evaluatorPlan.command);
    setConfirming(true);
  };
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
          <Button
            size="small"
            icon={<ReloadOutlined />}
            title="重新读取"
            disabled={dirty}
            onClick={() => {
              load();
              setReloadKey((value) => value + 1);
            }}
          />
          <Button
            size="small"
            type="primary"
            disabled={!canSave}
            title={saveTitle || undefined}
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
            <ValidationStatusCard
              projectId={project.id}
              view={view}
              dirty={dirty}
              active={active}
              reloadKey={reloadKey}
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
                evaluatorPlan={evaluatorPlan}
                onEvaluator={onCommand ? generateEvaluator : undefined}
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
        onCancel={() => {
          setConfirming(false);
          setAfterSave('');
        }}
        okText="保存"
        confirmLoading={saving}
        onOk={() => save(view.revision)}
        destroyOnHidden
      >
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          {afterSave && <Alert type="info" showIcon message="保存成功后自动让助手生成评估器。" />}
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
          {validation === 'after_publish' && (
            <Alert type="info" showIcon message="保存会改变求解契约修订：发布新版本后，需对新版本重新发起求解验证。" />
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
