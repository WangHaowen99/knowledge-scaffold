import { missingPublishStages } from '../lib/packagePublish';
import {
  DeleteOutlined,
  InboxOutlined,
  TagOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import {
  Alert,
  Button,
  Checkbox,
  Divider,
  Dropdown,
  Input,
  List,
  Modal,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Spin,
  Steps,
  Tag,
  Tooltip,
  Timeline,
  Tree,
  Typography,
  Upload,
  message,
} from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../lib/api.js';
import { openEvaluation, prefetchEvaluationEntry } from '../lib/evaluation.js';
import { isResearchBlocking } from '../lib/researchGate.js';
import { EMPTY_HISTORY, appendHistoryPage, historyEntryKey, versionTitle } from '../lib/versionHistory.js';
import { hasWorkspaceDirty } from '../lib/workspaceDirty.js';
import EvalRunDetail from './EvalRunDetail.jsx';
import { usePackagePublish } from './PublishCheckDialog.jsx';

// 阶段定义：探测产物目录推断完成态（对应 CLI 版 detect-stage），
// actions 是该阶段的操作入口；command 以空格结尾表示"填入输入框待补参数"
// 有产物 = 目录下存在真实文件：排除目录节点本身（尾 '/'，新项目会预建全部目录）
// 与目录索引 index.md
const hasEntry = (paths, ...prefixes) =>
  paths.some(
    (p) =>
      prefixes.some((prefix) => p.startsWith(prefix)) &&
      !p.endsWith('/') &&
      !p.endsWith('/index.md'),
  );

export const STAGES = [
  {
    key: 'research',
    title: '资料分析',
    // 探针只回答"有没有报告"。是否完成由验收回执决定，见 stageStatuses（issue #362）
    probe: (paths) => hasEntry(paths, 'research/'),
    actions: [
      { label: '联网资料分析', command: '/ontology-research ' },
      { label: '仅本地资料', command: '/ontology-research 仅基于本地文档 ' },
      { label: '追加分析', command: '/ontology-research 追加' },
      {
        label: '核验报告',
        // 报告挡着后续阶段时，聊天区把这个入口排到最前（lib/researchGate.js）
        verify: true,
        command:
          '运行 python3 .claude/scripts/research_checker.py 核验资料分析报告，逐条列出错误项与来源位置',
      },
    ],
  },
  {
    key: 'flowchart',
    title: '流程图（可选）',
    optional: true,
    probe: (paths) => hasEntry(paths, 'ontology/diagrams/'),
    actions: [
      { label: '创建/打磨流程图', command: '/ontology-flowchart' },
      { label: '全量刷新', command: '/ontology-flowchart 全量刷新' },
      { label: '校验流程图', command: '/ontology-validate-diagrams' },
    ],
  },
  {
    key: 'object',
    title: '对象提取',
    probe: (paths) => hasEntry(paths, 'ontology/object/'),
    actions: [
      { label: '提取对象', command: '/ontology-extract-object' },
      { label: '迁移提取', command: '迁移提取业务对象 ' },
    ],
  },
  {
    key: 'operation',
    title: '操作提取',
    // ontology/logic 为旧目录兼容（范式升级 ADR-0004）
    probe: (paths) => hasEntry(paths, 'ontology/operation/', 'ontology/logic/'),
    actions: [{ label: '提取操作', command: '/ontology-extract-operation' }],
  },
  {
    key: 'rulemetric',
    title: '规则与指标（可选）',
    optional: true,
    probe: (paths) => hasEntry(paths, 'ontology/rule/', 'ontology/metric/'),
    actions: [{ label: '提取规则与指标', command: '/ontology-extract-rule-metric' }],
  },
  {
    key: 'validate',
    title: '质量检查',
    probe: (paths) => hasEntry(paths, 'reports/'),
    actions: [{ label: '运行质量检查', command: '/ontology-validate' }],
  },
  {
    key: 'mock',
    title: '示例数据',
    probe: (paths) => hasEntry(paths, 'ontology/data-mock/'),
    actions: [{ label: '生成示例数据', command: '/ontology-mock' }],
  },
  {
    key: 'trace',
    title: '场景验证',
    probe: (paths) => hasEntry(paths, 'ontology/testcase/'),
    actions: [
      { label: '批量验证', command: '/ontology-trace 全部' },
      { label: '交互走查', command: '/ontology-trace 交互' },
    ],
  },
  {
    key: 'codegen',
    title: '代码生成（可选）',
    optional: true,
    probe: (paths) => hasEntry(paths, 'ontology/scripts/'),
    actions: [
      { label: '扫描候选', command: '/ontology-codegen-scan' },
      { label: '生成脚本', command: '/ontology-codegen ' },
    ],
  },
  {
    key: 'solver',
    title: '求解适配（可选）',
    optional: true,
    panel: true, // 入口是中栏「求解模型」面板，不走聊天命令
    probe: (paths) => hasEntry(paths, 'ontology/solver/'),
    actions: [],
  },

];

export const RESEARCH_STAGE_INDEX = STAGES.findIndex((stage) => stage.key === 'research');

// 资料分析阶段的完成态不看产物树：报告在 research/，验收回执在 .ontology-state/
// （不进产物树）。review 取后端项目进度的 research_review；只有它声明 blocking
// （未验收且对象尚未提取）时才把资料分析判为未完成。未取到时沿用原路径探针，
// 不凭空判未通过。
export function stageHits(paths, review = null) {
  const hits = STAGES.map((stage) => stage.probe(paths));
  if (hits[RESEARCH_STAGE_INDEX] && isResearchBlocking(review)) {
    hits[RESEARCH_STAGE_INDEX] = false;
  }
  return hits;
}

// 每个阶段的展示状态：可选阶段未完成不阻塞后续（finish/process/error/wait）
export function stageStatuses(paths, review = null) {
  const hits = stageHits(paths, review);
  let firstPending = -1;
  for (let i = 0; i < STAGES.length; i += 1) {
    if (!hits[i] && !STAGES[i].optional) {
      firstPending = i;
      break;
    }
  }
  return STAGES.map((stage, i) => {
    if (hits[i]) return 'finish';
    // 报告已生成但检查出确定性错误：显示未通过，而不是"进行中"
    if (i === RESEARCH_STAGE_INDEX && ['failed', 'unverifiable'].includes(review?.status)) return 'error';
    if (i === firstPending) return 'process';
    return 'wait';
  });
}

// 资料分析阶段标题后的状态角标；无需提示时为空
export function researchBadge(review) {
  if (!review) return '';
  if (review.status === 'unverifiable') return '来源无法核验（需修正读取或来源位置）';
  if (review.status === 'failed') return `未通过（${review.errors || 0} 项错误）`;
  if (review.status !== 'pending') return '';
  return review.manual_pending
    ? `待核验（${review.manual_pending} 项待人工确认）`
    : '已生成，待核验';
}

// 资料用途（对齐 modeling 的 material purpose 体系）；schema/term 仅表格类可选。
// 评测数据不在资料区打标，改在「数据评测」的数据源里上传
export const MATERIAL_PURPOSES = [
  { value: 'auto', label: '由助手判断' },
  { value: 'goal', label: '建模目标依据' },
  { value: 'schema', label: '数据 Schema', tableOnly: true },
  { value: 'term', label: '术语表', tableOnly: true },
  { value: 'evidence', label: '通用业务依据' },
];
const isTableFile = (name) => /\.(xlsx?|csv)$/i.test(name);

// 运行中的 /ontology-* 命令 → 所属阶段（左栏活跃高亮用）
export function stageForCommand(text) {
  const cmd = String(text || '').trim().split(/\s/)[0];
  if (!cmd.startsWith('/ontology')) return null;
  for (const stage of STAGES) {
    for (const action of stage.actions) {
      if (action.command.trim().split(/\s/)[0] === cmd) return stage.key;
    }
  }
  return null;
}

const EXTRA_ACTIONS = [
  { label: '整体视图', command: '/ontology-overview' },
  { label: '增量更新', command: '/ontology-update ' },
];

function flattenPaths(nodes, out = []) {
  for (const node of nodes) {
    out.push(node.path + (node.type === 'dir' ? '/' : ''));
    if (node.children) flattenPaths(node.children, out);
  }
  return out;
}

// 供 App 顶栏与概览复用：从产物树推断阶段进度
export function stageProgress(tree, review = null) {
  const paths = flattenPaths(tree);
  const statuses = stageStatuses(paths, review);
  const done = statuses.filter((status) => status === 'finish').length;
  const pending = statuses.indexOf('process');
  const current = pending >= 0 ? STAGES[pending].title : null;
  return { done, total: STAGES.length, current };
}

function toTreeData(nodes, keyword) {
  const kw = (keyword || '').toLowerCase();
  const filter = (list) =>
    list
      .map((node) => {
        const children = node.children ? filter(node.children) : undefined;
        const hit = !kw || node.name.toLowerCase().includes(kw);
        if (node.type === 'dir') {
          if (children && children.length > 0) {
            return { key: node.path, title: node.name, isLeaf: false, children };
          }
          return hit && kw ? { key: node.path, title: node.name, isLeaf: false, children: [] } : null;
        }
        return hit ? { key: node.path, title: node.name, isLeaf: true } : null;
      })
      .filter(Boolean);
  return filter(nodes);
}

function collectResources(nodes) {
  const files = [];
  const walk = (list) => {
    for (const node of list) {
      if (
        node.type === 'file' &&
        node.path.startsWith('resource/') &&
        node.name.toLowerCase() !== 'readme.md' &&
        node.path !== 'resource/materials.json' &&
        // 图例定义供流程图工具链使用（摘要节点分类依赖），不作为用户资料展示
        node.path !== 'resource/diagrams/图示说明定义.drawio'
      )
        files.push(node);
      if (node.children) walk(node.children);
    }
  };
  walk(nodes);
  return files;
}

export default function LeftPane({
  project,
  meta,
  tree,
  versions = [],
  versionNextOffset = null,
  activeStage = null,
  onSelectFile,
  onRefresh,
  onReverted,
  onLoadMoreVersions,
  onCommand,
  onKnowledge,
  onSolver,
}) {
  const [section, setSection] = useState('progress');
  const [expandedStage, setExpandedStage] = useState(null);
  const [treeKeyword, setTreeKeyword] = useState('');
  const [uploadSubdir, setUploadSubdir] = useState('auto');
  const [tagging, setTagging] = useState(null); // {commit, label}
  const [reverting, setReverting] = useState(null);
  const [loadingVersions, setLoadingVersions] = useState(false);

  const paths = useMemo(() => flattenPaths(tree), [tree]);
  // 资料分析的验收状态由后端项目进度给出（回执不在产物树里）
  const researchReview = meta?.research_review || null;
  const researchStatus = researchReview?.status || null;
  const researchBlocking = isResearchBlocking(researchReview);
  const researchTag = researchBadge(researchReview);
  const statuses = useMemo(() => {
    const base = stageStatuses(paths, { status: researchStatus, blocking: researchBlocking });
    // 会话正在执行某阶段命令时，进行中的高亮跟随实际执行的阶段
    const activeIndex = activeStage ? STAGES.findIndex((s) => s.key === activeStage) : -1;
    if (activeIndex < 0) return base;
    return base.map((status, index) =>
      index === activeIndex ? 'process' : status === 'process' ? 'wait' : status,
    );
  }, [paths, activeStage, researchStatus, researchBlocking]);
  const resources = useMemo(() => collectResources(tree), [tree]);
  const [deleting, setDeleting] = useState(null);
  const [directives, setDirectives] = useState({});
  // 结构上像数据表、用途仍是「由助手判断」的表格资料：{路径: {tables}}，可一键标为 schema
  const [schemaCandidates, setSchemaCandidates] = useState({});
  const [schemaScopes, setSchemaScopes] = useState({});
  const [sheetEditor, setSheetEditor] = useState(null);
  const [savingSheets, setSavingSheets] = useState(false);
  const [checkingSheets, setCheckingSheets] = useState(false);
  const schemaChecking = useRef(false);
  const schemaRequest = useRef(0);
  const invalidateSchemaRequest = () => {
    schemaRequest.current += 1;
    schemaChecking.current = false;
    setCheckingSheets(false);
  };
  const currentProjectId = useRef(project.id);
  currentProjectId.current = project.id;
  const loadDirectives = () => {
    const projectId = project.id;
    return api
      .resourceDirectives(projectId)
      .then((data) => {
        if (currentProjectId.current !== projectId) return;
        setDirectives(data.directives || {});
        setSchemaCandidates(data.schema_candidates || {});
        setSchemaScopes(data.schema_scopes || {});
      })
      .catch(() => {});
  };

  useEffect(() => {
    invalidateSchemaRequest();
    setDirectives({});
    setSchemaCandidates({});
    setSchemaScopes({});
    setSheetEditor(null);
  }, [project.id]);

  useEffect(() => {
    if (section !== 'resources') return;
    loadDirectives();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [section, project.id, tree]);

  // Legacy results are loaded only when explicitly opened; new evaluations live independently.
  const [evalInfo, setEvalInfo] = useState(null); // {enabled, reachable, state, evolution}
  const [evalDetailOpen, setEvalDetailOpen] = useState(false);
  const packagePublish = usePackagePublish({
    onCommand,
    onOpenFile: onSelectFile,
    onChanged: onRefresh,
    onOpenSolver: onSolver,
  });
  // 「历史」页签：后端合并迭代与发布版本并分页；打开页签或迭代刷新时从第一页重新读取
  const [history, setHistory] = useState(EMPTY_HISTORY);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyRevision, setHistoryRevision] = useState(0);
  const [downloading, setDownloading] = useState('');
  useEffect(() => setHistory(EMPTY_HISTORY), [project.id]);
  // 失败不阻断：点击“评测”时会重新读取并给出原因
  useEffect(() => { if (section === 'versions') prefetchEvaluationEntry().catch(() => {}); }, [section]);
  useEffect(() => {
    if (section !== 'versions') return undefined;
    let active = true;
    setHistoryLoading(true);
    api.history(project.id)
      .then((page) => { if (active) setHistory(appendHistoryPage(EMPTY_HISTORY, page)); })
      .catch((error) => { if (active) setHistory({ ...EMPTY_HISTORY, error: error.message }); })
      .finally(() => { if (active) setHistoryLoading(false); });
    return () => { active = false; };
  }, [section, project.id, versions, historyRevision]);

  const loadMoreHistory = async () => {
    const cursor = history.nextCursor;
    setHistoryLoading(true);
    try {
      const page = await api.history(project.id, { cursor });
      // 读取期间列表已重新加载（游标变了）时丢弃这一页
      setHistory((prev) => (prev.nextCursor === cursor ? appendHistoryPage(prev, page) : prev));
    } catch (error) {
      message.error(error.message);
    } finally {
      setHistoryLoading(false);
    }
  };

  const downloadRelease = async (releaseId) => {
    setDownloading(releaseId);
    try {
      await api.downloadRelease(project.id, releaseId);
    } catch (error) {
      message.error(error.message);
    } finally {
      setDownloading('');
    }
  };

  useEffect(() => {
    if (!evalDetailOpen) return undefined;
    let stop = false;
    const pull = () =>
      api.evalStatus(project.id).then((d) => {
        if (!stop) setEvalInfo(d);
      }).catch(() => {});
    pull();
    const timer = setInterval(() => {
      // 运行中密集轮询，空闲时低频
      if (evalInfo?.state?.status === 'running' || evalDetailOpen) pull();
    }, 8000);
    return () => {
      stop = true;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [section, project.id, evalInfo?.state?.status, evalDetailOpen]);

  // 评测进入终态：刷新文件树与项目 meta——报告出现在树里、阶段点亮，
  // 聊天区的「评测优化」建议（依赖 meta.eval_evolution）才能及时出现
  const evalStatusRef = useRef(null);
  useEffect(() => {
    const status = evalInfo?.state?.status;
    if (evalStatusRef.current === 'running' && status && status !== 'running') onRefresh?.();
    evalStatusRef.current = status;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [evalInfo?.state?.status]);

  const [evalActing, setEvalActing] = useState(false); // 停止/续接请求进行中
  const refreshEvalStatus = () =>
    api.evalStatus(project.id).then(setEvalInfo).catch(() => {});

  const stopEval = async () => {
    setEvalActing(true);
    try {
      const res = await api.evalStop(project.id);
      message.success(
        res.cancelled ? '已停止评测，modeling 侧轮次已取消' : '已停止评测',
      );
    } catch (err) {
      message.error(err.message || '停止评测失败');
    } finally {
      setEvalActing(false);
      refreshEvalStatus();
    }
  };

  const resumeEval = async () => {
    setEvalActing(true);
    try {
      await api.evalResume(project.id);
      message.info('正在续接上一轮：收取 modeling 侧已有结果，不重跑用例');
      // 乐观置为运行中，让密集轮询立刻生效；随后的真实状态会覆盖它
      setEvalInfo((prev) =>
        prev
          ? { ...prev, state: { ...prev.state, status: 'running', message: '续接上一轮…' } }
          : prev,
      );
    } catch (err) {
      message.error(err.message || '续接失败');
      refreshEvalStatus();
    } finally {
      setEvalActing(false);
    }
  };


  // 失分/异常用例 → /ontology-evolution 归因修订（与聊天区建议同一判定）
  const evolution = evalInfo?.evolution;
  const runEvolution = () =>
    onCommand(`/ontology-evolution ${evolution.report.split('/').pop()}`);

  const openSheetScope = async (path, readingMode, saveIfValid = false, force = false) => {
    if (savingSheets) return;
    const projectId = project.id;
    const version = ++schemaRequest.current;
    schemaChecking.current = true;
    setCheckingSheets(true);
    try {
      const scope = await api.previewResourceSchema(projectId, path, force);
      if (currentProjectId.current !== projectId || schemaRequest.current !== version) return;
      if (saveIfValid && !scope.error && scope.sheets.length > 0 &&
          scope.sheets.every((sheet) => !sheet.issues?.length)) {
        await api.setResourceDirective(projectId, path, 'schema', readingMode, {
          mode: 'manual', included_sheets: scope.included_sheets, file_sha256: scope.file_sha256,
        });
        if (currentProjectId.current !== projectId || schemaRequest.current !== version) return;
        await loadDirectives();
        onRefresh?.();
        message.success(`已设为数据 Schema，选中 ${scope.included_sheets.length} 个数据 Sheet`);
        return;
      }
      setSheetEditor({ projectId, path, readingMode, scope, selected: scope.included_sheets || [] });
    } catch (error) {
      if (currentProjectId.current !== projectId || schemaRequest.current !== version) return;
      setSheetEditor({ projectId, path, readingMode, selected: [], scope: { sheets: [], error: error.message } });
    } finally {
      if (currentProjectId.current === projectId && schemaRequest.current === version) {
        schemaChecking.current = false;
        setCheckingSheets(false);
      }
    }
  };

  const changeDirective = async (path, purpose, readingMode) => {
    if (purpose === 'schema') {
      await openSheetScope(path, readingMode, true);
      return;
    }
    invalidateSchemaRequest();
    try {
      const saved = await api.setResourceDirective(project.id, path, purpose, readingMode);
      setDirectives((prev) => ({ ...prev, [path]: saved }));
      await loadDirectives();
      onRefresh?.();
      setSchemaCandidates((prev) => {
        const { [path]: _removed, ...rest } = prev;
        return rest;
      });
    } catch (error) {
      message.error(error.message);
    }
  };

  const saveSheetScope = async () => {
    if (!sheetEditor || savingSheets || schemaChecking.current ||
        !sheetEditor.scope.file_sha256 || sheetEditor.projectId !== project.id) return;
    invalidateSchemaRequest();
    const version = schemaRequest.current;
    setSavingSheets(true);
    const projectId = project.id;
    try {
      await api.setResourceDirective(projectId, sheetEditor.path, 'schema',
        sheetEditor.readingMode || directives[sheetEditor.path]?.reading_mode || 'on_demand', {
          mode: 'manual', included_sheets: sheetEditor.selected,
          file_sha256: sheetEditor.scope.file_sha256,
        });
      if (currentProjectId.current !== projectId || schemaRequest.current !== version) return;
      setSheetEditor(null);
      await loadDirectives();
      if (currentProjectId.current !== projectId || schemaRequest.current !== version) return;
      onRefresh?.();
      message.success('Sheet 范围已保存；请重新执行受影响的资料分析和字段核验');
    } catch (error) {
      if (currentProjectId.current !== projectId || schemaRequest.current !== version) return;
      setSheetEditor((prev) => prev && ({ ...prev, scope: { ...prev.scope, error: error.message, file_sha256: null } }));
    } finally {
      setSavingSheets(false);
    }
  };

  // 资料变更提醒：资料上传/删除时间晚于最近迭代 → 派生产物可能过期
  const latestVersionTime = versions[0]?.time || 0;
  const resourceStale =
    (meta?.resource_updated_at || 0) > latestVersionTime && hasEntry(paths, 'research/');

  const autoSubdir = (name) => {
    const lower = name.toLowerCase();
    if (lower.endsWith('.xlsx') || lower.endsWith('.xls')) return 'excel';
    if (lower.endsWith('.drawio')) return 'diagrams';
    return 'others';
  };

  const upload = async (file, forcedSubdir = null) => {
    try {
      const subdir =
        forcedSubdir || (uploadSubdir === 'auto' ? autoSubdir(file.name) : uploadSubdir);
      const saved = await api.uploadResource(project.id, subdir, file);
      const done = saved.replaced ? `已替换 ${file.name}` : `已上传 ${file.name}`;
      message.success(
        saved.detected
          ? `${done}，识别为数据表（${saved.detected.tables.length} 张），用途已设为「数据 Schema」，可在资料列表修改`
          : done,
      );
      onRefresh();
    } catch (error) {
      message.error(error.message);
    }
    return false;
  };

  const removeResource = async (path) => {
    try {
      setDeleting(path);
      await api.deleteResource(project.id, path);
      message.success('已删除');
      onRefresh();
    } catch (error) {
      message.error(error.message);
    } finally {
      setDeleting(null);
    }
  };

  const revertTo = async (version) => {
    if (hasWorkspaceDirty()) {
      message.warning('当前页面有未保存修改，请先保存或放弃后再回退');
      return;
    }
    try {
      setReverting(version.commit);
      const preview = await api.revertPreview(project.id, version.commit);
      if (preview.ready === false) {
        throw new Error(preview.blockers?.[0]?.message || '当前项目暂不能回退');
      }
      const { added, modified, deleted } = preview.changes;
      await new Promise((resolve, reject) => {
        Modal.confirm({
          title: `回退到版本 ${version.version}？`,
          content: `将新增 ${added.length}、修改 ${modified.length}、删除 ${deleted.length} 个业务文件；项目设置和运行状态不会回退。`,
          okText: '确认回退',
          okButtonProps: { danger: true },
          cancelText: '取消',
          onOk: () => resolve(),
          onCancel: () => reject(new Error('cancelled')),
        });
      });
      const result = await api.revert(project.id, version.commit, preview.expected_head);
      message.success('已回退并生成新版本');
      if (result.warning) message.warning(result.warning);
      if (onReverted) onReverted();
      else onRefresh();
    } catch (error) {
      if (error.message !== 'cancelled') message.error(error.message);
    } finally {
      setReverting(null);
    }
  };

  const saveTag = async () => {
    if (!tagging?.label?.trim()) return;
    try {
      await api.tagVersion(project.id, tagging.commit, tagging.label.trim());
      message.success('已标记');
      setTagging(null);
      onRefresh();
    } catch (error) {
      message.error(error.message);
    }
  };

  return (
    <div>
      <Segmented
        block
        size="small"
        value={section}
        onChange={setSection}
        options={[
          { label: '进度', value: 'progress' },
          { label: '知识包', value: 'files' },
          { label: '资料', value: 'resources' },
          { label: '历史', value: 'versions' },
        ]}
        style={{ marginBottom: 12 }}
      />

      {section === 'progress' && (
        <>
          <Steps
            direction="vertical"
            size="small"
            className="stage-steps"
            items={STAGES.map((stage, index) => ({
              status: statuses[index],
              title: (
                <span
                  style={{ cursor: 'pointer' }}
                  onClick={() =>
                    setExpandedStage(expandedStage === stage.key ? null : stage.key)
                  }
                >
                  {stage.title}
                  {stage.key === 'research' && researchTag && (
                    <Tooltip title={researchReview?.hint || ''}>
                      <Tag
                        color={researchStatus === 'failed' ? 'red' : 'gold'}
                        style={{ marginLeft: 6 }}
                      >
                        {researchTag}
                      </Tag>
                    </Tooltip>
                  )}
                </span>
              ),
              description:
                expandedStage === stage.key ? (
                  <Space wrap size={4} style={{ marginTop: 4 }}>
                    {stage.key === 'solver' ? (
                      <Button size="small" onClick={() => onSolver?.()}>
                        {statuses[index] === 'finish' ? '打开求解模型' : '启用求解契约'}
                      </Button>
                    ) : (
                      stage.actions.map((action) => (
                        <Button
                          key={action.label}
                          size="small"
                          type={statuses[index] === 'process' ? 'primary' : 'default'}
                          ghost={statuses[index] === 'process'}
                          onClick={() => onCommand(action.command)}
                        >
                          {action.label}
                        </Button>
                      ))
                    )}
                  </Space>
                ) : undefined,
            }))}
          />
          <Divider style={{ margin: '8px 0' }} />
          <Button
            type={missingPublishStages(STAGES, statuses).length ? 'default' : 'primary'}
            block
            className="export-package-btn"
            aria-disabled={missingPublishStages(STAGES, statuses).length > 0}
            style={{ marginBottom: 8, ...(missingPublishStages(STAGES, statuses).length ? {
              color: '#999', background: '#f5f5f5', borderColor: '#d9d9d9', cursor: 'not-allowed',
            } : {}) }}
            loading={packagePublish.publishing}
            onClick={() => {
              const missing = missingPublishStages(STAGES, statuses);
              if (missing.length) {
                message.info(`建模未完成，请先完成：${missing.map((stage) => stage.title).join('、')}`);
                return;
              }
              packagePublish.publish(project.id);
            }}
          >
            发布知识包
          </Button>
          <Button block style={{ marginBottom: 8 }} onClick={() => packagePublish.showVersions(project.id)}>
            已发布版本（知识包评测）
          </Button>
          <Button block style={{ marginBottom: 8 }} onClick={() => setEvalDetailOpen(true)}>
            旧版评测记录（历史链路）
          </Button>
          {evalDetailOpen && evalInfo?.state?.status === 'running' &&
            <Button danger loading={evalActing} onClick={stopEval}>停止旧任务</Button>}
          {evalDetailOpen && evalInfo?.state?.resumable &&
            <Button loading={evalActing} onClick={resumeEval}>对账旧任务</Button>}
          <Space wrap size={4}>
            {EXTRA_ACTIONS.map((action) => (
              <Button key={action.label} size="small" onClick={() => onCommand(action.command)}>
                {action.label}
              </Button>
            ))}
            <Button size="small" onClick={() => onKnowledge?.()}>
              知识库
            </Button>
          </Space>
        </>
      )}

      {section === 'files' && (
        <>
          <Input.Search
            allowClear
            size="small"
            placeholder="在产物树内搜索文件名"
            value={treeKeyword}
            onChange={(e) => setTreeKeyword(e.target.value)}
            style={{ marginBottom: 8 }}
          />
          {tree.length > 0 ? (
            <Tree.DirectoryTree
              treeData={toTreeData(tree, treeKeyword)}
              defaultExpandAll={!!treeKeyword}
              key={treeKeyword ? 'kw' : 'all'}
              onSelect={(keys, info) => {
                if (info.node.isLeaf) onSelectFile(String(keys[0]));
              }}
              titleRender={(node) =>
                node.isLeaf ? (
                  <Dropdown
                    trigger={['contextMenu']}
                    menu={{
                      items: [
                        { key: 'open', label: '打开' },
                        { key: 'impact', label: '影响分析' },
                      ],
                      onClick: ({ key, domEvent }) => {
                        domEvent.stopPropagation();
                        if (key === 'open') onSelectFile(node.key);
                        else if (key === 'impact') onCommand(`/ontology-impact ${node.key}`);
                      },
                    }}
                  >
                    <span>{node.title}</span>
                  </Dropdown>
                ) : (
                  node.title
                )
              }
            />
          ) : (
            <Typography.Paragraph type="secondary">
              暂无知识包，先在「进度」里开始资料分析
            </Typography.Paragraph>
          )}
        </>
      )}

      {section === 'resources' && (
        <>
          {resourceStale && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 8 }}
              message="资料区有更新"
              description="上传/删除时间晚于最近迭代，派生产物可能已过期。"
              action={
                <Button
                  size="small"
                  onClick={() =>
                    onCommand('/ontology-update 资料区有新变更，请检查并刷新受影响的产物')
                  }
                >
                  让助手刷新
                </Button>
              }
            />
          )}
          <Space.Compact block style={{ marginBottom: 8 }}>
            <Select
              size="small"
              value={uploadSubdir}
              onChange={setUploadSubdir}
              style={{ width: '100%' }}
              options={[
                { value: 'auto', label: '按类型自动归类' },
                { value: 'diagrams', label: '流程图（diagrams）' },
                { value: 'excel', label: '表格（excel）' },
                { value: 'others', label: '其他资料（others）' },
              ]}
            />
          </Space.Compact>
          <Upload.Dragger multiple showUploadList={false} beforeUpload={(f) => upload(f)}>
            <p className="ant-upload-drag-icon">
              <InboxOutlined />
            </p>
            <p className="ant-upload-text">上传领域资料</p>
            <p className="ant-upload-hint">支持 Markdown、Word、Excel、PDF、Drawio、TXT；单个文件不超过 20 MB</p>
          </Upload.Dragger>
          <List
            size="small"
            style={{ marginTop: 8 }}
            dataSource={resources}
            locale={{ emptyText: '暂无资料' }}
            renderItem={(item) => {
              const itemSubdir = item.path.split('/')[1];
              return (
                <List.Item
                  className={deleting === item.path ? 'resource-item deleting' : 'resource-item'}
                  actions={[
                    <Upload
                      key="replace"
                      showUploadList={false}
                      beforeUpload={(f) => upload(f, itemSubdir)}
                    >
                      <Button size="small" type="text" title="替换（上传同名文件覆盖）" icon={<UploadOutlined />} />
                    </Upload>,
                    <Popconfirm
                      key="del"
                      title="删除该资料？"
                      onConfirm={() => removeResource(item.path)}
                    >
                      <Button size="small" type="text" loading={deleting === item.path} icon={<DeleteOutlined />} />
                    </Popconfirm>,
                  ]}
                >
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <Typography.Text
                      style={{ cursor: 'pointer', display: 'block' }}
                      onClick={() => onSelectFile(item.path)}
                      ellipsis
                    >
                      {item.path.split('/').pop()}
                    </Typography.Text>
                    <div className="resource-directive">
                      <Select
                        size="small"
                        variant="borderless"
                        popupMatchSelectWidth={false}
                        value={directives[item.path]?.purpose || 'auto'}
                        onChange={(purpose) =>
                          changeDirective(
                            item.path,
                            purpose,
                            directives[item.path]?.reading_mode || 'on_demand',
                          )
                        }
                        options={MATERIAL_PURPOSES.map((opt) => ({
                          value: opt.value,
                          label: opt.label,
                          disabled: opt.tableOnly && !isTableFile(item.path),
                        }))}
                      />
                      {directives[item.path]?.auto_detected && (
                        <Tooltip title="上传时按表格结构自动识别（数据表或术语表）；改用途即覆盖">
                          <Tag className="resource-detect-tag">自动识别</Tag>
                        </Tooltip>
                      )}
                      {schemaScopes[item.path] && (
                        <Button size="small" type="link"
                          danger={Boolean(schemaScopes[item.path].error)}
                          onClick={() => {
                            invalidateSchemaRequest();
                            const scope = schemaScopes[item.path];
                            const invalid = new Set((scope.sheets || [])
                              .filter((sheet) => sheet.schema_selectable === false || sheet.selectable === false)
                              .map((sheet) => sheet.name));
                            setSheetEditor({
                              projectId: project.id, path: item.path,
                              readingMode: directives[item.path]?.reading_mode,
                              scope,
                              selected: (scope.included_sheets || []).filter((name) => !invalid.has(name)),
                            });
                          }}>
                          {schemaScopes[item.path].error ? 'Sheet 范围待确认' :
                            `数据表 ${schemaScopes[item.path].included_sheets?.length || 0} · 参考 ${schemaScopes[item.path].reference_sheets?.length || 0}`} · 查看范围
                        </Button>
                      )}
                      {schemaCandidates[item.path] && (
                        <Tooltip
                          title={`表头像数据库列名，识别出 ${schemaCandidates[item.path].tables.length} 张表：${schemaCandidates[item.path].tables.slice(0, 8).join('、')}`}
                        >
                          <Button
                            size="small"
                            type="link"
                            className="resource-detect-action"
                            onClick={() =>
                              changeDirective(
                                item.path,
                                'schema',
                                directives[item.path]?.reading_mode || 'on_demand',
                              )
                            }
                          >
                            疑似数据表 · 设为 Schema
                          </Button>
                        </Tooltip>
                      )}
                    </div>
                  </div>
                </List.Item>
              );
            }}
          />
        </>
      )}

      <Modal title="数据 Schema 的 Sheet 范围"
        open={Boolean(sheetEditor && sheetEditor.projectId === project.id)}
        onCancel={() => { invalidateSchemaRequest(); setSheetEditor(null); }} onOk={saveSheetScope}
        closable={!savingSheets} maskClosable={!savingSheets} keyboard={!savingSheets}
        cancelButtonProps={{ disabled: savingSheets }}
        confirmLoading={savingSheets} okText="确认用途与范围"
        okButtonProps={{ disabled: checkingSheets || !sheetEditor?.scope.file_sha256 }}>
        <Typography.Paragraph>
          {sheetEditor?.path}
        </Typography.Paragraph>
        <Typography.Paragraph>
          第一行应直接填写字段名，每列名称唯一且不合并。勾选的 Sheet 用于对象字段提取与数据映射，其余作为参考资料。确认后才保存用途，取消不改变原设置，原文件不会改写。
        </Typography.Paragraph>
        {sheetEditor?.scope.error && <Alert type="warning" showIcon message={sheetEditor.scope.error} />}
        <Checkbox.Group value={sheetEditor?.selected || []}
          onChange={(selected) => setSheetEditor((prev) => ({ ...prev, selected }))}>
          <Space direction="vertical" style={{ marginTop: 12 }}>
            {(sheetEditor?.scope.sheets || []).map((sheet) => (
              <div key={sheet.name}>
              <Checkbox value={sheet.name} disabled={checkingSheets || savingSheets || sheet.schema_selectable === false || sheet.selectable === false}>
                {sheet.name} · {sheetEditor?.selected.includes(sheet.name) ? '数据结构' : '参考资料'}
              </Checkbox>
              {(sheet.issues || []).map((issue, index) => <Alert key={`${issue.code}-${index}`}
                style={{ marginTop: 6 }} showIcon type={issue.severity === 'error' ? 'error' : 'warning'}
                message={`Sheet「${sheet.name}」：${issue.message}`} />)}
              </div>
            ))}
          </Space>
        </Checkbox.Group>
        {!sheetEditor?.selected.length && <Alert style={{ marginTop: 12 }} type="info"
          message="未选择数据 Sheet：全部作为参考资料，不提供数据表字段。" />}
        <Button style={{ marginTop: 12 }} loading={checkingSheets} disabled={checkingSheets || savingSheets}
          onClick={() => openSheetScope(sheetEditor.path, sheetEditor.readingMode, false, true)}>重新检查文件</Button>
      </Modal>

      {section === 'versions' && (
        <>
          {(history.error || history.releaseError) && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 8 }}
              message={history.error
                ? `历史记录读取失败：${history.error}`
                : `发布版本读取失败：${history.releaseError}`}
              action={
                <Button size="small" onClick={() => setHistoryRevision((n) => n + 1)}>
                  重试
                </Button>
              }
            />
          )}
          {history.items.length === 0 && historyLoading && (
            <Spin size="small" style={{ display: 'block', margin: '16px auto' }} />
          )}
          {history.items.length === 0 && !historyLoading && !history.error && (
            <Typography.Paragraph type="secondary">还没有版本记录</Typography.Paragraph>
          )}
          {history.items.length > 0 && (
            <Timeline
              items={history.items.map((v) => ({
                key: historyEntryKey(v),
                color: v.kind === 'release' ? '#722ed1' : v.tags?.length ? 'green' : 'blue',
                children: (
                  <div className="version-item">
                    <div>
                      <Typography.Text strong>{versionTitle(v.kind, v.version)}</Typography.Text>{' '}
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        {new Date(v.time * 1000).toLocaleString()}
                      </Typography.Text>
                    </div>
                    {v.kind === 'release' ? (
                      <>
                        <div className="version-msg">
                          发布知识包{v.source_service ? ` · 来源 ${v.source_service}` : ''}
                        </div>
                        <Space size={4} style={{ marginTop: 2 }}>
                          <Button
                            size="small"
                            type="text"
                            loading={downloading === v.release_id}
                            onClick={() => downloadRelease(v.release_id)}
                          >
                            下载
                          </Button>
                          <Button
                            size="small"
                            type="text"
                            onClick={() => openEvaluation(v).catch((e) => message.error(e.message))}
                          >
                            评测
                          </Button>
                        </Space>
                      </>
                    ) : (
                      <>
                        <div className="version-msg">{v.message}</div>
                        {v.tags?.map((t) => (
                          <Tag
                            key={t.name}
                            color="green"
                            closable
                            onClose={(e) => {
                              e.preventDefault();
                              api.deleteTag(project.id, t.name).then(onRefresh).catch((err) =>
                                message.error(err.message),
                              );
                            }}
                            icon={<TagOutlined />}
                          >
                            {t.label}
                          </Tag>
                        ))}
                        <Space size={4} style={{ marginTop: 2 }}>
                          <Button
                            size="small"
                            type="text"
                            onClick={() => setTagging({ commit: v.commit, label: '' })}
                          >
                            标记
                          </Button>
                          {!v.current && v.can_revert !== false && (
                            <Button
                              size="small"
                              type="text"
                              danger
                              loading={reverting === v.commit}
                              disabled={!!reverting}
                              onClick={() => revertTo(v)}
                            >
                              回退到此版本
                            </Button>
                          )}
                        </Space>
                      </>
                    )}
                  </div>
                ),
              }))}
            />
          )}
          {history.nextCursor && (
            <Button block size="small" loading={historyLoading} onClick={loadMoreHistory}>
              加载更多
            </Button>
          )}
          <Modal
            title="标记迭代"
            open={!!tagging}
            onCancel={() => setTagging(null)}
            onOk={saveTag}
            okText="标记"
          >
            <Input
              placeholder="如：校验通过的里程碑"
              value={tagging?.label}
              onChange={(e) => setTagging({ ...tagging, label: e.target.value })}
              onPressEnter={saveTag}
              autoFocus
            />
          </Modal>
        </>
      )}

      <EvalRunDetail
        open={evalDetailOpen}
        projectId={project.id}
        state={evalInfo?.state}
        evolution={evolution}
        onClose={() => setEvalDetailOpen(false)}
        onOpenReport={(path) => {
          setEvalDetailOpen(false);
          onSelectFile(path);
        }}
        onEvolution={() => {
          setEvalDetailOpen(false);
          runEvolution();
        }}
      />
      {packagePublish.dialog}
    </div>
  );
}
