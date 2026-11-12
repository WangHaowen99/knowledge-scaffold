// 导入项目（设计稿 import）：U-01 暂存上传、P-10 检查、P-03 名称检查、P-11 导入；登录后可从知识包 Hub 导入（HB-04）
import { useEffect, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText, formatBytes } from '../../api/errors.js';
import { PageHead, ShellG } from '../../shell/Shell.jsx';
import { Button, Chip, Field, Icon, Note, Prog, Seg, Table, cx } from '../../ui/index.jsx';
import HubImportModal from './HubImportModal.jsx';

// 格式由后端自动识别，页面只显示识别结果（R2）
const FORMATS = [
  ['transfer', 'archive', '迁移包'],
  ['published_package', 'pkg', '已发布知识包'],
  ['workspace_archive', 'folder', '工作区压缩包'],
];

const BLOCKERS = {
  symlink: ['包含符号链接', ''],
  path_traversal: ['包含 .. 路径', ''],
  absolute_path: ['包含绝对路径', '压缩包里的文件必须用相对路径。'],
  unknown_format: ['认不出格式', '缺少 ontology/ 目录和包清单。'],
  empty_archive: ['压缩包是空的', ''],
  too_large: ['超过 200 MB 上限', '请拆分或精简后再导入。'],
  solver_contract_invalid: ['求解契约不合法', ''],
  no_objects: ['没有业务对象', ''],
};

export default function ImportPage() {
  const { api, services, config } = useApp();
  const [hubOpen, setHubOpen] = useState(false);
  // Hub 按登录用户授权：本地模式（鉴权关闭）没有 Hub 身份，入口不出现
  const hubOn = config?.auth?.enabled !== false;
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
      toast(pane === 'conflict' ? `导入完成（已改名为「${meta.name}」）` : '导入完成', 'download');
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
  const format = FORMATS.find(([k]) => k === inspect?.kind);
  const c = inspect?.counts || {};
  const conflictName = inspect?.name_check?.conflict;
  const caps = (inspect?.after_import?.capabilities || []).map((x) => x.label || x.name).join('、');

  return (
    <ShellG active="projects">
      <div className="en-im">
        <div className="row mb12"><a className="link row g4" href="#/projects"><Icon name="arrowl" size={14} />返回我的项目</a><span className="sp" /></div>
        <PageHead title="导入项目" actions={hubOn ? <Button icon="pkg" onClick={() => setHubOpen(true)}>已发布知识包</Button> : null} />
        <HubImportModal open={hubOpen} onClose={() => setHubOpen(false)} />
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
                    <div className="xs muted">{formatBytes(file.size)}</div>
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
                  <div className="sm mt4">zip · 不超过 200 MB</div>
                </div>
              )}
              {error ? <Note tone="red" className="mt12">{errorText(error)}</Note> : null}
              {inspect ? (
                <>
                  <div className="en-fmt">
                    <span className="ic"><Icon name={format?.[1] || 'pkg'} size={16} /></span>
                    <span className="muted">已识别：</span><b>{format?.[2] || inspect.kind_label}</b>
                  </div>
                  <div className="grid2 mt20">
                    <Field label="项目名称" req>
                      <div className="inp"><Icon name="edit" size={14} /><input value={name} onChange={(e) => setName(e.target.value)} /></div>
                    </Field>
                    <Field label="构建方式">
                      <Seg items={[['L2', '陪伴建模'], ['L4', '自主构建']]} value={buildMode} onChange={setBuildMode} />
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
                  ['识别格式', <Chip tone="blue" icon={format?.[1] || 'pkg'}>{inspect.kind_label}</Chip>],
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
                  <Note tone="blue" icon="pkg">已发布知识包只含交付物，不带资料和报告。</Note>
                ) : inspect.kind === 'transfer' ? (
                  <Note tone="blue" icon="archive">迁移包会原样恢复，含版本历史。</Note>
                ) : null}
                <div className="card pad">
                  <div className="sm b">导入后</div>
                  <div className="xs muted mt4" style={{ lineHeight: 1.8 }}>
                    {caps ? <>能力：{caps}<br /></> : null}
                    流程：{inspect.after_import?.start_title ? `从「${inspect.after_import.start_title}」开始` : '沿用原项目的进度'}{inspect.after_import?.mark_needs_review ? '，已有产物标「需复核」' : ''}
                  </div>
                </div>
              </div>
            </div>
          ) : null}
          {pane === 'conflict' ? (
            <div>
              <Note tone="amber">已有同名项目「{conflictName?.attempted_name || name}」</Note>
              <div className="col mt16" style={{ gap: 8 }}>
                {conflictName?.candidates?.[0] ? (
                  <div className={cx('opt', ncPick === 'A' && 'on')} onClick={() => setNcPick('A')}>
                    <span className="k">A</span>
                    <div className="grow"><div className="t">改名为「{conflictName.candidates[0]}」</div></div>
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
                        <div className="grow"><b>{t}</b>{b.entry ? <>：<span className="mono">{b.entry}</span></> : null}{why ? `。${why}` : null}</div>
                      </div>
                    );
                  })}
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
