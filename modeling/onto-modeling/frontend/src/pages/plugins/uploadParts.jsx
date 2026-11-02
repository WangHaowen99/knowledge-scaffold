// 上传与审核的几张卡（设计稿 pages-60-plugins.js plugin-upload）：上传区与上传列表、静态检查、发布、审核记录
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { formatBytes } from '../../api/errors.js';
import { ROLE_LABEL } from '../../state/roles.js';
import {
  Area, Button, Card, Chip, Empty, Field, Icon, Inp, KV, Menu, Modal, Note, Prog, Seg, St, Toggle, cx, useOutside,
} from '../../ui/index.jsx';
import {
  CHANNELS, checkView, contributesText, defaultPublishForm, effectText, kindText, orderUploads, publishBlock, reasonProblem,
  rejectDraft, rolloutEstimate, rolloutProblem, segUploads, timelineRows, uploadMeta, uploadStatus, uploadTitle,
} from './uploadModel.js';

const SHOW_ROWS = 4;

// 审核记录里的称呼「管理员 张工」，申请里只写显示名。别人的显示名按 AD-01 查（只在有别人的记录时查一次）
export function useActorNames(actors) {
  const { api, user } = useApp();
  const [users, setUsers] = useState(null);
  const need = actors.some((a) => a && a !== 'system' && a !== user?.username);
  useEffect(() => {
    if (!need || users) return undefined;
    let alive = true;
    api.adminUsers({ limit: 200 })
      .then((r) => alive && setUsers(Object.fromEntries((r.items || []).map((u) => [u.username, u]))))
      .catch(() => alive && setUsers({}));
    return () => { alive = false; };
  }, [api, need, users]);
  return useCallback((name, withRole = true) => {
    const u = name === user?.username ? user : users?.[name];
    if (!u) return name;
    return withRole ? `${ROLE_LABEL[u.role] || ''} ${u.display_name || u.username}`.trim() : u.display_name || u.username;
  }, [user, users]);
}

// ---------- 上传区与上传列表（PL-13、PL-14） ----------

function UploadRow({ u, on, onSelect }) {
  const st = uploadStatus(u);
  return (
    <div className={cx('pl-file plu-row', on && 'on')} role="button" tabIndex={0} onClick={() => onSelect(u.id)}
      onKeyDown={(e) => { if (e.key === 'Enter') onSelect(u.id); }}>
      <Icon name="pkg" size={18} />
      <div className="grow"><div className="b sm ell">{u.filename}</div><div className="xs muted">{uploadMeta(u)}</div></div>
      {st.marks ? (
        <>
          <Chip tone={u.manifest_ok ? 'teal' : 'red'} icon={u.manifest_ok ? 'check' : 'x'} sm>manifest.yaml</Chip>
          <Chip tone={u.checksums_ok ? 'teal' : 'red'} icon={u.checksums_ok ? 'check' : 'x'} sm>checksums.sha256</Chip>
        </>
      ) : null}
      {st.running ? <St k="running" sm label={st.text} /> : <Chip tone={st.tone} icon={st.icon} sm>{st.text}</Chip>}
    </div>
  );
}

function SendingRow({ s, onDismiss }) {
  return (
    <div className="pl-file">
      <Icon name="pkg" size={18} />
      <div className="grow">
        <div className="b sm ell">{s.name}</div>
        <div className="xs muted">
          {formatBytes(s.size)} · {s.error ? <span style={{ color: 'var(--red)' }}>{s.error}</span> : `上传中 ${s.pct || 0}%`}
        </div>
      </div>
      {s.error ? (
        <>
          <Chip tone="red" icon="x" sm>没有上传</Chip>
          <button type="button" className="icon-btn" style={{ width: 26, height: 26 }} title="移除这一行" onClick={() => onDismiss(s.key)}><Icon name="x" size={14} /></button>
        </>
      ) : <Prog p={s.pct} thin w="120px" />}
    </div>
  );
}

export function UploadCard({ list, sending, selectedId, limit, onFiles, onSelect, onDismiss }) {
  const input = useRef(null);
  const [drag, setDrag] = useState(false);
  const [all, setAll] = useState(false);
  const rows = orderUploads(list);
  const shown = all ? rows : rows.slice(0, SHOW_ROWS);
  const pick = (files) => { if (files?.length) onFiles([...files]); };
  return (
    <div className="card pad">
      <div
        className={cx('pl-drop', drag && 'drag')}
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => { e.preventDefault(); setDrag(false); pick(e.dataTransfer?.files); }}
      >
        <span className="pl-ico cap" style={{ width: 46, height: 46 }}><Icon name="upload" size={20} /></span>
        <div className="grow">
          <div className="b">把插件包拖到这里</div>
          <div className="xs muted">支持 .zip：能力包、节点包、行业包、系统知识包、提供方、导出器 · ≤ {formatBytes(limit)}</div>
        </div>
        <Button icon="folder" onClick={() => input.current?.click()}>选择文件</Button>
        <input
          ref={input} type="file" accept=".zip,application/zip" multiple hidden data-testid="pack-file"
          onChange={(e) => { pick(e.target.files); e.target.value = ''; }}
        />
      </div>
      {sending.length || rows.length ? (
        <div className="col mt12" style={{ gap: 8 }}>
          {sending.map((s) => <SendingRow key={s.key} s={s} onDismiss={onDismiss} />)}
          {shown.map((u) => <UploadRow key={u.id} u={u} on={u.id === selectedId} onSelect={onSelect} />)}
          {rows.length > SHOW_ROWS ? (
            <a className="link plu-more" onClick={() => setAll((v) => !v)}>{all ? '收起' : `再显示 ${rows.length - SHOW_ROWS} 个已处理或更早的上传`}</a>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

// ---------- 静态检查（PL-15、PL-16、PL-18） ----------

function CheckRow({ c }) {
  const v = checkView(c);
  return (
    <div className="pl-chk">
      <span className={cx('d', v.cls)}><Icon name={v.icon} /></span>
      <div className="grow"><div className="sm b">{c.label}</div>{c.detail ? <div className="xs muted">{c.detail}</div> : null}</div>
      <Chip tone={v.tone} sm>{v.label}</Chip>
    </div>
  );
}

export function CheckCard({ list, selectedId, detail, onSelect, onDownload, onReject }) {
  const seg = segUploads(list, selectedId);
  const u = detail && detail.id === selectedId ? detail : null;
  let body;
  if (!list.length) {
    body = (
      <Empty icon="shield" title="还没有上传过插件包">
        上传后自动做静态检查：清单、校验和、写入范围、hooks、前端代码、凭据、提示词体积逐项列在这里。
      </Empty>
    );
  } else if (!u) {
    body = <div className="muted sm">正在读取检查结果…</div>;
  } else if (u.state === 'checking') {
    body = (
      <div className="pl-chk">
        <span className="d wait"><span className="spin" /></span>
        <div className="grow">
          <div className="sm b">静态检查进行中</div>
          <div className="xs muted">逐项检查清单、校验和、写入范围、hooks、前端代码、凭据、提示词体积，不执行包里的代码；跑完自动刷新。</div>
        </div>
        <St k="running" sm label="检查中" />
      </div>
    );
  } else {
    const failed = (u.summary?.fail || 0) > 0;
    body = (
      <>
        {failed ? (
          <Note tone={u.state === 'rejected' ? 'gray' : 'red'}>
            {u.state === 'rejected' ? '已退回给上传者，不会进入包仓库。' : '有失败项的包不能发布，只能退回给上传者。'}
          </Note>
        ) : u.manifest ? (
          <div className="row mb8">
            <KV w="70px" pairs={[['kind', kindText(u.manifest)], ['贡献', contributesText(u.manifest.contributes)], ['生效时机', effectText(u.manifest)]]} />
          </div>
        ) : null}
        <div className={failed ? 'mt8' : undefined}>{(u.checks || []).map((c) => <CheckRow key={c.id} c={c} />)}</div>
        {failed || u.state === 'rejected' ? (
          <div className="row mt12">
            <span className="sp" />
            <Button size="sm" icon="download" onClick={() => onDownload(u)}>下载检查报告</Button>
            {u.state === 'failed' ? <Button size="sm" kind="danger" onClick={() => onReject(u)}>退回</Button> : null}
          </div>
        ) : null}
      </>
    );
  }
  return (
    <div className="card">
      <div className="card-h">
        <h3>静态检查</h3>
        <span className="sp" />
        {seg.length > 1 ? <Seg items={seg} value={selectedId} onChange={onSelect} /> : u ? <span className="sub">{uploadTitle(u)}</span> : null}
      </div>
      <div className="card-b">{body}</div>
    </div>
  );
}

export function RejectModal({ upload, busy, onClose, onSubmit }) {
  const [reason, setReason] = useState(() => rejectDraft(upload));
  const problem = reasonProblem(reason);
  return (
    <Modal
      open icon="undo" danger w={520} title={`退回 ${uploadTitle(upload)}`} sub="退回的上传不进包仓库，也不留下版本" onClose={onClose}
      foot={(
        <>
          <Button onClick={onClose}>取消</Button>
          <Button kind="danger" icon="undo" disabled={!!problem || busy} title={problem || undefined} onClick={() => onSubmit(reason.trim())}>退回</Button>
        </>
      )}
    >
      <Field label="退回原因" req hint="写进审核记录；上传者改好后重新上传。">
        <Area value={reason} onChange={(e) => setReason(e.target.value)} minHeight={96} />
      </Field>
      <Note tone="gray" className="mt12">现在退回不会自动通知上传者，请另行告知。</Note>
    </Modal>
  );
}

// ---------- 发布（PL-17） ----------

function RolloutRange({ value, onChange }) {
  return (
    <div className="plu-range">
      <Prog p={value} w="100%" />
      <span className="knob" style={{ left: `${value}%` }} />
      <input type="range" min={0} max={100} step={5} value={value} aria-label="灰度百分比" onChange={(e) => onChange(Number(e.target.value))} />
    </div>
  );
}

function TargetPicker({ by, options, chosen, onAdd, onRemove }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState('');
  const ref = useRef(null);
  useOutside(ref, () => setOpen(false), open);
  const left = options.filter((o) => !chosen.some((c) => c.id === o.id));
  const addTenant = () => {
    const t = text.trim();
    if (t && !chosen.some((c) => c.id === t)) onAdd({ id: t, label: t });
    setText('');
    setOpen(false);
  };
  return (
    <span className="plu-tg" ref={ref}>
      {chosen.map((t) => (
        <Chip key={t.id} tone="line" sm>
          {t.label}
          <span className="plu-x" title="去掉" onClick={() => onRemove(t.id)}><Icon name="x" size={12} /></span>
        </Chip>
      ))}
      <Chip tone="line" sm icon="plus" className="plu-add" onClick={() => setOpen((v) => !v)}>{by === 'tenant' ? '指定租户' : '指定项目'}</Chip>
      {open ? (
        by === 'tenant' ? (
          <div className="menu plu-pop">
            <Inp
              autoFocus placeholder="租户名称，回车添加" value={text} onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') addTenant(); }}
            />
          </div>
        ) : (
          <Menu
            className="plu-pop"
            items={left.length ? left.map((o) => ({ label: o.label, icon: 'folder', onClick: () => onAdd(o) })) : [{ label: '没有可选的项目了', disabled: true }]}
            onPick={() => setOpen(false)}
          />
        )
      ) : null}
    </span>
  );
}

export function PublishCard({ upload, rollout, busy, error, onPublish, onHold }) {
  const [form, setForm] = useState(defaultPublishForm);
  useEffect(() => { setForm(defaultPublishForm()); }, [upload?.id]);
  const set = (patch) => setForm((f) => ({ ...f, ...patch }));
  const title = upload ? `发布 ${uploadTitle(upload)}` : '发布';
  if (!upload) {
    return <Card title={title} icon="upload"><div className="muted sm">上传的包通过静态检查后，在这里选择通道和灰度范围再发布。</div></Card>;
  }
  const block = publishBlock(upload);
  if (block) {
    const tone = { failed: 'red', published: 'teal', rejected: 'gray' }[upload.state] || 'gray';
    return (
      <Card title={title} icon="upload">
        <Note tone={tone} icon={{ checking: 'clock', published: 'check' }[upload.state]}>{block}</Note>
        {upload.state === 'published' ? (
          <div className="row mt12"><Button size="sm" icon="activity" onClick={() => navigate(pagePath('ops-ctl'))}>去运维中心</Button></div>
        ) : null}
      </Card>
    );
  }
  const problem = rolloutProblem(form);
  return (
    <Card title={title} icon="upload">
      <Field label="通道">
        <div className="col" style={{ gap: 8 }}>
          {CHANNELS.map(([k, sym, desc]) => (
            <div key={k} className={cx('opt', form.channel === k && 'on')} role="radio" aria-checked={form.channel === k} onClick={() => set({ channel: k })}>
              <span className="k">{sym}</span>
              <div><div className="t">{k}</div><div className="c">{desc}</div></div>
            </div>
          ))}
        </div>
      </Field>
      <div className="mt12">
        <Field label="灰度范围">
          <Seg items={[['project', '按项目'], ['tenant', '按租户']]} value={form.by} onChange={(by) => set({ by, targets: [] })} />
          <div className="row mt8"><RolloutRange value={form.percent} onChange={(percent) => set({ percent })} /><span className="b num">{form.percent}%</span></div>
          <div className="xs muted">
            {rolloutEstimate(form, rollout?.total)}；或指定：
            <TargetPicker
              by={form.by} options={form.by === 'project' ? rollout?.projects || [] : []} chosen={form.targets}
              onAdd={(t) => set({ targets: [...form.targets, t] })} onRemove={(id) => set({ targets: form.targets.filter((t) => t.id !== id) })}
            />
          </div>
        </Field>
      </div>
      <div className="pl-sw mt8">
        <div><div className="sm b">出错自动熔断</div><div className="xs muted">失败率超过 5% 或连续 5 次失败时，回退到上一版本</div></div>
        <Toggle on={form.autoBreaker} onChange={(v) => set({ autoBreaker: v })} />
      </div>
      {error || problem ? <Note tone={error ? 'red' : 'amber'} className="mt12">{error || problem}</Note> : null}
      <div className="row mt12">
        <Button kind="primary" icon="upload" disabled={busy || !!problem} onClick={() => onPublish(form, upload)}>发布到 {form.channel}</Button>
        <span className="sp" />
        <Button kind="ghost" disabled={busy} onClick={() => { setForm(defaultPublishForm()); onHold(upload); }}>暂不发布</Button>
      </div>
    </Card>
  );
}

// ---------- 审核记录（PL-15 timeline） ----------

export function TimelineCard({ upload, nameOf }) {
  const rows = timelineRows(upload, { nameOf });
  return (
    <Card title="审核记录" icon="history">
      {rows.length ? (
        <div className="tl">
          {rows.map((r, i) => (
            <div key={i} className="tl-i">
              <span className={cx('tl-dot', r.dot)}>{r.spin ? <span className="spin" style={{ width: 10, height: 10 }} /> : <Icon name={r.icon} />}</span>
              <div>
                <div className="tl-t">{r.title}</div>
                <div className="tl-m">{r.meta}</div>
                {r.note ? <div className="tl-m">原因：{r.note}</div> : null}
              </div>
            </div>
          ))}
        </div>
      ) : <div className="muted sm">还没有审核记录</div>}
    </Card>
  );
}
