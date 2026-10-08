import { DeleteOutlined, EditOutlined, PlusOutlined, UploadOutlined } from '@ant-design/icons';
import {
  Alert,
  Button,
  Input,
  Modal,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
} from 'antd';
import { useEffect, useState } from 'react';
import { api } from '../lib/api.js';
import {
  GRAPH_IMPORT_OPTIONS,
  PROVIDER_OPTIONS,
  SOURCE_KINDS,
  emptySourceForm,
  importPreviewRequest,
  kindMeta,
  sourceBadge,
  sourceFormError,
  sourceFormPayload,
  sourceKind,
  sourceSummary,
  sourceToForm,
} from '../lib/eval-data-source-form.js';

const VISIBLE_SOURCE_KINDS = SOURCE_KINDS.filter((kind) => kind.value !== 'dataset');

// 评测集下拉里的非存档来源
const TRACE_CHOICE = '__trace__';
const MANUAL_CHOICE = '__manual__';

// 数据评测向导：① 数据源（已保存过 → 选择；从未配置 → 先配置）② 评测集（导入过 → 选择）
export default function EvalWizard({ project, open, onClose, onStarted, onBindingProblems }) {
  const [loading, setLoading] = useState(false);
  const [step, setStep] = useState('source'); // source | cases
  const [sources, setSources] = useState([]);
  const [options, setOptions] = useState({ mock_tables: 0 });
  const [defaults, setDefaults] = useState({});
  const [selectedSourceId, setSelectedSourceId] = useState('');
  const [editing, setEditing] = useState(null); // {id, form} | null；无已存配置时恒为表单
  const [savingSource, setSavingSource] = useState(false);
  const [sets, setSets] = useState([]);
  const [traceCases, setTraceCases] = useState([]);
  const [caseChoice, setCaseChoice] = useState(MANUAL_CHOICE);
  const [cases, setCases] = useState([]);
  const [loadingSet, setLoadingSet] = useState(false);
  const [saveAsName, setSaveAsName] = useState(null); // null = 未展开「另存为评测集」
  const [running, setRunning] = useState(false);

  useEffect(() => {
    if (!open) return undefined;
    let active = true;
    setLoading(true);
    setStep('source');
    setEditing(null);
    setSaveAsName(null);
    Promise.all([
      api.evalDataSources(project.id),
      api.evalSets(project.id).catch(() => ({ sets: [] })),
      api.evalCases(project.id).catch(() => ({ cases: [] })),
    ])
      .then(([ds, setData, trace]) => {
        if (!active) return;
        const saved = ds.sources || [];
        const visibleSources = saved.filter((source) => sourceKind(source) !== 'dataset');
        setSources(visibleSources);
        setOptions(ds.options || { mock_tables: 0 });
        setDefaults(ds.defaults || {});
        setSelectedSourceId(visibleSources[0]?.id || '');
        // 从未配置过数据源：直接进配置表单（评测前必须先配置）
        if (!saved.length) setEditing({ id: '', form: emptySourceForm(ds.defaults) });
        setSets(setData.sets || []);
        setTraceCases(trace.cases || []);
        return initCases(setData.sets || [], trace.cases || []);
      })
      .catch((error) => {
        if (active) message.error(error.message);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, project.id]);

  // 导入过评测集 → 默认选最近使用的；否则沿用场景验证转换的用例
  const initCases = async (savedSets, trace) => {
    if (savedSets.length) return chooseCases(savedSets[0].id, trace);
    return chooseCases(trace.length ? TRACE_CHOICE : MANUAL_CHOICE, trace);
  };

  const chooseCases = async (choice, trace = traceCases) => {
    setCaseChoice(choice);
    setSaveAsName(null);
    if (choice === TRACE_CHOICE) return setCases(trace.map((c) => ({ ...c })));
    if (choice === MANUAL_CHOICE) return setCases([{ question: '', groundTruth: '' }]);
    setLoadingSet(true);
    try {
      const data = await api.evalSet(project.id, choice);
      setCases(data.set.cases || []);
    } catch (error) {
      message.error(error.message);
    } finally {
      setLoadingSet(false);
    }
    return undefined;
  };

  const selectedSource = sources.find((s) => s.id === selectedSourceId) || null;
  const savedSetId = sets.some((s) => s.id === caseChoice) ? caseChoice : '';

  // ---- 数据源 ----

  const saveSource = async () => {
    const error = sourceFormError(editing.form, options);
    if (error) {
      message.error(error);
      return;
    }
    setSavingSource(true);
    try {
      const payload = sourceFormPayload(editing.form);
      const data = editing.id
        ? await api.evalDataSourceUpdate(project.id, editing.id, payload)
        : await api.evalDataSourceCreate(project.id, payload);
      setSources((prev) => [data.source, ...prev.filter((s) => s.id !== data.source.id)]);
      setSelectedSourceId(data.source.id);
      setEditing(null);
      setStep('cases');
    } catch (err) {
      message.error(err.message);
    } finally {
      setSavingSource(false);
    }
  };

  const deleteSource = async (source) => {
    try {
      await api.evalDataSourceDelete(project.id, source.id);
      const rest = sources.filter((s) => s.id !== source.id);
      setSources(rest);
      if (selectedSourceId === source.id) setSelectedSourceId(rest[0]?.id || '');
      if (!rest.length) setEditing({ id: '', form: emptySourceForm(defaults) });
    } catch (err) {
      message.error(err.message);
    }
  };

  // ---- 评测集 ----

  const importCases = async (file) => {
    try {
      const data = await api.evalCasesImport(project.id, file);
      if (data.set) {
        setSets((prev) => [data.set, ...prev]);
        setCaseChoice(data.set.id);
        setCases(data.cases);
        message.success(`已导入 ${data.cases.length} 条用例，并保存为评测集「${data.set.name}」`);
      } else {
        setCases((prev) => [...prev.filter((c) => c.question.trim()), ...data.cases]);
        message.warning(data.warning || `已导入 ${data.cases.length} 条用例（未保存为评测集）`);
      }
    } catch (error) {
      message.error(error.message);
    }
    return false; // 阻止默认上传行为
  };

  const saveAsSet = async () => {
    try {
      const data = await api.evalSetCreate(project.id, {
        name: saveAsName.trim(),
        cases,
        source: caseChoice === TRACE_CHOICE ? 'trace' : 'manual',
      });
      setSets((prev) => [data.set, ...prev]);
      setCaseChoice(data.set.id);
      setSaveAsName(null);
      message.success(`已保存为评测集「${data.set.name}」`);
    } catch (error) {
      message.error(error.message);
    }
  };

  const deleteSet = async () => {
    try {
      await api.evalSetDelete(project.id, savedSetId);
      const rest = sets.filter((s) => s.id !== savedSetId);
      setSets(rest);
      await chooseCases(rest[0]?.id || (traceCases.length ? TRACE_CHOICE : MANUAL_CHOICE));
    } catch (error) {
      message.error(error.message);
    }
  };

  const updateCase = (index, patch) =>
    setCases((prev) => prev.map((c, i) => (i === index ? { ...c, ...patch } : c)));

  const runnableCases = cases.filter((c) => c.question.trim());

  const start = async (allowUnbound) => {
    await api.evalRun(project.id, {
      cases: runnableCases,
      dataSourceId: selectedSourceId,
      evalSetId: savedSetId,
      allowUnbound,
    });
    message.success('评测已启动，进度见「数据评测」阶段');
    onStarted?.();
  };

  const run = async () => {
    setRunning(true);
    try {
      await start(false);
    } catch (error) {
      // 数据绑定未过检：交给检查弹窗修复或降级，确认后用同一份评测集续跑
      if (error.code === 'binding_problems' && onBindingProblems) onBindingProblems(start);
      else message.error(error.message);
    } finally {
      setRunning(false);
    }
  };

  // ---- 渲染 ----

  let title = '数据评测 · 第 2 步：选择评测集';
  let footer;
  let body;
  if (loading) {
    title = '数据评测';
    footer = null;
    body = (
      <div style={{ textAlign: 'center', padding: 32 }}>
        <Spin />
      </div>
    );
  } else if (step === 'source' && editing) {
    title = '数据评测 · 第 1 步：新建数据源配置';
    if (!sources.length) title = '数据评测 · 第 1 步：配置数据源';
    else if (editing.id) title = '数据评测 · 第 1 步：编辑数据源配置';
    footer = [
      sources.length > 0 && (
        <Button key="back" onClick={() => setEditing(null)}>
          ← 返回选择
        </Button>
      ),
      <Button key="cancel" onClick={onClose}>
        取消
      </Button>,
      <Button key="save" type="primary" loading={savingSource} onClick={saveSource}>
        保存并继续 →
      </Button>,
    ];
    body = (
      <SourceForm
        form={editing.form}
        // patch 可为函数：并发上传等异步回填需基于最新表单计算
        onChange={(patch) =>
          setEditing((prev) => ({
            ...prev,
            form: { ...prev.form, ...(typeof patch === 'function' ? patch(prev.form) : patch) },
          }))
        }
        options={options}
        defaults={defaults}
        projectId={project.id}
        firstTime={!sources.length}
      />
    );
  } else if (step === 'source') {
    title = '数据评测 · 第 1 步：选择数据源';
    footer = [
      <Button key="cancel" onClick={onClose}>
        取消
      </Button>,
      <Button key="next" type="primary" disabled={!selectedSource} onClick={() => setStep('cases')}>
        下一步：选择评测集 →
      </Button>,
    ];
    body = (
      <div className="eval-ds-view">
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
          选择本轮评测使用的数据源配置；需要其他数据源时新建一个，已保存的配置可重复选用。
        </Typography.Paragraph>
        {sources.map((source) => (
          <SourceCard
            key={source.id}
            source={source}
            options={options}
            selected={source.id === selectedSourceId}
            onSelect={() => setSelectedSourceId(source.id)}
            onEdit={() => setEditing({ id: source.id, form: sourceToForm(source, defaults) })}
            onDelete={() => deleteSource(source)}
          />
        ))}
        <Button
          type="dashed"
          icon={<PlusOutlined />}
          onClick={() => setEditing({ id: '', form: emptySourceForm(defaults) })}
        >
          新建数据源配置
        </Button>
        {selectedSource && (
          <ImportPreview
            projectId={project.id}
            request={{ data_source_id: selectedSource.id, rev: selectedSource.updated_at }}
          />
        )}
      </div>
    );
  } else {
    let casesHint = '逐条填写问题与标准答案，或批量导入 JSON。';
    if (savedSetId) {
      casesHint = '运行前可修改、删除或补充用例，修改会在运行时同步保存到该评测集。';
    } else if (caseChoice === TRACE_CHOICE) {
      casesHint = '用例由「场景验证」的 trace 场景自动转换（问题 → consumer 智能体作答 → Judge 按标准答案判分）。运行前请复核标准答案是否准确。';
    }
    footer = [
      <Button key="prev" onClick={() => setStep('source')}>
        ← 上一步
      </Button>,
      <Button
        key="run"
        type="primary"
        loading={running}
        disabled={!runnableCases.length || loadingSet}
        onClick={run}
      >
        运行评测（{runnableCases.length} 条）
      </Button>,
    ];
    body = (
      <>
        <div className="eval-set-bar">
          <Typography.Text type="secondary" style={{ fontSize: 12, flexShrink: 0 }}>
            数据源：{selectedSource?.name || '—'}
          </Typography.Text>
        </div>
        <div className="eval-set-bar">
          <Typography.Text strong style={{ fontSize: 12.5, flexShrink: 0 }}>
            评测集
          </Typography.Text>
          <Select
            size="small"
            style={{ flex: 1, minWidth: 0 }}
            value={caseChoice}
            onChange={(value) => chooseCases(value)}
            options={[
              ...(sets.length
                ? [
                    {
                      label: '已保存的评测集',
                      options: sets.map((s) => ({ value: s.id, label: `${s.name}（${s.count} 条）` })),
                    },
                  ]
                : []),
              {
                label: '其他来源',
                options: [
                  {
                    value: TRACE_CHOICE,
                    label: `场景验证转换（${traceCases.length} 条）`,
                    disabled: !traceCases.length,
                  },
                  { value: MANUAL_CHOICE, label: '手工录入' },
                ],
              },
            ]}
          />
          {savedSetId && (
            <Popconfirm
              title="删除该评测集？"
              description="只删除保存的用例清单，不影响已有评测报告。"
              okText="删除"
              okButtonProps={{ danger: true }}
              onConfirm={deleteSet}
            >
              <Button size="small" type="text" danger icon={<DeleteOutlined />} />
            </Popconfirm>
          )}
        </div>
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          {casesHint}
          {!sets.length && ' 导入的用例会保存为评测集，下次评测可直接选用。'}
        </Typography.Paragraph>
        <Spin spinning={loadingSet}>
          <div style={{ maxHeight: 340, overflow: 'auto' }}>
            {cases.map((item, index) => (
              <div key={index} className="eval-case-row">
                <div className="eval-case-head">
                  <Typography.Text strong style={{ fontSize: 12 }}>
                    用例 {index + 1}
                  </Typography.Text>
                  <Button
                    size="small"
                    type="text"
                    danger
                    onClick={() => setCases((prev) => prev.filter((_, i) => i !== index))}
                  >
                    删除
                  </Button>
                </div>
                <Input.TextArea
                  autoSize={{ minRows: 1, maxRows: 3 }}
                  placeholder="问题"
                  value={item.question}
                  onChange={(e) => updateCase(index, { question: e.target.value })}
                />
                <Input.TextArea
                  autoSize={{ minRows: 1, maxRows: 3 }}
                  placeholder="标准答案"
                  style={{ marginTop: 4 }}
                  value={item.groundTruth}
                  onChange={(e) => updateCase(index, { groundTruth: e.target.value })}
                />
              </div>
            ))}
          </div>
        </Spin>
        <Space wrap style={{ marginTop: 8 }}>
          <Button
            size="small"
            onClick={() => setCases((prev) => [...prev, { question: '', groundTruth: '' }])}
          >
            + 手工添加用例
          </Button>
          <Upload accept=".json,application/json" showUploadList={false} beforeUpload={importCases}>
            <Button size="small">导入 JSON 为评测集</Button>
          </Upload>
          <Button
            size="small"
            type="link"
            onClick={() => window.open(api.evalCasesTemplateUrl(project.id))}
          >
            下载模板
          </Button>
          {!savedSetId && runnableCases.length > 0 && saveAsName === null && (
            <Button size="small" type="link" onClick={() => setSaveAsName('')}>
              另存为评测集
            </Button>
          )}
        </Space>
        {saveAsName !== null && (
          <Space.Compact size="small" style={{ marginTop: 8, width: '100%' }}>
            <Input
              autoFocus
              placeholder="评测集名称"
              value={saveAsName}
              onChange={(e) => setSaveAsName(e.target.value)}
              onPressEnter={() => saveAsName.trim() && saveAsSet()}
            />
            <Button type="primary" disabled={!saveAsName.trim()} onClick={saveAsSet}>
              保存
            </Button>
            <Button onClick={() => setSaveAsName(null)}>取消</Button>
          </Space.Compact>
        )}
      </>
    );
  }

  return (
    <Modal title={title} open={open} onCancel={onClose} width={720} footer={footer} destroyOnHidden>
      {body}
    </Modal>
  );
}

function SourceCard({ source, options, selected, onSelect, onEdit, onDelete }) {
  const meta = kindMeta(sourceKind(source));
  return (
    <div className={`eval-ds-card${selected ? ' selected' : ''}`} onClick={onSelect}>
      <div className="eval-ds-head">
        <span className="eval-ds-icon">{meta.icon}</span>
        <span className="eval-ds-title">{source.name}</span>
        <span className="eval-ds-badge">{sourceBadge(source)}</span>
      </div>
      <div className="eval-ds-desc eval-ds-summary">
        <span>{sourceSummary(source, options)}</span>
        <span className="eval-ds-actions" onClick={(e) => e.stopPropagation()}>
          <Tooltip title="编辑">
            <Button size="small" type="text" icon={<EditOutlined />} onClick={onEdit} />
          </Tooltip>
          <Popconfirm
            title="删除该数据源配置？"
            description="已保存的连接凭据会一并删除。"
            okText="删除"
            okButtonProps={{ danger: true }}
            onConfirm={onDelete}
          >
            <Button size="small" type="text" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </span>
      </div>
    </div>
  );
}

function SourceForm({ form, onChange, options, defaults, projectId, firstTime }) {
  const field = (key) => ({
    value: form[key] ?? '',
    onChange: (e) => onChange({ [key]: e.target.value }),
  });
  const kindDisabled = (kind) => kind === 'mock' && !options.mock_tables;
  const kindBadge = (kind) => {
    if (kind === 'mock') {
      return options.mock_tables ? `${options.mock_tables} 张示例表` : '需先完成「示例数据」阶段';
    }
    if (kind === 'dataset') {
      return form.files?.length ? `已上传 ${form.files.length} 份` : '上传 Excel';
    }
    if (kind === 'connection') return 'NL2SQL / Neo4j';
    return '零配置';
  };
  return (
    <div className="eval-ds-view">
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
        {firstTime && '运行评测前需先配置数据源，保存后下次评测可直接选用。'}
        {'评测时 consumer 智能体挂载知识包作答；接入数据源后，涉及具体数据的问题会实际查询数据源，' +
          '不接则仅按知识包语义作答。'}
      </Typography.Paragraph>
      <Input
        addonBefore="配置名称"
        placeholder="留空自动命名"
        maxLength={60}
        {...field('name')}
      />
      {VISIBLE_SOURCE_KINDS.map((kind) => {
        const disabled = kindDisabled(kind.value);
        const selected = form.kind === kind.value;
        return (
          <div
            key={kind.value}
            className={`eval-ds-card${selected ? ' selected' : ''}${disabled ? ' disabled' : ''}`}
            onClick={() => !disabled && onChange({ kind: kind.value })}
          >
            <div className="eval-ds-head">
              <span className="eval-ds-icon">{kind.icon}</span>
              <span className="eval-ds-title">{kind.title}</span>
              <span className="eval-ds-badge">{kindBadge(kind.value)}</span>
            </div>
            <div className="eval-ds-desc">{kind.desc}</div>
            {selected && kind.value === 'dataset' && (
              <div className="eval-ds-files" onClick={(e) => e.stopPropagation()}>
                <DataFileUpload projectId={projectId} form={form} onChange={onChange} />
              </div>
            )}
            {selected && kind.value === 'connection' && (
              <div className="eval-ds-files" onClick={(e) => e.stopPropagation()}>
                <ConnectionFields
                  form={form}
                  field={field}
                  onChange={onChange}
                  defaults={defaults}
                  options={options}
                  projectId={projectId}
                />
              </div>
            )}
          </div>
        );
      })}
      {(form.kind === 'mock' || form.kind === 'dataset') && (
        <div className="eval-ds-flow">
          <span>数据表</span>
          <span className="eval-ds-arrow">→</span>
          <span>评测库</span>
          <span className="eval-ds-arrow">→</span>
          <span>NL2SQL 绑定</span>
          <span className="eval-ds-arrow">→</span>
          <span>consumer 查询作答</span>
        </div>
      )}
      <ImportPreview projectId={projectId} request={importPreviewRequest(form)} />
    </div>
  );
}

// 业务数据表：在表单里直接上传 Excel（一个 Sheet 一张表），保存配置时引用文件 ID
function DataFileUpload({ projectId, form, onChange }) {
  const files = form.files || [];
  const upload = async (file) => {
    onChange((current) => ({ uploading: (current.uploading || 0) + 1 }));
    try {
      const { file: saved } = await api.uploadEvalDataFile(projectId, file);
      onChange((current) => ({ files: [...(current.files || []), saved] }));
    } catch (error) {
      message.error(`${file.name}：${error.message}`);
    } finally {
      onChange((current) => ({ uploading: Math.max(0, (current.uploading || 0) - 1) }));
    }
  };
  const remove = (id) =>
    onChange((current) => ({ files: (current.files || []).filter((f) => f.id !== id) }));
  return (
    <>
      {files.map((f) => (
        <div key={f.id} className="eval-ds-file">
          <span className="eval-ds-file-name">{f.name}</span>
          <span className="eval-ds-file-size">{Math.round(f.size / 1024)} KB</span>
          <Tooltip title="移除">
            <Button
              size="small"
              type="text"
              icon={<DeleteOutlined />}
              aria-label={`移除 ${f.name}`}
              onClick={() => remove(f.id)}
            />
          </Tooltip>
        </div>
      ))}
      <Upload
        accept=".xlsx,.xlsm"
        multiple
        showUploadList={false}
        beforeUpload={(file) => {
          upload(file);
          return false;
        }}
      >
        <Button size="small" icon={<UploadOutlined />} loading={form.uploading > 0}>
          {files.length ? '继续上传' : '上传 Excel 数据表'}
        </Button>
      </Upload>
      <div className="eval-conn-hint">
        {'一个 Sheet 对应一张表、首行为列名；表名与列名需与对象文档声明的来源表、属性英文名一致。'}
      </div>
    </>
  );
}

function ConnectionFields({ form, field, onChange, defaults, options, projectId }) {
  const savedSecret = (has) => (has ? '已保存，留空沿用' : '');
  return (
    <div className="eval-conn-fields">
      <label className="eval-conn-field eval-conn-wide">
        <span>Provider</span>
        <Select
          size="small"
          value={form.provider}
          options={PROVIDER_OPTIONS}
          onChange={(provider) => onChange({ provider })}
        />
      </label>
      {form.provider === 'neo4j' ? (
        <>
          <ConnField label="Neo4j URL" placeholder="bolt://neo4j:7687" {...field('uri')} />
          <ConnField label="Database" placeholder="neo4j" {...field('database')} />
          <ConnField label="User" placeholder="neo4j" {...field('username')} />
          <ConnField
            label="Password"
            password
            placeholder={savedSecret(form.has_password) || '用于连接测试并同步至评测服务'}
            {...field('password')}
          />
          <ConnField label="Workspace 隔离字段" {...field('workspace_property')} />
          <ConnField label="Workspace 对应值" placeholder="例如 graph-eval" {...field('workspace_value')} />
          <ConnField
            label="Ontology 隔离字段"
            placeholder={form.graph_import === 'none' ? '留空则不按知识包隔离' : '导入图数据时必填'}
            {...field('ontology_property')}
          />
          <ConnField label="Ontology 对应值" value={defaults.ontology_id || ''} disabled />
          <div className="eval-conn-hint">
            {'Workspace 对应值同时作为 consumer 评测工作空间 ID（小写字母开头，仅小写字母、数字、_、-，' +
              '最长 64 个字符，已存在时直接复用）；Ontology 对应值固定为当前知识包 ID，发起评测时自动填写。' +
              '两组隔离条件按 AND 联合匹配。'}
          </div>
          <div className="eval-conn-field eval-conn-wide">
            <span>评测前导入图数据</span>
            <Segmented
              size="small"
              value={form.graph_import}
              options={GRAPH_IMPORT_OPTIONS.map((item) => ({
                ...item,
                disabled: item.value === 'mock' && !options.mock_tables,
              }))}
              onChange={(graph_import) => onChange({ graph_import })}
            />
          </div>
          {form.graph_import === 'dataset' && (
            <div className="eval-conn-wide">
              <DataFileUpload projectId={projectId} form={form} onChange={onChange} />
            </div>
          )}
          {form.graph_import !== 'none' && (
            <div className="eval-conn-hint">
              {'每轮评测导入知识包后，按知识包 mapping 把数据写成节点与关系；写入前清空本隔离范围' +
                '（Workspace 对应值 + 知识包 ID）内的旧数据，不影响图库里的其他数据。'}
            </div>
          )}
        </>
      ) : (
        <>
          <ConnField label="数据服务 URL" placeholder="http://localhost:5600" {...field('url')} />
          <ConnField label="Scene" placeholder="例如 mv_shadow_project_id" {...field('scene')} />
          <ConnField
            label="Token"
            password
            placeholder={savedSecret(form.has_token) || '服务启用鉴权时填写（可选）'}
            {...field('token')}
          />
          <ConnField
            label="评测工作空间 ID"
            placeholder="例如 nl2sql-eval"
            {...field('evaluation_workspace_id')}
          />
          <div className="eval-conn-hint">
            {'Scene 对应数据服务里的 MySQL 数据库。连接在每轮评测导入知识包后由 modeling ' +
              '测试并同步到评测服务，连接失败时评测会停在「数据源」步骤并给出原因。'}
          </div>
        </>
      )}
    </div>
  );
}

function ConnField({ label, password = false, ...props }) {
  const Control = password ? Input.Password : Input;
  return (
    <label className="eval-conn-field">
      <span>{label}</span>
      <Control size="small" autoComplete="off" {...props} />
    </label>
  );
}

// 一键导入预览：导入哪些表、日期如何处理、对象绑到哪张表（即发布包 mapping 的内容）。
// 对象绑不到表 = 这一轮问答根本查不到库；发起前就说清楚，别等报告出分
function ImportPreview({ projectId, request }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const key = request ? JSON.stringify(request) : '';
  useEffect(() => {
    if (!key) {
      setData(null);
      return undefined;
    }
    let active = true;
    setLoading(true);
    const timer = setTimeout(() => {
      api
        .evalImportPreview(projectId, JSON.parse(key))
        .then((result) => active && setData(result))
        .catch((error) => active && setData({ error: error.message }))
        .finally(() => active && setLoading(false));
    }, 300);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [projectId, key]);
  if (!key) return null;
  if (!data) return loading ? <Spin size="small" /> : null;
  if (data.error) return <Alert type="error" showIcon message={data.error} />;
  if (!data.tables?.length && !data.problems?.length) return null;
  const objects = Object.entries(data.objects || {});
  const bound = objects.filter(([, v]) => v.table);
  const dateNotes = data.tables.flatMap((t) => [
    ...t.date_columns.map((c) => `${t.name}.${c}`),
  ]);
  return (
    <Spin spinning={loading}>
      <div className="eval-import-preview">
        <div className="eval-import-title">
          导入预览
          <Typography.Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
            {`${data.tables.length} 张表 · ${data.tables.reduce((n, t) => n + t.rows, 0)} 行 · `}
            {`${bound.length}/${objects.length} 个对象进入 mapping`}
          </Typography.Text>
        </div>
        {bound.map(([key, item]) => (
          <div key={key} className="eval-import-row">
            <span className="eval-import-object">{key}</span>
            <span className="eval-ds-arrow">→</span>
            <Tag>{item.table}</Tag>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {`${Object.keys(item.columns).length} 列对齐`}
              {item.missing.length ? `，缺 ${item.missing.slice(0, 3).join('、')}` : ''}
            </Typography.Text>
          </div>
        ))}
        {dateNotes.length > 0 && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {`日期列已规范化：${dateNotes.slice(0, 6).join('、')}${dateNotes.length > 6 ? ' 等' : ''}`}
          </Typography.Text>
        )}
        {data.problems.length > 0 && (
          <Alert
            type="error"
            showIcon
            message="导入数据未通过检查，运行评测会被拦下"
            description={data.problems.slice(0, 5).map((text) => <div key={text}>{text}</div>)}
          />
        )}
        {data.warnings.length > 0 && (
          <Alert
            type="warning"
            showIcon
            message={`${data.warnings.length} 条提示`}
            description={data.warnings.slice(0, 6).map((text) => <div key={text}>{text}</div>)}
          />
        )}
      </div>
    </Spin>
  );
}
