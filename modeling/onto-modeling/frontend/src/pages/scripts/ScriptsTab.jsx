// 「脚本」页签：沙箱状态、可代码化候选（生成脚本）、已生成的脚本（契约检查 / 描述符 / 沙箱验证）、
// 脚本只读预览（ST-04 取路径、F-02 读原文，「去 Studio 编辑」）、沙箱验证输出（SC-02）
import { useEffect, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { errorText } from '../../api/errors.js';
import { sendHint } from '../../state/commandTask.js';
import { Button, Card, Chip, Empty, Icon, Menu, cx, useOutside } from '../../ui/index.jsx';
import { SendButton } from '../plugin/parts.jsx';
import {
  candidateView, contractChip, defaultPicked, defaultScript, descriptorChip, generateCommand, moreItems, pickedOperations,
  rowAction, sandboxChip, sandboxView, togglePicked, verificationView,
} from './scriptsModel.js';

const StateChip = ({ c }) => <Chip tone={c.tone} sm icon={c.icon || undefined}>{c.text}</Chip>;

// 「⋯」：重新生成脚本、去 Studio 编辑
function MoreMenu({ row, tasks, onStudio }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useOutside(ref, () => setOpen(false), open);
  const items = moreItems(row).map((x) => ({
    label: x.label,
    icon: x.icon,
    title: x.text ? sendHint(x.text) : undefined,
    onClick: () => (x.kind === 'studio' ? onStudio() : tasks.send({ command: x.command, args: x.args, text: x.text })),
  }));
  return (
    <span ref={ref} className="scr-more" onClick={(e) => e.stopPropagation()}>
      <Button size="xs" kind="ghost" icon="more" aria-label="更多" onClick={() => setOpen((v) => !v)} />
      {open ? <Menu items={items} className="scr-more-menu" onPick={() => setOpen(false)} /> : null}
    </span>
  );
}

function Candidates({ data, tasks }) {
  const cv = candidateView(data?.candidates);
  const scannedAt = data?.candidates?.scanned_at || '';
  const [picked, setPicked] = useState(() => defaultPicked(data?.candidates));
  // 重新扫描后按新的默认勾选
  const seen = useRef(scannedAt);
  useEffect(() => {
    if (seen.current !== scannedAt) {
      seen.current = scannedAt;
      setPicked(defaultPicked(data?.candidates));
    }
  }, [scannedAt, data]);
  const ops = pickedOperations(data?.candidates, picked);
  const cmd = generateCommand(ops);
  let body;
  if (!data) body = <div className="sm muted">正在读取…</div>;
  else if (!cv.scanned) {
    body = <div className="sm muted">还没扫描过。点右上角「扫描可代码化的操作」，助手在对话里逐个判断哪些操作适合写成脚本，结果回到这里。</div>;
  } else {
    body = (
      <>
        {cv.items.length ? cv.items.map((c) => {
          const on = picked.includes(c.operation.key);
          return (
            <div key={c.operation.key} className={cx('opt', on && 'on')} onClick={() => setPicked((p) => togglePicked(p, c.operation.key))}>
              <span className={cx('chk', on && 'on')} role="checkbox" aria-checked={on} />
              <div className="grow">
                <div className="t">{c.operation.name}</div>
                <div className="c">{c.reason}</div>
              </div>
            </div>
          );
        }) : <div className="sm muted">这次扫描没找到适合写成脚本的操作。</div>}
        {cv.unsuitableText ? <div className="row xs muted g4"><Icon name="info" size={12} />{cv.unsuitableText}</div> : null}
      </>
    );
  }
  return (
    <Card title={cv.title} icon="scan" sub={data ? cv.sub : ''}>
      <div className="col" style={{ gap: 8 }}>
        {body}
        <SendButton tasks={tasks} {...cmd} kind="primary" icon="sparkle" disabled={!ops.length}>
          生成脚本（{ops.length} 个）
        </SendButton>
      </div>
    </Card>
  );
}

function ScriptTable({ items, selected, onSelect, tasks, onStudio }) {
  return (
    <>
      <div className="tbl-wrap">
        <table className="tbl compact scr-tbl">
          <thead>
            <tr><th>脚本</th><th>对应操作</th><th>契约检查</th><th>描述符</th><th>沙箱验证</th><th aria-label="操作" /></tr>
          </thead>
          <tbody>
            {items.map((row) => {
              const cc = contractChip(row);
              const act = rowAction(row);
              return (
                <tr key={row.script.key} className={cx(row.script.key === selected && 'on')} onClick={() => onSelect(row.script.key)} title="点一下在下面预览">
                  <td><span className="mono">{row.script.name}</span></td>
                  <td>{row.operation?.name || '—'}</td>
                  <td>{cc.pop ? <span data-pop={cc.pop}><StateChip c={cc} /></span> : <StateChip c={cc} />}</td>
                  <td><StateChip c={descriptorChip(row)} /></td>
                  <td><StateChip c={sandboxChip(row)} /></td>
                  <td onClick={(e) => e.stopPropagation()}>
                    {act.kind === 'more'
                      ? <MoreMenu row={row} tasks={tasks} onStudio={() => onStudio(row)} />
                      : <SendButton tasks={tasks} command={act.command} args={act.args} text={act.text} size="xs" kind="soft">{act.label}</SendButton>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="row mt12 xs muted g4"><Icon name="info" size={12} />契约检查：头部注释和参数一一对应、入参形式和消费端一致；描述符供消费端调用脚本时使用。三项都通过才能保存。</div>
    </>
  );
}

// 脚本只读预览：ST-04 取条目路径，F-02 读原文
function Preview({ row, tick, onStudio }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [state, setState] = useState(null);
  const id = row?.script?.id;
  useEffect(() => {
    if (!id) return undefined;
    let alive = true;
    setState(null);
    (async () => {
      try {
        const entry = await api.studioEntry(pid, id);
        const file = await api.fileContent(pid, entry.path);
        if (alive) setState({ content: file.content, truncated: file.truncated });
      } catch (e) {
        if (alive) setState({ error: errorText(e) });
      }
    })();
    return () => { alive = false; };
  }, [api, pid, id, tick]);
  if (!row) return null;
  let body;
  if (!state) body = <span className="muted">正在读取…</span>;
  else if (state.error) body = <span className="muted">读不到脚本原文：{state.error}</span>;
  else body = <>{state.content}{state.truncated ? '\n…（文件较大，只显示开头，完整内容去 Studio 看）' : ''}</>;
  return (
    <Card
      title={row.script.name}
      icon="code"
      sub={`${row.operation?.name ? `${row.operation.name} · ` : ''}只读预览，脚本内容在 Studio 编辑`}
      extra={<Button size="xs" kind="soft" icon="edit" onClick={() => onStudio(row)}>去 Studio 编辑</Button>}
    >
      <div className="code scr-src">{body}</div>
    </Card>
  );
}

// 沙箱验证输出（SC-02）
function Verification({ row, tick }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [v, setV] = useState(undefined);
  const key = row?.script?.key;
  useEffect(() => {
    if (!key) return undefined;
    let alive = true;
    setV(undefined);
    api.scriptVerification(pid, key).then((r) => alive && setV(r)).catch(() => alive && setV(null));
    return () => { alive = false; };
  }, [api, pid, key, tick]);
  if (!row) return null;
  const vv = verificationView(v, row.script.name);
  let body;
  if (v === undefined) body = <span className="scr-dim">正在读取…</span>;
  else if (vv.status === 'not_run') body = <span className="scr-dim">还没在沙箱里跑过：契约检查通过后，助手会用合成输入跑一遍。</span>;
  else {
    body = (
      <>
        {vv.lines.map((l, i) => <div key={i} className={cx('scr-ln', l.kind === 'err' && 'err')}>{l.text}</div>)}
        <div className={cx('scr-ln', vv.summary.ok ? 'ok' : 'bad')}>{vv.summary.text}</div>
      </>
    );
  }
  return (
    <Card title="沙箱验证输出" icon="terminal" sub={vv.sub} extra={v === undefined ? null : <StateChip c={vv.chip} />}>
      <div className="code scr-term">{body}</div>
    </Card>
  );
}

export default function ScriptsTab({ data, sandbox, tick, tasks }) {
  const { pid } = useProject();
  const items = data?.items || [];
  const [sel, setSel] = useState(null);
  const key = sel && items.some((r) => r.script.key === sel) ? sel : defaultScript(items);
  const row = items.find((r) => r.script.key === key) || null;
  const sb = sandboxView(sandbox);
  const toStudio = (r) => navigate(pagePath('studio-table', pid), { entry: r.script.id });

  return (
    <>
      {sb ? (
        <div className="row g12">
          <Chip tone={sb.tone} icon={sb.icon}>{sb.text}</Chip>
          <span className="row g4 sm muted"><Icon name="ban" size={14} />{sb.note}</span>
        </div>
      ) : null}
      <div className="grid2 mt16" style={{ gridTemplateColumns: '400px 1fr' }}>
        <Candidates data={data} tasks={tasks} />
        <Card title="已生成的脚本" icon="code">
          {!data ? <div className="sm muted">正在读取…</div> : items.length ? (
            <ScriptTable items={items} selected={key} onSelect={setSel} tasks={tasks} onStudio={toStudio} />
          ) : (
            <Empty icon="code" title="还没有脚本">勾选左边的操作点「生成脚本」，助手在对话里写脚本，并在沙箱里用合成输入跑通。</Empty>
          )}
        </Card>
      </div>
      {row ? (
        <div className="grid2 mt16" style={{ gridTemplateColumns: '1.15fr 1fr' }}>
          <Preview row={row} tick={tick} onStudio={toStudio} />
          <Verification row={row} tick={tick} />
        </div>
      ) : null}
    </>
  );
}
