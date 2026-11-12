import PublishedVersionsDialog from './PublishedVersionsDialog';
import { UploadOutlined } from '@ant-design/icons';
import {
  Alert,
  Button,
  Checkbox,
  Collapse,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
  Upload,
  message,
} from 'antd';
import { useCallback, useEffect, useState } from 'react';
import { api } from '../lib/api.js';
import { validateResourceFile } from '../lib/resourceUpload.js';
import { openEvaluation, prefetchEvaluationEntry } from '../lib/evaluation.js';
import { notifyPackagePublished } from '../lib/packageEvaluation.js';
import {
  STRUCTURE_TITLES,
  hardBlockers,
  needsPublishCheck,
  needsSolverAcknowledgement,
  reopensPublishCheck,
} from '../lib/packagePublish.js';
import {
  FINDING_TITLES,
  VALIDATIONS_FILE,
  findingSource,
  isManagedSolverPath,
  shortRevision,
} from '../lib/solver-contract-model.js';

// 发布 / 评测前的数据绑定检查（ADR-0007）：阻断项给修复入口，也允许降级继续
const KIND_TEXT = {
  declared_missing: '声明的表不存在',
  no_columns: '表里没有一列对得上',
  declared_unverifiable: '无数据表可核对',
};

const MODE_TEXT = {
  publish: {
    title: '发布知识包 · 数据绑定检查',
    proceed: '发布',
    degrade: '忽略并降级发布',
    degradeNote: '这些对象发布后不绑定数据表，发布记录会注明降级发布。',
  },
  eval: {
    title: '数据评测 · 数据绑定检查',
    proceed: '开始评测',
    degrade: '忽略并继续评测',
    degradeNote: '这些对象在本轮评测中不绑定数据表，涉及它们的问题无法查库。',
  },
};

const formatTime = (seconds) => new Date(seconds * 1000).toLocaleString();

// 不以空格结尾：ChatPane 收到后直接发送，而不是只填进输入框
function assistantPrompt(issues) {
  return [
    '发布前数据绑定检查发现以下对象的「# 数据来源」与项目数据表对不上，请逐个修正：',
    ...issues.map((issue) => `- ${issue.message}`),
    '修正口径：首行只写现有数据表名（schema 表格资料或评测数据源上传表里的 Sheet 名，逐字相同）；'
      + '确实不落库的对象填「无」；不要臆造表名。修完运行规则检查，确认没有「列名对账」错误。',
  ].join('\n');
}

function tableOptions(issue, tables) {
  const candidates = issue.candidates || [];
  const recommended = new Set(candidates.map((item) => item.table));
  const groups = [];
  if (candidates.length) {
    groups.push({
      label: '推荐',
      options: candidates.map((item) => ({
        value: item.table,
        label: item.matched ? `${item.table}（${item.matched} 列对得上）` : item.table,
      })),
    });
  }
  groups.push({
    label: '全部数据表',
    options: tables.filter((name) => !recommended.has(name)).map((name) => ({ value: name, label: name })),
  });
  return groups;
}

function IssueRow({ issue, tables, busy, onBind, onOpenFile, onAssistant }) {
  return (
    <div className="pcd-issue">
      <div className="pcd-issue-head">
        <Typography.Text strong>{issue.object}</Typography.Text>
        <Tag color={issue.blocking ? 'error' : 'warning'}>{KIND_TEXT[issue.kind] || issue.kind}</Tag>
      </div>
      <div className="pcd-issue-msg">{issue.message}</div>
      <Space wrap size={6}>
        {tables.length > 0 && (
          <Select
            size="small"
            showSearch
            value={null}
            placeholder="改为现有数据表"
            popupMatchSelectWidth={false}
            style={{ minWidth: 180 }}
            disabled={busy}
            options={tableOptions(issue, tables)}
            onChange={(table) => onBind(issue, table)}
          />
        )}
        <Button size="small" loading={busy} onClick={() => onBind(issue, '')}>
          标为不落库
        </Button>
        {onOpenFile && (
          <Button size="small" type="link" onClick={() => onOpenFile(issue.path)}>
            打开文档
          </Button>
        )}
        {onAssistant && issue.blocking && (
          <Button size="small" type="link" onClick={() => onAssistant([issue])}>
            交给助手修复
          </Button>
        )}
      </Space>
    </div>
  );
}

// 表从哪来：已上传但未标 schema 的表格资料可直接改标，或补传一份
function TableSources({ check, busy, onMark, onUpload }) {
  const resources = check.table_resources || [];
  const noTables = check.table_count === 0;
  return (
    <Alert
      className="pcd-sources"
      type={noTables ? 'warning' : 'info'}
      showIcon
      message={
        noTables
          ? '项目里还没有可核对的数据表'
          : `现有 ${check.table_count} 张数据表（来自 schema 表格资料与评测数据源上传表）`
      }
      description={
        <>
          <div>
            {noTables
              ? '声明了来源表的对象暂不绑定（不阻断）。把含表结构的表格标为「数据 Schema」或补传一份后会重新检查；声明的表若是编造的，改为「无」。'
              : '对象引用的表若在下列未标注的表格资料里，标为「数据 Schema」后会重新检查。'}
          </div>
          {resources.map((path) => (
            <div key={path} className="pcd-resource">
              <span className="pcd-resource-name">{path.split('/').pop()}</span>
              <Button size="small" type="link" loading={busy === path} onClick={() => onMark(path)}>
                标为数据 Schema
              </Button>
            </div>
          ))}
          <Upload accept=".xlsx,.xlsm,.csv" showUploadList={false} beforeUpload={onUpload}>
            <Button size="small" icon={<UploadOutlined />} loading={busy === 'upload'}>
              上传 Schema 表格
            </Button>
          </Upload>
        </>
      }
    />
  );
}

function CheckSummary({ check, mode }) {
  const last = mode === 'publish' ? check.last_publish : null;
  return (
    <div className="pcd-summary">
      <span>
        对象 {check.objects} 个，已绑定数据表 {check.bound_objects} 个；阻断项 {check.blocking} 个
      </span>
      {last && (
        <span className="pcd-last">
          上次发布：{formatTime(last.at)}
          {last.by ? ` · ${last.by}` : ''}
          {last.degraded ? ` · 降级发布（${last.unbound_count} 个对象未绑定）` : ''}
        </span>
      )}
    </div>
  );
}

// 求解知识包的发布检查：静态检查问题须先修正（降级不能绕过）；端到端验证改在发布后的独立评测中进行，
// 发布前须确认求解能力尚未验证（后端同样强制）
function SolverSection({ solver, acknowledged, onAcknowledge, onOpenSolver, onOpenFile, onRefresh }) {
  const items = solver.findings || [];
  return (
    <div className="pcd-section">
      <div className="pcd-section-title">
        <Space size={6}>
          <span>求解契约</span>
          <Tag color={items.length ? 'error' : 'success'}>{items.length ? '需修正' : '静态检查通过'}</Tag>
          {onOpenSolver && (
            <Button size="small" type="link" onClick={onOpenSolver}>
              打开求解模型
            </Button>
          )}
          <Button size="small" type="link" onClick={onRefresh}>
            刷新
          </Button>
        </Space>
      </div>
      <div className="pcd-issue-msg">当前修订 {shortRevision(solver.revision)}</div>
      {items.map((item, index) => (
        // eslint-disable-next-line react/no-array-index-key
        <div key={index} className="pcd-issue">
          <div className="pcd-issue-head">
            {item.solver_key && <Typography.Text strong>{item.solver_key}</Typography.Text>}
            <Tag color="error">{FINDING_TITLES[item.code] || item.code}</Tag>
          </div>
          <div className="pcd-issue-msg">{item.message}</div>
          {item.hint && <div className="pcd-issue-msg">{item.hint}</div>}
          <div className="pcd-last">{findingSource(item)}</div>
          {onOpenFile && item.path && item.path !== VALIDATIONS_FILE && !isManagedSolverPath(item.path) && (
            <Button size="small" type="link" onClick={() => onOpenFile(item.path)}>
              打开文件
            </Button>
          )}
        </div>
      ))}
      {solver.truncated && <div className="pcd-issue-msg">问题较多，只列出前一部分。</div>}
      {solver.verified_release && (
        <Alert
          type="success"
          showIcon
          message={`求解契约与已通过求解验证的 ${solver.verified_release.version} 一致`}
          description="当前修订已验证通过，本次发布无需确认；发布后也可在评测页面对新版本单独验证。"
        />
      )}
      {solver.requires_validation_after_publish && (
        <Alert
          type="warning"
          showIcon
          message="求解能力尚未经端到端验证"
          description={(
            <Space direction="vertical" size={4}>
              <span>
                本次发布已通过静态检查，知识包结构完整、可被导入；但求解能力能否在 consumer 中实际跑通
                （Agent 调用求解工具、入参一致、得到权威结果）尚未验证。
              </span>
              <span>
                发布后请在「已发布版本（知识包评测）」中对该版本发起求解验证。未通过验证的版本仍可被导入，但求解结果可能不可用。
              </span>
              <Checkbox checked={acknowledged} onChange={(event) => onAcknowledge(event.target.checked)}>
                我已了解，发布后需完成求解验证
              </Checkbox>
            </Space>
          )}
        />
      )}
    </div>
  );
}

// 结构自检：consumer 导入时会拒绝的包不能发布，降级也不能绕过（规则副本见后端 consumer_package_rules）
function StructureSection({ structure }) {
  return (
    <div className="pcd-section">
      <div className="pcd-section-title">知识包结构：导出后按 consumer 导入规则检查未通过</div>
      {structure.findings.map((item, index) => (
        // eslint-disable-next-line react/no-array-index-key
        <div key={index} className="pcd-issue">
          <div className="pcd-issue-head">
            <Tag color="error">{STRUCTURE_TITLES[item.code] || item.code}</Tag>
          </div>
          <div className="pcd-issue-msg">{item.message}</div>
        </div>
      ))}
      {structure.truncated && <div className="pcd-issue-msg">问题较多，只列出前一部分。</div>}
      <div className="pcd-last">修正知识包内容后重新检查；consumer 不接受的包即使发布也无法导入。</div>
    </div>
  );
}

function useBindingCheck(open, projectId) {
  const [check, setCheck] = useState(null);
  const load = useCallback(async () => {
    try {
      setCheck(await api.packageCheck(projectId));
    } catch (error) {
      message.error(error.message);
    }
  }, [projectId]);
  useEffect(() => {
    if (open) load();
    return () => setCheck(null);
  }, [open, load]);
  return [check, load];
}

export default function PublishCheckDialog({
  open,
  projectId,
  mode = 'publish',
  onClose,
  onProceed,
  onCommand,
  onOpenFile,
  onChanged,
  onOpenSolver,
}) {
  const [check, reload] = useBindingCheck(open, projectId);
  const [busy, setBusy] = useState('');
  const [acknowledged, setAcknowledged] = useState(false);
  const text = MODE_TEXT[mode];

  const act = async (key, action, done) => {
    setBusy(key);
    try {
      await action();
      message.success(done);
      onChanged?.();
    } catch (error) {
      message.error(error.status === 409 ? '文档刚被修改过，已重新检查，请再试一次' : error.message);
    } finally {
      setBusy('');
      reload();
    }
  };

  const bind = (issue, table) =>
    act(
      issue.object,
      () => api.setObjectDataSource(projectId, issue.object, table, issue.revision),
      table ? `「${issue.object}」已改为数据表 ${table}` : `「${issue.object}」已标为不落库`,
    );

  const markSchema = (path) =>
    act(
      path,
      async () => {
        const { directives = {} } = await api.resourceDirectives(projectId);
        await api.setResourceDirective(projectId, path, 'schema', directives[path]?.reading_mode || 'on_demand');
      },
      `${path.split('/').pop()} 已标为数据 Schema`,
    );

  const uploadSchema = (file) => {
    const validation = validateResourceFile(file);
    if (!validation.valid) {
      message.error(validation.message);
      return false;
    }
    const subdir = file.name.toLowerCase().endsWith('.csv') ? 'others' : 'excel';
    act('upload', () => api.uploadResource(projectId, subdir, file, undefined, 'schema'), `已上传 ${file.name} 并标为数据 Schema`);
    return false;
  };

  const askAssistant = (issues) => {
    onCommand(assistantPrompt(issues));
    onClose();
  };

  const openFile = (path) => {
    onOpenFile(path);
    onClose();
  };

  const openSolver = () => {
    onOpenSolver();
    onClose();
  };

  const proceed = async (allowUnbound) => {
    setBusy('proceed');
    try {
      await onProceed(allowUnbound, acknowledged);
    } catch (error) {
      message.error(error.message);
      reload();
    } finally {
      setBusy('');
    }
  };

  const solver = mode === 'publish' && check?.solver?.enabled ? check.solver : null;
  const solverBlocking = solver?.blocking || 0;
  const structure = mode === 'publish' && check?.structure?.blocking ? check.structure : null;
  const hardBlocking = mode === 'publish' ? hardBlockers(check) : solverBlocking;
  const awaitingAcknowledgement = mode === 'publish' && needsSolverAcknowledgement(check) && !acknowledged;
  useEffect(() => {
    if (!open) setAcknowledged(false);
  }, [open]);
  const blockingIssues = check ? check.issues.filter((issue) => issue.blocking) : [];
  const pendingIssues = check ? check.issues.filter((issue) => !issue.blocking) : [];
  const rowProps = {
    tables: check?.tables || [],
    onBind: bind,
    onOpenFile: onOpenFile && openFile,
    onAssistant: onCommand && askAssistant,
  };
  const renderRows = (issues) =>
    issues.map((issue) => (
      <IssueRow key={issue.object} issue={issue} busy={busy === issue.object} {...rowProps} />
    ));

  const collapsed = [
    pendingIssues.length > 0 && {
      key: 'pending',
      label: `无法核对、暂不绑定的对象（${pendingIssues.length}）`,
      children: renderRows(pendingIssues),
    },
    check?.warnings.length > 0 && {
      key: 'warnings',
      label: `其他提示（${check.warnings.length}）`,
      children: check.warnings.map((warning) => (
        <div key={warning} className="pcd-warning">{warning}</div>
      )),
    },
  ].filter(Boolean);

  const footer = [
    onCommand && blockingIssues.length > 0 && (
      <Button key="assistant" onClick={() => askAssistant(blockingIssues)}>
        全部交给助手修复
      </Button>
    ),
    blockingIssues.length > 0 && (
      <Popconfirm
        key="degrade"
        title={`${check.blocking} 个对象未通过检查`}
        description={text.degradeNote}
        disabled={hardBlocking > 0 || awaitingAcknowledgement}
        onConfirm={() => proceed(true)}
      >
        <Button
          danger
          disabled={hardBlocking > 0 || awaitingAcknowledgement}
          title={hardBlocking > 0 ? '降级只放宽数据绑定，求解契约与知识包结构问题须先修正' : undefined}
          loading={busy === 'proceed'}
        >
          {text.degrade}
        </Button>
      </Popconfirm>
    ),
    <Button
      key="proceed"
      type="primary"
      disabled={!check || check.blocking > 0 || hardBlocking > 0 || awaitingAcknowledgement}
      loading={busy === 'proceed'}
      onClick={() => proceed(false)}
    >
      {check?.blocking || hardBlocking
        ? `${text.proceed}（需先修复）`
        : awaitingAcknowledgement
          ? `${text.proceed}（需先确认）`
          : text.proceed}
    </Button>,
  ].filter(Boolean);

  return (
    <Modal
      title={solver ? '发布知识包 · 发布检查' : text.title}
      open={open}
      onCancel={onClose}
      width={760}
      zIndex={1100}
      footer={footer}
      destroyOnHidden
    >
      {!check ? (
        <div className="pcd-loading"><Spin /></div>
      ) : (
        <div className="pcd-body">
          <CheckSummary check={check} mode={mode} />
          {check.truncated && (
            <Alert type="info" showIcon message="条目较多，只列出前一部分；修复后会重新检查。" />
          )}
          {solver && (
            <SolverSection
              solver={solver}
              acknowledged={acknowledged}
              onAcknowledge={setAcknowledged}
              onOpenSolver={onOpenSolver && openSolver}
              onOpenFile={onOpenFile && openFile}
              onRefresh={reload}
            />
          )}
          {structure && <StructureSection structure={structure} />}
          {blockingIssues.length > 0 && (
            <div className="pcd-section">
              <div className="pcd-section-title">需要修复：声明的来源表与现有数据表对不上</div>
              {renderRows(blockingIssues)}
            </div>
          )}
          <TableSources check={check} busy={busy} onMark={markSchema} onUpload={uploadSchema} />
          {collapsed.length > 0 && <Collapse size="small" items={collapsed} />}
        </div>
      )}
    </Modal>
  );
}

// 发布入口共用：检查通过提交发布，有问题项再打开检查弹窗
export function usePackagePublish(dialogExtras = {}) {
  const [projectId, setProjectId] = useState('');
  const [publishing, setPublishing] = useState(false);
  const [versionsProject, setVersionsProject] = useState(null);

  const submitRelease = async (pid, allowUnbound, solverAcknowledged = false) => {
    const receipt = await api.publishPackage(pid, { allowUnbound, solverAcknowledged });
    notifyPackagePublished(pid);
    if (receipt.local_record_pending) message.warning('知识包已发布，本地发布记录待恢复');
    else if (solverAcknowledged) {
      // 求解知识包：发布后直接去评测页面对这个版本发起求解验证
      message.success({
        duration: 10,
        content: (
          <span>
            知识包 {receipt.version} 已发布，求解能力待验证
            <Button
              size="small"
              type="link"
              onClick={() => openEvaluation(receipt, { runType: 'solver' }).catch((e) => message.error(e.message))}
            >
              去评测
            </Button>
          </span>
        ),
      });
    } else message.success(allowUnbound ? '已降级发布，未通过检查的对象不绑定数据表' : '知识包已发布');
    // 评测优化之后发布：回写评测服务这是哪一轮优化建议的处理结果
    const writeback = receipt.evaluation_writeback;
    if (writeback?.status === 'done' && writeback.release_id === receipt.release_id) {
      message.success(`已回写评测服务：新版本 ${writeback.version} 处理了 ${writeback.base_version} 的优化建议`);
    } else if (writeback?.status === 'pending') {
      message.warning('新版本的处理结果暂未回写评测服务，打开已发布版本时会自动重试');
    }
    setProjectId('');
    setVersionsProject({ id: pid });
  };

  const publish = async (pid) => {
    setPublishing(true);
    // 发布成功提示里的「去评测」要在点击时立即打开窗口：先取好评测入口地址，失败时点击会再读取并给出原因
    prefetchEvaluationEntry().catch(() => {});
    try {
      const check = await api.packageCheck(pid);
      if (needsPublishCheck(check)) setProjectId(pid);
      else await submitRelease(pid, false);
    } catch (error) {
      if (reopensPublishCheck(error)) setProjectId(pid);
      else message.error(error.message);
    } finally {
      setPublishing(false);
    }
  };

  const dialog = (
    <>
    <PublishedVersionsDialog projectId={versionsProject?.id || ''} projectName={versionsProject?.name || ''}
      onClose={() => setVersionsProject(null)} onCommand={dialogExtras.onCommand} />
    <PublishCheckDialog
      {...dialogExtras}
      open={!!projectId}
      projectId={projectId}
      mode="publish"
      onClose={() => setProjectId('')}
      onProceed={(allowUnbound, solverAcknowledged) => submitRelease(projectId, allowUnbound, solverAcknowledged)}
    />
    </>
  );
  return { publish, publishing, dialog, showVersions: (project) => setVersionsProject(typeof project === 'string' ? { id: project } : project) };
}
