// 发布页「需要处理」：没通过或有提醒的检查项各一张卡，标题说问题和后果，下面按对象列，底部是修复动作。
// 元素照 develop PublishCheckDialog（ADR-0098）：数据绑定修正（PB-02）、表的来源、无法核对的对象与其他提示、求解契约
// （静态检查、未验证确认）、结构自检与「交给助手修复」；包与流程带来的门禁给「去运行 / 去处理」。
// 数据绑定不挡发布：只剩它没过时，发布卡的「发布」直接按降级发布，这里只给修复入口。
// 文件路径与行号只用于「打开」和交给助手，不显示。
import { useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { uploadRules, validateResourceFile } from '../../lib/resourceUpload.js';
import { isLocked } from '../../state/readonly.js';
import { Button, Check, Chip, Icon, cx } from '../../ui/index.jsx';
import { ScopeLockBar } from '../plugin/parts.jsx';
import { usePopMenu } from '../versions/popMenu.js';
import {
  FINDING_TITLES, KIND_TEXT, STRUCTURE_TITLES, assistantPrompt, fileTarget, findingSource,
  gateMark, shortRevision, structurePrompt, structureRows, tableGroups,
} from './publishCheckModel.js';

const LOCK_TEXT = {
  running: ['助手正在改其中的对象，这一轮结束后才能改数据来源'],
  pending: ['其中的对象和助手这一轮的修改有冲突，选好保留哪边后才能改数据来源'],
};
const FOLD_AT = 5;

const openTarget = (pid, path) => {
  const target = fileTarget(path);
  if (target) navigate(pagePath(target.page, pid), target.query);
};

// 一张问题卡：标题一句话（问题与后果），正文按对象列，底部动作
function Section({ gate, title, children, foot }) {
  const mark = gateMark(gate);
  return (
    <section id={`pb-sec-${gate.id}`} className={cx('card pb-sec', mark.tone)}>
      <div className="pb-sec-h">
        <span className="pb-mark"><Icon name={mark.icon} size={14} /></span>
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="pb-sec-t">{gate.title}</div>
          <div className="sm pb-sec-d" title={title || gate.summary}>{title || gate.summary}</div>
        </div>
      </div>
      {children ? <div className="pb-sec-b">{children}</div> : null}
      {foot ? <div className="pb-sec-f row g8">{foot}</div> : null}
    </section>
  );
}

// 超过 5 条先收起
function Folded({ items, render }) {
  const [all, setAll] = useState(false);
  const shown = all ? items : items.slice(0, FOLD_AT);
  return (
    <>
      {shown.map(render)}
      {items.length > FOLD_AT ? (
        <div className="pb-more sm" role="button" onClick={() => setAll((v) => !v)}>
          {all ? '收起' : `展开其余 ${items.length - FOLD_AT} 条`}
        </div>
      ) : null}
    </>
  );
}

// 「改为现有数据表」：推荐 / 全部数据表 两组；菜单用 fixed 定位
function TableSelect({ issue, tables, disabled, onPick }) {
  const menu = usePopMenu({ width: 300, height: 320, align: 'left' });
  const groups = tableGroups(issue, tables);
  return (
    <div ref={menu.ref} style={{ minWidth: 170 }}>
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
  const path = `ontology/object/${issue.object.key}.md`;
  return (
    <div className="pb-row" title={locked ? '助手在改，暂不能修改' : undefined}>
      <div className="row g6">
        <b>{issue.object.name}</b>
        <Chip sm tone={issue.blocking ? 'red' : 'amber'}>{KIND_TEXT[issue.kind] || issue.kind}</Chip>
        <span className="sp" />
        {fileTarget(path) ? <span className="pb-link xs" role="button" onClick={() => openTarget(pid, path)}>打开</span> : null}
      </div>
      <div className="xs muted mt4">{issue.message}</div>
      <div className="row g6 mt8 wrap">
        {tables.length ? <TableSelect issue={issue} tables={tables} disabled={busy || locked} onPick={(t) => onBind(issue, t)} /> : null}
        <Button size="xs" disabled={busy || locked} onClick={() => onBind(issue, '')}>{busy ? '正在改…' : '标为不落库'}</Button>
        {issue.blocking ? <Button size="xs" kind="ghost" onClick={() => onAssistant([issue])}>交给助手修复</Button> : null}
      </div>
    </div>
  );
}

// 表从哪来：已上传但没标 schema 的表格资料可直接改标，或补传一份
function TableSources({ check, busy, onMark, onUpload }) {
  const { config } = useApp();
  const resources = check.binding?.table_resources || [];
  const noTables = !check.binding?.table_count;
  const pick = (e) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    const checked = validateResourceFile(file, uploadRules(config));
    if (!checked.valid) { toast(checked.message, 'alert'); return; }
    onUpload(file);
  };
  return (
    <div className="pb-row">
      <div className="sm">{noTables ? '项目里还没有可核对的数据表：把含表结构的表格标为「数据 Schema」或上传一份，会重新检查' : `已有 ${check.binding.table_count} 张数据表，引用的表在下面的表格里时标为「数据 Schema」`}</div>
      <div className="row g6 mt8 wrap">
        {resources.map((path) => (
          <Button key={path} size="xs" disabled={busy === path} onClick={() => onMark(path)}>把「{path.split('/').pop()}」标为数据 Schema</Button>
        ))}
        <label className={cx('btn xs', !resources.length && 'soft')} style={{ cursor: busy === 'upload' ? 'wait' : 'pointer' }}>
          <Icon name="upload" size={14} />上传 Schema 表格
          <input type="file" hidden accept=".xlsx,.xlsm,.csv" disabled={busy === 'upload'} onChange={pick} />
        </label>
      </div>
    </div>
  );
}

function BindingSection({ gate, check, onRecheck, onChanged, onAssistant }) {
  const { api } = useApp();
  const { pid, scope } = useProject();
  const [busy, setBusy] = useState('');
  const issues = check.binding?.issues || [];
  const blocking = issues.filter((issue) => issue.blocking);
  const pending = issues.filter((issue) => !issue.blocking);
  const warnings = check.binding?.warnings || [];
  const tables = check.binding?.tables || [];

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
  const row = (issue) => (
    <IssueRow key={issue.object.id} issue={issue} tables={tables} busy={busy === issue.object.key}
      locked={isLocked(scope, issue.object)} onBind={bind} onAssistant={onAssistant} />
  );
  const showSources = !check.binding?.table_count || (check.binding?.table_resources || []).length > 0;
  return (
    <Section
      gate={gate}
      foot={blocking.length ? <Button size="sm" kind="primary" onClick={() => onAssistant(blocking)}>全部交给助手修复</Button> : null}
    >
      {issues.some((x) => isLocked(scope, x.object)) ? <ScopeLockBar lock={scope} targets={issues.map((x) => x.object)} text={LOCK_TEXT} /> : null}
      {blocking.length ? <Folded items={blocking} render={row} /> : null}
      {showSources ? <TableSources check={check} busy={busy} onMark={markSchema} onUpload={uploadSchema} /> : null}
      {pending.length ? <Fold title={`无法核对、暂不绑定的对象（${pending.length}）`}>{pending.map(row)}</Fold> : null}
      {warnings.length ? <Fold title={`其他提示（${warnings.length}）`}>{warnings.map((w) => <div key={w} className="xs mt4">{w}</div>)}</Fold> : null}
      {check.binding?.truncated ? <div className="xs muted mt8">条目较多，只列出前一部分；修好后会重新检查。</div> : null}
    </Section>
  );
}

function Fold({ title, children }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="pb-fold">
      <div className="row g4 sm" role="button" onClick={() => setOpen((v) => !v)}>
        <Icon name={open ? 'chevd' : 'chev'} size={12} />{title}
      </div>
      {open ? <div className="mt4">{children}</div> : null}
    </div>
  );
}

// 求解知识包：静态检查问题须先修正（降级不能绕过）；端到端验证在发布后的独立评测里做，发布前确认尚未验证
function SolverSection({ gate, solver, acknowledged, onAcknowledge, onRecheck }) {
  const { pid } = useProject();
  const items = solver.findings || [];
  return (
    <Section
      gate={gate}
      title={items.length ? gate.summary : '求解能力还没经过端到端验证，发布后在评测里验证'}
      foot={(
        <>
          <Button size="sm" kind="ghost" onClick={() => navigate(pagePath('scripts', pid), { tab: 'sv' })}>打开求解模型</Button>
          <Button size="sm" kind="ghost" onClick={onRecheck}>刷新</Button>
          <span className="sp" />
          <span className="xs muted">修订 {shortRevision(solver.revision)}</span>
        </>
      )}
    >
      {items.length ? (
        <Folded
          items={items}
          render={(item, index) => (
            // eslint-disable-next-line react/no-array-index-key
            <div key={index} className="pb-row">
              <div className="row g6">
                {item.solver_key ? <b>{item.solver_key}</b> : null}
                <Chip sm tone="red">{FINDING_TITLES[item.code] || item.code}</Chip>
                <span className="sp" />
                {item.path && fileTarget(item.path) ? <span className="pb-link xs" role="button" onClick={() => openTarget(pid, item.path)}>打开</span> : null}
              </div>
              <div className="xs mt4">{item.message}</div>
              {item.hint ? <div className="xs muted mt4">{item.hint}</div> : null}
              <div className="xs faint mt4">{findingSource(item)}</div>
            </div>
          )}
        />
      ) : null}
      {solver.verified_release ? (
        <div className="pb-row sm">求解契约与已通过验证的 {solver.verified_release.version} 一致，这次发布不用确认。</div>
      ) : null}
      {solver.requires_validation_after_publish ? (
        <div className="pb-row">
          <div className="xs muted">
            静态检查已通过，包可以被导入；但 consumer 里能否实际跑通（Agent 调用求解工具、入参一致、得到权威结果）还没验证。
            发布后在「已发布版本」里对这个版本发起求解验证；没通过验证的版本仍可导入，但求解结果可能不可用。
          </div>
          <div className="mt8"><Check on={acknowledged} onChange={onAcknowledge} label="我已了解，发布后需完成求解验证" /></div>
        </div>
      ) : null}
    </Section>
  );
}

// 知识包结构：consumer 导入会拒绝的包不能发布，降级也不能绕过；按对象列出，路径只用来「打开」
function StructureSection({ gate, onAssistant }) {
  const { pid } = useProject();
  const findings = gate.findings || [];
  // 只有一条、且标题已经说了的不再重复列
  const rows = structureRows(findings).filter((r) => !(findings.length === 1 && r.message === gate.summary));
  return (
    <Section
      gate={gate}
      foot={findings.length ? <Button size="sm" kind="primary" onClick={() => onAssistant(structurePrompt(findings))}>交给助手修复</Button> : null}
    >
      {rows.length ? (
        <Folded
          items={rows}
          render={(r) => (
          <div key={r.key} className="pb-row row g8">
            {r.name ? <b className="pb-obj">{r.name}</b> : <Chip sm tone="red">{STRUCTURE_TITLES[findings[0]?.code] || '结构'}</Chip>}
            <span className="grow sm">{r.message}</span>
            {r.path && fileTarget(r.path) ? <span className="pb-link xs" role="button" onClick={() => openTarget(pid, r.path)}>打开</span> : null}
            </div>
          )}
        />
      ) : null}
    </Section>
  );
}

// 建模阶段没做完：去工作台接着做
function StagesSection({ gate }) {
  const { pid } = useProject();
  return <Section gate={gate} foot={<Button size="sm" kind="primary" onClick={() => navigate(pagePath('wb-l2', pid))}>去工作台</Button>} />;
}

// 包或流程带来的门禁：「去运行」开始那个节点，「去处理」跳到对应页面
function PackSection({ gate, onGateAction, busy }) {
  const { pid } = useProject();
  const findings = (gate.findings || []).filter((f) => f.message && f.message !== gate.summary);
  const label = gate.action?.type === 'start_node' ? '去运行' : '去处理';
  return (
    <Section gate={gate} foot={gate.action ? <Button size="sm" kind="primary" disabled={busy} onClick={() => onGateAction(gate)}>{label}</Button> : null}>
      {findings.length ? (
        <Folded
          items={findings}
          render={(f, i) => (
            // eslint-disable-next-line react/no-array-index-key
            <div key={i} className="pb-row row g8">
              <span className="grow sm">{f.message}</span>
              {f.path && fileTarget(f.path) ? <span className="pb-link xs" role="button" onClick={() => openTarget(pid, f.path)}>打开</span> : null}
            </div>
          )}
        />
      ) : null}
    </Section>
  );
}

export default function PublishProblems({
  check, gates, acknowledged, onAcknowledge, onRecheck, onChanged, onGateAction, busy,
}) {
  const { api } = useApp();
  const { pid } = useProject();
  // 交给助手：发到默认对话，去工作台看
  const askAssistant = async (issuesOrText) => {
    try {
      const text = typeof issuesOrText === 'string' ? issuesOrText : assistantPrompt(issuesOrText);
      await api.sendMessage(pid, '-', { text });
      navigate(pagePath('wb-l2', pid));
    } catch (error) {
      toast(errorText(error), 'alert');
    }
  };
  if (!gates.length) return null;
  return (
    <div className="pb-problems">
      {gates.map((g) => {
        if (g.id === 'modeling_stages') return <StagesSection key={g.id} gate={g} />;
        if (g.id === 'data_binding') {
          return (
            <BindingSection key={g.id} gate={g} check={check}
              onRecheck={onRecheck} onChanged={onChanged} onAssistant={askAssistant} />
          );
        }
        if (g.id === 'solver_contract') {
          return <SolverSection key={g.id} gate={g} solver={check.solver || {}} acknowledged={acknowledged} onAcknowledge={onAcknowledge} onRecheck={onRecheck} />;
        }
        if (g.id === 'package_structure') return <StructureSection key={g.id} gate={g} onAssistant={askAssistant} />;
        return <PackSection key={g.id} gate={g} onGateAction={onGateAction} busy={busy} />;
      })}
    </div>
  );
}
