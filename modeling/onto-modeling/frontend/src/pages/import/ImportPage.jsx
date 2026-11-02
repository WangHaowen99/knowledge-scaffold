// 导入项目（设计稿 import）：U-01 暂存上传、P-10 检查、P-03 名称检查、P-11 导入
import { useEffect, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText, formatBytes } from '../../api/errors.js';
import { PageHead, ShellG } from '../../shell/Shell.jsx';
import { Button, Chip, Field, Icon, Note, Prog, Seg, Table, cx } from '../../ui/index.jsx';

const FORMATS = [
  ['transfer', 'archive', '迁移包', '完整工作区，含版本历史。用于从其他环境搬过来；凭据不随包迁移，导入后重新授权数据源。'],
  ['published_package', 'pkg', '已发布知识包', '只有交付物：对象、操作、规则、指标与脚本落到工作区继续建模。资料与报告为空，阶段进度从头计算；原包完整留底，映射与引用不会丢失。'],
  ['workspace_archive', 'folder', '工作区压缩包', '建模任务工作区内容（ontology/、resource/、research/ 等），按原样导入。'],
];

const BLOCKERS = {
  symlink: ['包含符号链接', '出于安全原因，压缩包里的符号链接和 .. 路径一律拒绝。'],
  path_traversal: ['包含 .. 路径', '出于安全原因，压缩包里的符号链接和 .. 路径一律拒绝。'],
  absolute_path: ['包含绝对路径', '压缩包里的文件必须用相对路径。'],
  unknown_format: ['认不出格式', '缺少 ontology/ 目录和包清单。'],
  empty_archive: ['压缩包是空的', '导入后项目里会什么都没有。'],
  too_large: ['超过 200 MB 上限', '请拆分或精简后再导入。'],
  solver_contract_invalid: ['求解契约不合法', '包里的求解契约有语法错误。'],
  no_objects: ['没有业务对象', '包里找不到任何业务对象。'],
};

export default function ImportPage() {
  const { api, services } = useApp();
  const fileRef = useRef(null);
  const [file, setFile] = useState(null);
  const [upload, setUpload] = useState(null);
  const [progress, setProgress] = useState(null);
  const [inspect, setInspect] = useState(null);
  const [name, setName] = useState('');
  const [buildMode, setBuildMode] = useState('L2');
  const [pane, setPane] = useState('pick');
  const [ncPick, setNcPick] = useState('A');
  const [custom, setCustom] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const choose = async (f) => {
    if (!f) return;
    if (upload) api.deleteUpload(upload.upload_id).catch(() => {});
    setFile(f);
    setInspect(null);
    setError(null);
    setPane('pick');
    setProgress(0);
    try {
      const up = await services.upload(f, 'project_import', { onProgress: setProgress });
      setUpload(up);
      const r = await api.inspectImport({ upload_id: up.upload_id });
      setInspect(r);
      setName(r.suggested_name || '');
      if (r.blockers?.length) setPane('bad');
    } catch (e) {
      setError(e);
    } finally {
      setProgress(null);
    }
  };

  useEffect(() => () => { if (upload) api.deleteUpload(upload.upload_id).catch(() => {}); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const next = async () => {
    setBusy(true);
    try {
      const chk = await api.nameCheck(name);
      if (chk.reason === 'conflict') {
        setInspect((i) => ({ ...i, name_check: chk }));
        setNcPick('A');
        setPane('conflict');
      } else if (!chk.available) {
        toast(chk.reason === 'empty' ? '请输入项目名称' : '项目名称不能超过 200 个字符', 'alert');
      } else setPane('confirm');
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };

  const doImport = async () => {
    setBusy(true);
    try {
      let body = { upload_id: upload.upload_id, name, build_mode: buildMode };
      if (pane === 'conflict') {
        body = ncPick === 'A'
          ? { ...body, name: inspect.name_check.conflict?.candidates?.[0] || name, name_strategy: 'exact' }
          : { ...body, name: custom.trim() };
      }
      const meta = await api.importProject(body);
      toast(pane === 'conflict' ? `导入完成（已改名为「${meta.name}」）` : '导入完成，原包已留底', 'download');
      setUpload(null);
      setTimeout(() => navigate(pagePath(meta.landing_page || 'wb-l2', meta.id)), 700);
    } catch (e) {
      if (e.code === 'project_name_conflict') {
        setInspect((i) => ({ ...i, name_check: { ...(i.name_check || {}), conflict: e.details?.conflict } }));
        setPane('conflict');
      } else if (e.code === 'unsupported_archive_kind' || e.code === 'archive_invalid') {
        setInspect((i) => ({ ...i, blockers: e.details?.blockers || [{ code: 'unknown_format', message: errorText(e) }] }));
        setPane('bad');
      }
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };

  const step2 = pane === 'confirm' || pane === 'conflict';
  const kindIndex = FORMATS.findIndex(([k]) => k === inspect?.kind);
  const c = inspect?.counts || {};
  const conflictName = inspect?.name_check?.conflict;
  const caps = (inspect?.after_import?.capabilities || []).map((x) => x.label || x.name).join('、');

  return (
    <ShellG active="projects">
      <div className="en-im">
        <div className="row mb12"><a className="link row g4" href="#/projects"><Icon name="arrowl" size={14} />返回我的项目</a><span className="sp" /></div>
        <PageHead title="导入项目" desc="从其他环境搬过来的迁移包，或者别处发布的知识包，导入后成为你的一个新项目。" eyebrow="导入" />
        <div className="en-im-steps">
          <div className={cx('s', step2 ? 'done' : 'on')}><i>{step2 ? <Icon name="check" size={12} /> : '1'}</i>选择与命名</div>
          <span className="ln" />
          <div className={cx('s', step2 && 'on')}><i>2</i>内容确认</div>
        </div>
        <input ref={fileRef} type="file" accept=".zip,application/zip" hidden onChange={(e) => { choose(e.target.files?.[0]); e.target.value = ''; }} />
        <div className="card" style={{ padding: '22px 24px' }}>
          {pane === 'pick' ? (
            <div>
              {file ? (
                <div className="en-file">
                  <span className="fi"><Icon name="archive" size={18} /></span>
                  <div className="grow">
                    <div className="b">{file.name}</div>
                    <div className="xs muted">{formatBytes(file.size)} · 从本机选择 · 格式自动识别</div>
                    {progress != null ? <div className="mt4" style={{ width: 240 }}><Prog p={progress} thin /></div> : null}
                  </div>
                  <Button size="sm" kind="ghost" icon="refresh" onClick={() => fileRef.current?.click()}>重新选择</Button>
                </div>
              ) : (
                <div
                  className="en-drop"
                  onClick={() => fileRef.current?.click()}
                  onDragOver={(e) => e.preventDefault()}
                  onDrop={(e) => { e.preventDefault(); choose(e.dataTransfer.files?.[0]); }}
                >
                  <div className="ic"><Icon name="upload" size={20} /></div>
                  <b style={{ color: 'var(--ink)' }}>把迁移包或知识包拖到这里，或点击选择</b>
                  <div className="sm mt4">zip · 不超过 200 MB · 格式自动识别</div>
                </div>
              )}
              {error ? <Note tone="red" className="mt12">{errorText(error)}</Note> : null}
              {inspect ? (
                <>
                  <div className="sec-t" style={{ marginTop: 20 }}>识别结果 · 格式自动识别</div>
                  <div className="en-fmt">
                    {FORMATS.map(([k, i, t, d], idx) => (
                      <div key={k} className={cx('f', idx === kindIndex ? 'on' : 'off')}>
                        {idx === kindIndex ? <span className="hit chip blue sm"><Icon name="check" />已识别</span> : null}
                        <span style={{ color: idx === kindIndex ? 'var(--blue)' : 'var(--ink3)' }}><Icon name={i} size={20} /></span>
                        <div className="b mt8">{t}</div>
                        <div className="xs muted mt4" style={{ lineHeight: 1.6 }}>{d}</div>
                      </div>
                    ))}
                  </div>
                  <div className="grid2 mt20">
                    <Field label="项目名称" req hint="默认取包里的名称，可以改">
                      <div className="inp"><Icon name="edit" size={14} /><input value={name} onChange={(e) => setName(e.target.value)} /></div>
                    </Field>
                    <Field label="构建方式" hint="导入后在「项目设置 › 构建方式」里也能改">
                      <Seg items={[['L2', '陪伴建模（L2）'], ['L4', '自主构建（L4）']]} value={buildMode} onChange={setBuildMode} />
                    </Field>
                  </div>
                </>
              ) : null}
            </div>
          ) : null}
          {pane === 'confirm' && inspect ? (
            <div className="grid2" style={{ gridTemplateColumns: '1.2fr 1fr', gap: 20 }}>
              <div>
                <Table compact cols={['项目', '内容']} rows={[
                  ['识别格式', <Chip tone="blue" icon={FORMATS[kindIndex]?.[1] || 'pkg'}>{inspect.kind_label}</Chip>],
                  ['压缩包根目录', <span className="mono">{inspect.root_dir}</span>],
                  ['文件数', <b className="num">{inspect.files}</b>],
                  ['业务对象', <b className="num">{c.objects}</b>],
                  ['业务操作', <b className="num">{c.operations}</b>],
                  ['规则 / 指标', <><b className="num">{c.rules}</b> / <b className="num">{c.metrics}</b></>],
                  ['脚本', <><b className="num">{c.scripts}</b>{c.solver_operations ? `（含 ${c.solver_operations} 个求解评估器）` : ''}</>],
                  ['总大小', <b className="num">{formatBytes(inspect.total_size)}</b>],
                ]} />
              </div>
              <div className="col" style={{ gap: 10 }}>
                {inspect.kind === 'published_package' ? (
                  <Note tone="blue" icon="pkg"><b>已发布知识包只含交付物。</b>对象、操作、规则、指标与脚本会落到工作区继续建模；资料与报告等过程资产为空，阶段进度从头计算。原包会完整留底，映射与引用不会丢失。</Note>
                ) : inspect.kind === 'transfer' ? (
                  <Note tone="blue" icon="archive"><b>迁移包会原样恢复。</b>完整工作区和版本历史都会带过来；凭据不随包迁移。</Note>
                ) : null}
                {inspect.evidence_sources?.length ? (
                  <Note tone="gray" icon="db">证据快照不随包导入；包里登记的数据源 {inspect.evidence_sources.map((s) => s.id).join('、')} 只带连接信息，不带凭据，导入后需要重新授权。</Note>
                ) : null}
                <div className="card pad">
                  <div className="sm b">导入后</div>
                  <div className="xs muted mt4" style={{ lineHeight: 1.8 }}>
                    项目名：{name}<br />
                    能力：按包里记录的能力自动匹配{caps ? `（${caps}）` : ''}<br />
                    流程：从「资料分析」开始{inspect.after_import?.mark_needs_review ? '，已有产物标为「需复核」' : ''}
                  </div>
                </div>
              </div>
            </div>
          ) : null}
          {pane === 'conflict' ? (
            <div>
              <Note tone="amber"><b>项目名称已存在。</b>你已经有一个叫「{conflictName?.attempted_name || name}」的项目，换个名字就能继续。</Note>
              <div className="col mt16" style={{ gap: 8 }}>
                {conflictName?.candidates?.[0] ? (
                  <div className={cx('opt', ncPick === 'A' && 'on')} onClick={() => setNcPick('A')}>
                    <span className="k">A</span>
                    <div className="grow"><div className="t">改名为「{conflictName.candidates[0]}」</div><div className="c">系统建议的名称，可以直接用</div></div>
                  </div>
                ) : null}
                <div className={cx('opt', ncPick === 'B' && 'on')} onClick={() => setNcPick('B')}>
                  <span className="k">B</span>
                  <div className="grow">
                    <div className="t">自定义名称</div>
                    <div className="mt8"><div className="inp"><Icon name="edit" size={14} /><input placeholder="输入新的项目名称" value={custom} onChange={(e) => { setCustom(e.target.value); setNcPick('B'); }} /></div></div>
                  </div>
                </div>
              </div>
              <div className="xs muted mt8">新名称不能与原名称相同，也不能和你已有的项目重名。</div>
            </div>
          ) : null}
          {pane === 'bad' && inspect ? (
            <div className="row g16" style={{ alignItems: 'flex-start' }}>
              <span style={{ width: 52, height: 52, borderRadius: 16, display: 'grid', placeItems: 'center', background: 'var(--r50)', color: 'var(--red)', flex: 'none' }}><Icon name="ban" size={24} /></span>
              <div className="grow">
                <h3 style={{ fontSize: 16 }}>这个压缩包不能从这里导入</h3>
                <div className="muted sm mt4">文件：<span className="mono">{file?.name}</span></div>
                <div className="col mt12" style={{ gap: 8 }}>
                  {inspect.blockers.map((b, i) => {
                    const [t, why] = BLOCKERS[b.code] || [b.message, ''];
                    return (
                      <div className="note red" key={i}>
                        <Icon name="alert" />
                        <div className="grow"><b>{t}</b>{b.entry ? <>：<span className="mono">{b.entry}</span></> : null}。{why}</div>
                      </div>
                    );
                  })}
                  <div className="note gray"><Icon name="info" /><div className="grow">其他常见原因：认不出格式（缺少 <span className="mono">ontology/</span> 目录和包清单）、超过 200 MB 上限、压缩包是空的（导入会显示成功但项目里什么都没有）。</div></div>
                </div>
              </div>
            </div>
          ) : null}
        </div>
        <div className="row mt16">
          <Button kind="ghost" onClick={() => navigate('/projects')}>取消</Button>
          <span className="sp" />
          <span>
            {pane === 'pick' ? <Button kind="primary" icon="arrow" disabled={!inspect || busy || !name.trim()} onClick={next}>下一步</Button> : null}
            {pane === 'confirm' ? <><Button onClick={() => setPane('pick')}>上一步</Button> <Button kind="primary" icon="download" disabled={busy} onClick={doImport}>导入</Button></> : null}
            {pane === 'conflict' ? <><Button onClick={() => setPane('pick')}>上一步</Button> <Button kind="primary" disabled={busy || (ncPick === 'B' && !custom.trim())} onClick={doImport}>改名并导入</Button></> : null}
            {pane === 'bad' ? <Button kind="primary" icon="upload" onClick={() => { setPane('pick'); setInspect(null); setFile(null); fileRef.current?.click(); }}>重新选择文件</Button> : null}
          </span>
        </div>
      </div>
    </ShellG>
  );
}
