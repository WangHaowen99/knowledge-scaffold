// 发布检查（develop PublishCheckDialog 的元素与文字，ADR-0098）：摘要与上次发布、求解契约（静态检查、未验证确认）、
// 结构自检、需要修复的数据绑定、表的来源、无法核对的对象与其他提示。设计稿第五版的发布页是整页，这里放在门禁链下面的卡片里，
// 「发布」按钮留在右侧发布卡；降级与「全部交给助手修复」在卡片底部。
import { useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { checkResourceFile, uploadRules } from '../../lib/resourceUpload.js';
import { isLocked } from '../../state/readonly.js';
import { Button, Card, Check, Chip, Icon, Modal, Note, cx } from '../../ui/index.jsx';
import { ScopeLockBar } from '../plugin/parts.jsx';
import { usePopMenu } from '../versions/popMenu.js';
import {
  DEGRADE_NOTE, FINDING_TITLES, KIND_TEXT, STRUCTURE_TITLES, assistantPrompt, checkSummary, checkTitle, degradeState, fileTarget,
  findingSource, lastPublishText, shortRevision, tableGroups,
} from './publishCheckModel.js';

const LOCK_TEXT = {
  running: ['助手正在改其中的对象，这一轮结束后才能改数据来源'],
  pending: ['其中的对象和助手这一轮的修改有冲突，选好保留哪边后才能改数据来源'],
};

const openTarget = (pid, path) => {
  const target = fileTarget(path);
  if (target) navigate(pagePath(target.page, pid), target.query);
};

// 「改为现有数据表」：推荐 / 全部数据表 两组；表格外层会裁切，菜单用 fixed 定位
function TableSelect({ issue, tables, disabled, onPick }) {
  const menu = usePopMenu({ width: 300, height: 320, align: 'left' });
  const groups = tableGroups(issue, tables);
  return (
    <div ref={menu.ref} style={{ minWidth: 180 }}>
      <div className="inp sel" role="button" style={{ cursor: disabled ? 'not-allowed' : 'pointer', opacity: disabled ? 0.6 : 1 }}
        onClick={(e) => !disabled && menu.toggle(e)}>
        <span className="grow ell muted">改为现有数据表</span>
      </div>
      {menu.at ? (
        <div className="menu pb-menu" style={menu.at}>
          {groups.map((g) => (
            <div key={g.label}>
              <div className="xs muted" style={{ padding: '6px 10px 2px' }}>{g.label}</div>
              {g.options.map((o) => (
                <div key={o.value} className="mi" onClick={() => { menu.close(); onPick(o.value); }}>
                  <Icon name="table" size={14} /><span className="grow ell mono">{o.label}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function IssueRow({ issue, tables, busy, locked, onBind, onAssistant }) {
  const { pid } = useProject();
  const target = fileTarget(`ontology/object/${issue.object.key}.md`);
  return (
    <div className="pcd-issue" title={locked ? '助手在改，暂不能修改' : undefined}>
      <div className="row g6">
        <b>{issue.object.name}</b>
        <Chip sm tone={issue.blocking ? 'red' : 'amber'}>{KIND_TEXT[issue.kind] || issue.kind}</Chip>
      </div>
      <div className="xs muted mt4">{issue.message}</div>
      <div className="row g6 mt8 wrap">
        {tables.length ? <TableSelect issue={issue} tables={tables} disabled={busy || locked} onPick={(t) => onBind(issue, t)} /> : null}
        <Button size="xs" disabled={busy || locked} onClick={() => onBind(issue, '')}>{busy ? '正在改…' : '标为不落库'}</Button>
        {target ? <Button size="xs" kind="ghost" onClick={() => openTarget(pid, `ontology/object/${issue.object.key}.md`)}>打开文档</Button> : null}
        {issue.blocking ? <Button size="xs" kind="ghost" onClick={() => onAssistant([issue])}>交给助手修复</Button> : null}
      </div>
    </div>
  );
}

// 表从哪来：已上传但未标 schema 的表格资料可直接改标，或补传一份
function TableSources({ check, busy, onMark, onUpload }) {
  const { config } = useApp();
  const resources = check.binding?.table_resources || [];
  const noTables = !check.binding?.table_count;
  const pick = (e) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    const error = checkResourceFile(file.name, file.size, uploadRules(config));
    if (error) { toast(error, 'alert'); return; }
    onUpload(file);
  };
  return (
    <Note tone={noTables ? 'amber' : undefined} className="mt12">
      <div className="b">{noTables ? '项目里还没有可核对的数据表' : `现有 ${check.binding.table_count} 张数据表（来自 schema 表格资料）`}</div>
      <div className="xs mt4">
        {noTables
          ? '声明了来源表的对象暂不绑定（不阻断）。把含表结构的表格标为「数据 Schema」或补传一份后会重新检查；声明的表若是编造的，改为「无」。'
          : '对象引用的表若在下列未标注的表格资料里，标为「数据 Schema」后会重新检查。'}
      </div>
      {resources.map((path) => (
        <div key={path} className="row g6 mt4">
          <span className="grow ell sm">{path.split('/').pop()}</span>
          <Button size="xs" kind="ghost" disabled={busy === path} onClick={() => onMark(path)}>标为数据 Schema</Button>
        </div>
      ))}
      <label className="btn sm mt8" style={{ display: 'inline-flex', cursor: busy === 'upload' ? 'wait' : 'pointer' }}>
        <Icon name="upload" size={14} />上传 Schema 表格
        <input type="file" hidden accept=".xlsx,.xlsm,.csv" disabled={busy === 'upload'} onChange={pick} />
      </label>
    </Note>
  );
}

// 求解知识包：静态检查问题须先修正（降级不能绕过）；端到端验证在发布后的独立评测中进行，发布前须确认求解能力尚未验证
function SolverSection({ solver, acknowledged, onAcknowledge, onRefresh }) {
  const { pid } = useProject();
  const items = solver.findings || [];
  return (
    <div className="pcd-section">
      <div className="row g6">
        <span className="b">求解契约</span>
        <Chip sm tone={items.length ? 'red' : 'teal'}>{items.length ? '需修正' : '静态检查通过'}</Chip>
        <Button size="xs" kind="ghost" onClick={() => navigate(pagePath('scripts', pid), { tab: 'sv' })}>打开求解模型</Button>
        <Button size="xs" kind="ghost" onClick={onRefresh}>刷新</Button>
      </div>
      <div className="xs muted mt4">当前修订 {shortRevision(solver.revision)}</div>
      {items.map((item, index) => (
        // eslint-disable-next-line react/no-array-index-key
        <div key={index} className="pcd-issue">
          <div className="row g6">
            {item.solver_key ? <b>{item.solver_key}</b> : null}
            <Chip sm tone="red">{FINDING_TITLES[item.code] || item.code}</Chip>
          </div>
          <div className="xs mt4">{item.message}</div>
          {item.hint ? <div className="xs muted mt4">{item.hint}</div> : null}
          <div className="xs muted mt4">{findingSource(item)}</div>
          {item.path && fileTarget(item.path) ? <Button size="xs" kind="ghost" className="mt4" onClick={() => openTarget(pid, item.path)}>打开文件</Button> : null}
        </div>
      ))}
      {solver.truncated ? <div className="xs muted mt4">问题较多，只列出前一部分。</div> : null}
      {solver.verified_release ? (
        <Note tone="teal" icon="check" className="mt8">
          <div className="b">求解契约与已通过求解验证的 {solver.verified_release.version} 一致</div>
          <div className="xs mt4">当前修订已验证通过，本次发布无需确认；发布后也可在评测页面对新版本单独验证。</div>
        </Note>
      ) : null}
      {solver.requires_validation_after_publish ? (
        <Note tone="amber" className="mt8">
          <div className="b">求解能力尚未经端到端验证</div>
          <div className="xs mt4">
            本次发布已通过静态检查，知识包结构完整、可被导入；但求解能力能否在 consumer 中实际跑通
            （Agent 调用求解工具、入参一致、得到权威结果）尚未验证。
          </div>
          <div className="xs mt4">发布后请在「已发布版本（知识包评测）」中对该版本发起求解验证。未通过验证的版本仍可被导入，但求解结果可能不可用。</div>
          <div className="mt8"><Check on={acknowledged} onChange={onAcknowledge} label="我已了解，发布后需完成求解验证" /></div>
        </Note>
      ) : null}
    </div>
  );
}

// 结构自检：consumer 导入时会拒绝的包不能发布，降级也不能绕过
function StructureSection({ structure }) {
  return (
    <div className="pcd-section">
      <div className="b">知识包结构：导出后按 consumer 导入规则检查未通过</div>
      {structure.findings.map((item, index) => (
        // eslint-disable-next-line react/no-array-index-key
        <div key={index} className="pcd-issue">
          <Chip sm tone="red">{STRUCTURE_TITLES[item.code] || item.code}</Chip>
          <div className="xs mt4">{item.message}</div>
        </div>
      ))}
      {structure.truncated ? <div className="xs muted mt4">问题较多，只列出前一部分。</div> : null}
      <div className="xs muted mt4">修正知识包内容后重新检查；consumer 不接受的包即使发布也无法导入。</div>
    </div>
  );
}

function Fold({ title, children }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="pcd-fold mt8">
      <div className="row g4 sm" role="button" style={{ cursor: 'pointer' }} onClick={() => setOpen((v) => !v)}>
        <Icon name={open ? 'chevd' : 'chev'} size={12} />{title}
      </div>
      {open ? <div className="mt4">{children}</div> : null}
    </div>
  );
}

export default function PublishCheck({ check, checking, acknowledged, onAcknowledge, publishing, onRecheck, onDegrade, onChanged }) {
  const { api } = useApp();
  const { pid, scope } = useProject();
  const [busy, setBusy] = useState('');
  const [confirmDegrade, setConfirmDegrade] = useState(false);
  const issues = check.binding?.issues || [];
  const blockingIssues = issues.filter((issue) => issue.blocking);
  const pendingIssues = issues.filter((issue) => !issue.blocking);
  const warnings = check.binding?.warnings || [];
  const solver = check.solver?.enabled ? check.solver : null;
  const structure = check.structure?.blocking ? check.structure : null;
  const degrade = degradeState(check, acknowledged);
  const last = lastPublishText(check);

  const act = async (key, action, done) => {
    setBusy(key);
    try {
      await action();
      toast(done, 'check');
      onChanged?.();
    } catch (error) {
      toast(error.status === 409 ? '文档刚被修改过，已重新检查，请再试一次' : errorText(error), 'alert');
    } finally {
      setBusy('');
      onRecheck();
    }
  };
  const bind = (issue, table) => act(
    issue.object.key,
    () => api.setObjectDataSource(pid, issue.object.key, { kind: table ? 'table' : 'none', ...(table ? { value: table } : {}), base_revision: issue.revision }),
    table ? `「${issue.object.name}」已改为数据表 ${table}` : `「${issue.object.name}」已标为不落库`,
  );
  const markSchema = (path) => act(path, () => api.setResourcePurpose(pid, { path, purpose: 'schema' }), `${path.split('/').pop()} 已标为数据 Schema`);
  const uploadSchema = (file) => {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('purpose', 'schema');
    fd.append('subdir', file.name.toLowerCase().endsWith('.csv') ? 'others' : 'excel');
    act('upload', () => api.addResources(pid, fd), `已上传 ${file.name} 并标为数据 Schema`);
  };
  // 交给助手：发到当前对话，去工作台看
  const askAssistant = async (list) => {
    try {
      await api.sendMessage(pid, '-', { text: assistantPrompt(list) });
      navigate(pagePath('wb-l2', pid));
    } catch (error) {
      toast(errorText(error), 'alert');
    }
  };
  const rows = (list) => list.map((issue) => (
    <IssueRow key={issue.object.id} issue={issue} tables={check.binding?.tables || []} busy={busy === issue.object.key}
      locked={isLocked(scope, issue.object)} onBind={bind} onAssistant={askAssistant} />
  ));

  return (
    <Card title={checkTitle(check)} icon="shield" className="pcd">
      {issues.some((x) => isLocked(scope, x.object)) ? <ScopeLockBar lock={scope} targets={issues.map((x) => x.object)} text={LOCK_TEXT} /> : null}
      <div className="pcd-summary row wrap g8 sm">
        <span>{checkSummary(check)}</span>
        {last ? <span className="xs muted">{last}</span> : null}
      </div>
      {check.binding?.truncated ? <Note className="mt8">条目较多，只列出前一部分；修复后会重新检查。</Note> : null}
      {solver ? <SolverSection solver={solver} acknowledged={acknowledged} onAcknowledge={onAcknowledge} onRefresh={onRecheck} /> : null}
      {structure ? <StructureSection structure={structure} /> : null}
      {blockingIssues.length ? (
        <div className="pcd-section">
          <div className="b">需要修复：声明的来源表与现有数据表对不上</div>
          {rows(blockingIssues)}
        </div>
      ) : null}
      <TableSources check={check} busy={busy} onMark={markSchema} onUpload={uploadSchema} />
      {pendingIssues.length ? <Fold title={`无法核对、暂不绑定的对象（${pendingIssues.length}）`}>{rows(pendingIssues)}</Fold> : null}
      {warnings.length ? <Fold title={`其他提示（${warnings.length}）`}>{warnings.map((w) => <div key={w} className="xs mt4">{w}</div>)}</Fold> : null}
      <div className="row mt12 g8">
        {blockingIssues.length ? <Button size="sm" onClick={() => askAssistant(blockingIssues)}>全部交给助手修复</Button> : null}
        {blockingIssues.length ? (
          <Button size="sm" kind="danger" disabled={degrade.disabled || publishing} title={degrade.title || undefined}
            onClick={() => setConfirmDegrade(true)}>忽略并降级发布</Button>
        ) : null}
        <span className="sp" />
        <Button size="sm" kind="soft" icon="refresh" disabled={checking} onClick={onRecheck}>重新检查</Button>
      </div>
      <Modal open={confirmDegrade} onClose={() => setConfirmDegrade(false)} title={`${blockingIssues.length} 个对象未通过检查`} w={460}
        foot={<><span className="sp" /><Button onClick={() => setConfirmDegrade(false)}>取消</Button>
          <Button kind="danger" disabled={publishing} onClick={() => { setConfirmDegrade(false); onDegrade(); }}>忽略并降级发布</Button></>}>
        <div className={cx('sm')}>{DEGRADE_NOTE}</div>
      </Modal>
    </Card>
  );
}
