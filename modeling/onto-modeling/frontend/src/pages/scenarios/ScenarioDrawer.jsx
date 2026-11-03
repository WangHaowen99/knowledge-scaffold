// 场景详情抽屉（AR-03 详情、AR-04 版本记录、AR-05 改标题 / 输入 / 预期结论、AR-07 删除）。
// 写入范围内的场景只读：编辑、删除停用；交互走查照样只发命令。设计稿没画详情，按设计系统的抽屉补。
import { useEffect, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import {
  Area, Button, Chip, Drawer, Ev, Field, Icon, Inp, KV, Modal, Note, St,
} from '../../ui/index.jsx';
import { ScopeLockBar, SendButton } from '../plugin/parts.jsx';
import {
  INPUT_KIND, SCENARIO, enumLabel, failHint, lastRun, rowLocked, rowLockPop, rowState, walkCommand,
} from './scenariosModel.js';

const EDITABLE = ['输入', '预期结论'];
const SOURCE_LABEL = { assistant: '助手', web: '网页', studio: 'Studio', mcp: 'MCP', autobuild: '自主构建', import: '导入', plugin: '插件', revert: '回退' };

export function ScenarioDrawer({ row, type, lock, tasks, onClose, onChanged }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [detail, setDetail] = useState(null);
  const [history, setHistory] = useState([]);
  const [form, setForm] = useState(null);
  const [confirmDel, setConfirmDel] = useState(false);
  const [busy, setBusy] = useState(false);
  const key = row?.key;
  const rev = row?.revision;

  useEffect(() => {
    if (!key) return undefined;
    let alive = true;
    setForm(null);
    api.artifact(pid, SCENARIO, key).then((d) => { if (alive) setDetail(d); }).catch((e) => toast(errorText(e), 'alert'));
    api.artifactHistory(pid, SCENARIO, key).then((r) => { if (alive) setHistory(r.items || []); }).catch(() => {});
    return () => { alive = false; };
  }, [api, pid, key, rev]);

  if (!row) return null;
  const d = detail?.key === key ? detail : null;
  const locked = rowLocked(lock, row, type);
  const f = row.fields || {};
  const [inputLabel, inputTone] = INPUT_KIND[f.input_kind] || [f.input_kind, 'line'];
  const st = rowState(row);
  const walk = walkCommand(row);

  const save = async () => {
    if (!form.title.trim()) { toast('场景名称不能为空', 'alert'); return; }
    setBusy(true);
    try {
      const r = await api.saveArtifact(pid, SCENARIO, key, {
        base_revision: d.revision, fields: { title: form.title.trim() }, sections: Object.fromEntries(EDITABLE.map((s) => [s, form[s]])), summary: `修改场景「${form.title.trim()}」`,
      });
      toast(`已保存为版本 ${r.version_label}`, 'check');
      setForm(null);
      setDetail(r.artifact);
      onChanged?.();
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    setBusy(true);
    try {
      const r = await api.deleteArtifact(pid, SCENARIO, key, d?.revision);
      toast(`已删除，保存为版本 ${r.version_label}`, 'check');
      setConfirmDel(false);
      onClose();
      onChanged?.();
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };

  const pop = locked ? rowLockPop(lock) : undefined;
  const foot = form ? (
    <>
      <Button onClick={() => setForm(null)}>取消</Button>
      <Button kind="primary" disabled={busy} onClick={save}>保存</Button>
    </>
  ) : (
    <>
      <span data-pop={pop}><Button kind="danger" icon="trash" disabled={locked || !d} onClick={() => setConfirmDel(true)}>删除</Button></span>
      <span className="sp" />
      <SendButton tasks={tasks} command={walk.command} args={walk.args} text={walk.text} icon="hand">交互走查</SendButton>
      <span data-pop={pop}>
        <Button
          kind="primary"
          icon="edit"
          disabled={locked || !d}
          onClick={() => setForm({ title: d.title, ...Object.fromEntries(EDITABLE.map((s) => [s, d.sections?.[s] || ''])) })}
        >
          编辑
        </Button>
      </span>
    </>
  );

  return (
    <>
      <Drawer open onClose={onClose} icon="flask" title={row.title} w={520} foot={foot}>
        {locked ? (
          <ScopeLockBar
            lock={lock}
            text={{
              running: ['助手正在改这个场景，这一轮结束前只读'],
              pending: ['这个场景和助手这一轮的修改有冲突：选好保留哪边前只读'],
            }}
          />
        ) : null}
        <KV
          w="88px"
          pairs={[
            ['状态', <span key="s" data-pop={st === 'fail' ? failHint(row) : undefined}><St k={st} sm /></span>],
            ['切片', enumLabel(type, 'slice', f.slice)],
            ['推演的操作', f.operation ? <span key="o" className="wl" onClick={() => navigate(pagePath('studio-table', pid), { entry: f.operation.id })}>{f.operation.name}</span> : '—'],
            ['来源', f.source ? <Ev key="e" anchor={f.source} label={f.source_label} /> : '—'],
            ['输入', <Chip key="i" tone={inputTone} sm>{inputLabel}</Chip>],
            ['上次推演', lastRun(f.last_run_at)],
          ]}
        />
        {st === 'fail' ? <Note tone="red" className="mt12">{failHint(row)}</Note> : null}
        {st === 'stale' && d?.stale_reason ? <Note tone="amber" icon="refresh" className="mt12">{d.stale_reason}</Note> : null}
        {form ? (
          <div className="col mt16" style={{ gap: 12 }}>
            <Field label="场景名称" req><Inp value={form.title} maxLength={80} onChange={(e) => setForm((x) => ({ ...x, title: e.target.value }))} /></Field>
            {EDITABLE.map((s) => (
              <Field key={s} label={s}>
                <Area value={form[s]} onChange={(e) => setForm((x) => ({ ...x, [s]: e.target.value }))} />
              </Field>
            ))}
          </div>
        ) : (
          Object.entries(d?.sections || {}).map(([title, text]) => (
            <div key={title}>
              <div className="sec-t mt16">{title}</div>
              <div className="sm sc-sec">{text || '—'}</div>
            </div>
          ))
        )}
        {!form ? (
          <>
            <div className="sec-t mt20"><Icon name="history" size={12} />版本记录</div>
            <div className="col" style={{ gap: 6 }}>
              {history.map((h) => (
                <div key={h.commit} className="row sm sc-hist">
                  <span className="mono b">{h.version_label}</span>
                  <span className="grow ell">{h.summary}</span>
                  <span className="xs muted">{SOURCE_LABEL[h.source] || h.source} · {lastRun(h.at)}</span>
                </div>
              ))}
              {!history.length ? <div className="xs muted">还没有版本记录</div> : null}
            </div>
          </>
        ) : null}
      </Drawer>
      <Modal
        open={confirmDel}
        danger
        onClose={() => setConfirmDel(false)}
        icon="trash"
        title="删除这个场景？"
        sub={row.title}
        foot={<><Button onClick={() => setConfirmDel(false)}>取消</Button><Button kind="danger fill" disabled={busy} onClick={remove}>删除</Button></>}
      >
        <div className="sm">可在版本历史里找回</div>
      </Modal>
    </>
  );
}
