// 新建项目（设计稿 create）：四步向导。P-03 名称检查、U-01 资料暂存、EV-01 数据源数、PL-01 包目录、C-06 引擎、A-09 模型配置、P-02 创建
import { useEffect, useMemo, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { navigate, pagePath, useRoute } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { ShellG } from '../../shell/Shell.jsx';
import { Button, Chip, Field, Icon, Menu, Prog, Toggle, cx, useOutside } from '../../ui/index.jsx';
import { createBody } from './createBody.js';

const STEPS = [['目标', '名称与一句话目标'], ['起点与资料', '从哪里开始、先给什么'], ['行业与能力', '行业包、可选能力包'], ['引擎与构建方式', '用什么跑、怎么跑']];
const EXAMPLES = ['店长一句话说明需求，按规则自动生成补货单', '根据业主报修判断故障紧急程度并派维修工单', '按订单预测和站点约束求解骑手排班方案'];
const PURPOSES = [['auto', '由助手判断'], ['goal', '建模目标依据'], ['evidence', '通用业务依据'], ['schema', '数据 Schema'], ['term', '术语表']];
const PURPOSE_LABEL = Object.fromEntries(PURPOSES);
const CAP_ICON = { core: 'cube', trace: 'flask', knowledge: 'book', solver: 'code', reverse: 'db', diagram: 'branch', 'web-research': 'globe', migrate: 'recycle', evolution: 'refresh' };

function useNameCheck(api, name) {
  const [res, setRes] = useState(null);
  useEffect(() => {
    if (!name.trim()) { setRes({ available: false, reason: 'empty' }); return undefined; }
    let alive = true;
    const t = setTimeout(() => api.nameCheck(name).then((r) => alive && setRes(r)).catch(() => {}), 200);
    return () => { alive = false; clearTimeout(t); };
  }, [api, name]);
  return res;
}

function FileRow({ f, onPurpose, onRemove }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useOutside(ref, () => setOpen(false), open);
  const isTable = /\.(xlsx?|xlsm|csv)$/i.test(f.name);
  const purpose = f.purpose && f.purpose !== 'auto' ? f.purpose : (f.upload?.detected?.purpose || 'auto');
  return (
    <div className="en-fl">
      <span style={{ color: 'var(--blue)' }}><Icon name={isTable ? 'table' : 'filetext'} size={14} /></span>
      <span className="grow ell">{f.name}</span>
      {f.progress != null && f.progress < 100 ? (
        <><span className="xs muted">上传中 {f.progress}%</span><span style={{ width: 80 }}><Prog p={f.progress} thin /></span></>
      ) : f.error ? <span className="xs" style={{ color: 'var(--red)' }}>{f.error}</span> : (
        <span className="xs faint">{f.purpose && f.purpose !== 'auto' ? '已手动指定' : '自动识别'}</span>
      )}
      <span ref={ref} style={{ position: 'relative' }}>
        <span className={cx('chip sm', purpose === 'schema' ? 'teal' : 'blue')} style={{ cursor: 'pointer' }} onClick={() => setOpen((v) => !v)}>
          {PURPOSE_LABEL[purpose]}<Icon name="chevd" />
        </span>
        {open ? (
          <Menu
            style={{ position: 'absolute', right: 0, top: 26, zIndex: 30 }}
            items={PURPOSES.map(([k, t]) => ({ label: t, hint: k === purpose ? '当前' : '', onClick: () => onPurpose(k) }))}
            onPick={() => setOpen(false)}
          />
        ) : null}
      </span>
      <Button size="xs" kind="ghost" icon="x" onClick={onRemove} title="移除" />
    </div>
  );
}

export default function CreatePage() {
  const { api, services } = useApp();
  const route = useRoute();
  const fileRef = useRef(null);
  const [step, setStep] = useState(1);
  const [name, setName] = useState('');
  const [goal, setGoal] = useState('');
  const [starts, setStarts] = useState(() => (route.query.start === 'example' ? ['example'] : ['upload']));
  const [files, setFiles] = useState([]);
  const [packs, setPacks] = useState([]);
  const [industries, setIndustries] = useState([]);
  const [systems, setSystems] = useState([]);
  const [caps, setCaps] = useState({});
  const [engines, setEngines] = useState(null);
  const [engine, setEngine] = useState('claude');
  const [model, setModel] = useState(null);
  const [sources, setSources] = useState(null);
  const [buildMode, setBuildMode] = useState('L2');
  const [busy, setBusy] = useState(false);
  const chk = useNameCheck(api, name);

  useEffect(() => {
    api.packs({ category: 'industry,system,capability' }).then((r) => {
      const items = r.items || [];
      setPacks(items);
      setIndustries(items.filter((p) => p.category === 'industry' && p.default_on).map((p) => p.name));
      setSystems(items.filter((p) => p.category === 'system' && p.default_on).map((p) => p.name));
      setCaps(Object.fromEntries(items.filter((p) => p.category === 'capability' && p.project_status !== 'required')
        .map((p) => [p.name, !!p.default_on && p.project_status !== 'offline'])));
    }).catch((e) => toast(errorText(e), 'alert'));
    api.engines().then((r) => { setEngines(r); setEngine(r.default || 'claude'); }).catch(() => {});
    api.modelSettings().then(setModel).catch(() => {});
    api.evidenceSources().then(setSources).catch(() => {});
  }, [api]);

  const addFiles = (list) => {
    [...(list || [])].forEach((file) => {
      const key = `${file.name}-${file.size}-${Math.random()}`;
      setFiles((fs) => [...fs, { key, name: file.name, progress: 0, purpose: 'auto' }]);
      services.upload(file, 'material', {
        onProgress: (p) => setFiles((fs) => fs.map((x) => (x.key === key ? { ...x, progress: p } : x))),
      }).then((up) => setFiles((fs) => fs.map((x) => (x.key === key ? { ...x, upload: up, progress: 100 } : x))))
        .catch((e) => setFiles((fs) => fs.map((x) => (x.key === key ? { ...x, error: errorText(e), progress: null } : x))));
    });
  };
  const removeFile = (f) => {
    if (f.upload) api.deleteUpload(f.upload.upload_id).catch(() => {});
    setFiles((fs) => fs.filter((x) => x.key !== f.key));
  };

  const conflict = chk?.reason === 'conflict';
  const capPacks = packs.filter((p) => p.category === 'capability');
  const selectedPages = capPacks.filter((p) => p.ui?.kind === 'page' && caps[p.name]).flatMap((p) => p.ui.pages.map((x) => x.title));
  const srcCount = sources?.total ?? 0;
  // 证据源本期只留接口（EV-01 回 501）：读不到数据源登记时不出「连接业务系统」（ADR-0056）
  const evidenceOn = !!sources;
  const ctx = model?.engines?.[engine]?.context;
  const uploading = files.some((f) => f.progress != null && f.progress < 100);

  const create = async () => {
    if (!name.trim()) { setStep(1); toast('请输入项目名称', 'alert'); return; }
    if (!goal.trim()) { setStep(1); toast('请写一句话目标', 'alert'); return; }
    setBusy(true);
    try {
      const finalName = conflict && chk.conflict?.candidates?.[0] ? chk.conflict.candidates[0] : name;
      const meta = await api.createProject(createBody({ name: finalName, goal, starts, files, industries, systems, caps, engine, buildMode }));
      toast(`项目已创建：${meta.name}`, 'check');
      setTimeout(() => navigate(pagePath(meta.landing_page || 'wb-materials', meta.id)), 600);
    } catch (e) {
      if (e.code?.startsWith('project_name')) setStep(1);
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };

  const nameHint = !chk ? null : chk.reason === 'empty'
    ? <span className="row g4" style={{ color: 'var(--red)' }}>请输入项目名称</span>
    : conflict
      ? <span className="row g4" style={{ color: 'var(--red)' }}><Icon name="alert" size={12} />你已经有一个叫「{name.trim()}」的项目</span>
      : chk.reason === 'too_long'
        ? <span className="row g4" style={{ color: 'var(--red)' }}>项目名称不能超过 200 个字符</span>
        : <span className="row g4" style={{ color: 'var(--teal)' }}><Icon name="check" size={12} />名称可用 · 知识包包名自动生成为 <span className="mono">{chk.package_name}</span></span>;

  const startItems = [
    ['upload', 'upload', '上传资料', '业务说明、规则文档、样例单据、表结构'],
    ...(evidenceOn ? [['connect_system', 'plug', '连接业务系统', srcCount ? `已登记 ${srcCount} 个数据源可用：${sources.items.map((s) => s.id).join('、')}` : '还没有登记数据源，管理员登记后可用']] : []),
    ['industry_pack', 'layers', '从行业包开始', '用行业术语、参考对象和示例起步'],
    ['import_package', 'pkg', '导入已有知识包', '从别处发布的包继续建模'],
    ['example', 'play', '看示例项目', '5 分钟走完一遍完整流程'],
  ];

  const uiTag = (p) => {
    const kind = p.ui?.kind;
    if (kind === 'kernel') return <span className="en-ui core"><Icon name="layout" size={12} />{p.ui.note}</span>;
    if (kind === 'page') return <span className="en-ui page"><Icon name="layout" size={12} />{p.ui.note}</span>;
    if (kind === 'subpages') return <span className="en-ui sub"><Icon name="folder" size={12} />{p.ui.note}</span>;
    if (kind === 'none') return <span className="en-ui none">{p.ui.note}</span>;
    return null;
  };

  return (
    <ShellG active="projects" flush>
      <div className="en-cr">
        <div className="en-cr-l">
          <a className="link row g4 sm" href="#/projects"><Icon name="arrowl" size={14} />返回我的项目</a>
          <div className="mt12"><h1 style={{ fontSize: 18, fontWeight: 700 }}>新建项目</h1></div>
          <div className="en-steps">
            {STEPS.map(([t, d], i) => {
              const n = i + 1;
              return (
                <div key={t} className={cx('s', n === step ? 'on' : n < step && 'done')} onClick={() => setStep(n)}>
                  <span className="n">{n < step ? <Icon name="check" size={12} /> : n}</span>
                  <div><div className="t">{t}</div><div className="d">{d}</div></div>
                </div>
              );
            })}
          </div>
        </div>
        <div className="card en-cr-m">
          <div className="bd">
            {step === 1 ? (
              <div>
                <h2>先说说这个知识包要解决什么问题</h2>
                <p>先写个粗略的目标就行，助手会结合资料带你确认。</p>
                <div className="mt20">
                  <Field label="项目名称" req hint={nameHint}>
                    <div className={cx('inp', (conflict || chk?.reason === 'too_long') && 'err')}><Icon name="edit" size={14} /><input value={name} onChange={(e) => setName(e.target.value)} placeholder="给项目起个名字" autoFocus /></div>
                  </Field>
                </div>
                <div className="mt16">
                  <Field label="一句话目标" req>
                    <div className="inp area" style={{ minHeight: 92 }}><textarea style={{ height: 72 }} value={goal} onChange={(e) => setGoal(e.target.value)} placeholder="谁在用、给什么输入、要什么输出" /></div>
                  </Field>
                </div>
                <div className="row wrap mt8" style={{ gap: 6 }}>
                  <span className="xs muted">参考写法：</span>
                  {EXAMPLES.map((t) => <span key={t} className="chip line en-ex" style={{ cursor: 'pointer' }} onClick={() => setGoal(t)}><Icon name="sparkle" />{t}</span>)}
                </div>
                <div className="sec-t" style={{ marginTop: 20 }}><Icon name="sparkle" size={14} />好的目标写清楚三件事</div>
                <div className="grid3" style={{ gap: 10 }}>
                  {[['user', '谁在用', '店长 / 区域督导'], ['download', '给什么输入', '门店、品类、库存、到货周期'], ['upload', '要什么输出', '补货单初稿 + 超量清单']].map(([i, t, d]) => (
                    <div key={t} className="card pad" style={{ padding: '12px 14px' }}>
                      <div className="row g4" style={{ color: 'var(--blue2)' }}><Icon name={i} size={14} /><b className="sm">{t}</b></div>
                      <div className="sm mt4">{d}</div>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}
            {step === 2 ? (
              <div>
                <h2>从哪里开始？</h2>
                <p>{evidenceOn ? '可以多选：先传资料，再连业务系统，也可以先什么都不传。' : '可以多选，也可以先什么都不传。'}</p>
                <div className="grid3 mt16" style={{ gridTemplateColumns: `repeat(${startItems.length},1fr)`, gap: 10 }}>
                  {startItems.map(([k, i, t, d]) => {
                    const on = starts.includes(k);
                    const dis = k === 'connect_system' && !srcCount;
                    return (
                      <div key={k} className={cx('en-pick', on && 'on', dis && 'dis')} onClick={() => { if (dis) return; if (k === 'import_package') { navigate('/projects/import'); return; } setStarts((s) => (on ? s.filter((x) => x !== k) : [...s, k])); }}>
                        <span className={cx('ck chk', on && 'on')} />
                        <span className="pi"><Icon name={i} size={18} /></span>
                        <div className="t mt8">{t}</div>
                        <div className="c">{d}</div>
                      </div>
                    );
                  })}
                </div>
                <input ref={fileRef} type="file" multiple hidden accept=".md,.markdown,.txt,.doc,.docx,.xls,.xlsx,.xlsm,.csv,.pdf,.drawio,.html,.htm,.png,.jpg,.jpeg" onChange={(e) => { addFiles(e.target.files); e.target.value = ''; }} />
                <div className="en-drop mt16" onClick={() => fileRef.current?.click()} onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); addFiles(e.dataTransfer.files); }}>
                  <div className="ic"><Icon name="upload" size={20} /></div>
                  <b style={{ color: 'var(--ink)' }}>把资料拖到这里，或点击选择</b>
                  <div className="sm mt4">md / txt / pdf / word / excel / drawio · 单个不超过 20 MB · 现在没有也可以直接开始</div>
                </div>
                {files.length ? (
                  <>
                    <div className="sec-t" style={{ marginTop: 16 }}>
                      已选 {files.length} 份 · 用途由平台自动识别，可以改<span className="sp" />
                      <a className="link xs" onClick={() => files.forEach(removeFile)}>全部移除</a>
                    </div>
                    <div className="col" style={{ gap: 6 }}>
                      {files.map((f) => (
                        <FileRow key={f.key} f={f} onRemove={() => removeFile(f)} onPurpose={(p) => setFiles((fs) => fs.map((x) => (x.key === f.key ? { ...x, purpose: p } : x)))} />
                      ))}
                    </div>
                  </>
                ) : null}
              </div>
            ) : null}
            {step === 3 ? (
              <div>
                <h2>选行业和能力</h2>
                <p>选好的能力之后在项目设置里随时增减，从下一条消息开始生效。带页面的能力启用后，会出现在左栏的「插件」分组里。</p>
                <div className="sec-t" style={{ marginTop: 16 }}><Icon name="layers" size={14} />行业包 · 可多选<span className="sp" /><a className="link xs" href="#/plugins">去插件中心看更多 <Icon name="chev" size={12} /></a></div>
                <div className="grid3" style={{ gap: 10 }}>
                  {packs.filter((p) => p.category === 'industry').map((p) => {
                    const on = industries.includes(p.name);
                    return (
                      <div key={p.name} className={cx('en-pick', on && 'on')} onClick={() => setIndustries((s) => (on ? s.filter((x) => x !== p.name) : [...s, p.name]))}>
                        <span className={cx('ck chk', on && 'on')} />
                        <div className="t">{p.label}</div>
                        <div className="c mt4">{p.description}</div>
                      </div>
                    );
                  })}
                </div>
                <div className="sec-t" style={{ marginTop: 16 }}><Icon name="server" size={14} />系统知识包 · 可选</div>
                <div className="row wrap" style={{ gap: 8 }}>
                  {packs.filter((p) => p.category === 'system').map((p) => {
                    const on = systems.includes(p.name);
                    return (
                      <span key={p.name} className={cx('chip', on ? 'blue' : 'line')} style={{ height: 30, padding: '0 11px', cursor: 'pointer' }} onClick={() => setSystems((s) => (on ? s.filter((x) => x !== p.name) : [...s, p.name]))}>
                        <Icon name={on ? 'check' : 'plus'} />{p.label}
                      </span>
                    );
                  })}
                </div>
                <div className="sec-t" style={{ marginTop: 16 }}><Icon name="puzzle" size={14} />能力包<span className="sp" /><span className="xs faint" style={{ fontWeight: 500 }}>不是每个能力包都带页面</span></div>
                <div className="grid2" style={{ gap: 8 }}>
                  {capPacks.map((p) => {
                    const required = p.project_status === 'required';
                    const offline = p.project_status === 'offline';
                    const on = required || !!caps[p.name];
                    return (
                      <div key={p.name} className={cx('en-cap', on && 'on', required && 'lock')}>
                        <span className="pi"><Icon name={CAP_ICON[p.name] || 'puzzle'} size={16} /></span>
                        <div className="grow" style={{ minWidth: 0 }}>
                          <div className="row g4"><b>{p.label}</b>{p.badges?.includes('recommended') ? <Chip tone="blue" sm>推荐</Chip> : null}</div>
                          <div className="xs muted">{p.description}</div>
                          {uiTag(p)}
                        </div>
                        {required ? <span className="chip gray sm"><Icon name="lock" />必装</span>
                          : offline ? <span className="chip gray sm">离线部署禁用</span>
                            : <Toggle on={on} onChange={(v) => setCaps((c) => ({ ...c, [p.name]: v }))} />}
                      </div>
                    );
                  })}
                </div>
                <div className="en-rpv">
                  <span className="xs muted" style={{ marginRight: 2 }}>建好后的左栏</span>
                  {[['layout', '工作台'], ['graph', 'Studio'], ['folder', '资料'], ['upload', '发布']].map(([i, t]) => <span key={t} className="it"><Icon name={i} size={12} />{t}</span>)}
                  <span className="dv" />
                  <span className="xs muted">插件</span>
                  {selectedPages.length ? selectedPages.map((t) => <span key={t} className="it pl"><Icon name="puzzle" size={12} />{t}</span>) : <span className="xs faint">没有带页面的能力</span>}
                </div>
              </div>
            ) : null}
            {step === 4 ? (
              <div>
                <h2>用什么跑、怎么跑</h2>
                <p>都可以之后在项目设置里改。</p>
                <div className="sec-t" style={{ marginTop: 16, minHeight: 42 }}><Icon name="cpu" size={14} />会话引擎<span className="sp" /></div>
                <div className="grid2" style={{ gap: 10 }}>
                  {(engines?.items || []).map((e) => {
                    const on = engine === e.id;
                    const dis = !e.installed;
                    return (
                      <div key={e.id} className={cx('en-pick', on && 'on', dis && 'dis')} onClick={() => !dis && setEngine(e.id)}>
                        <span className={cx('ck rad', on && 'on')} />
                        <div className="row g12">
                          <span className="pi"><Icon name={e.channel === 'stable' ? 'shieldok' : 'zap'} size={18} /></span>
                          <div>
                            <div className="t">{e.label}</div>
                            <div className="c">{dis ? <span style={{ color: 'var(--amber)' }}>{e.unavailable_reason || '当前部署未安装'}</span> : e.channel === 'stable' ? '功能最完整，推荐' : '新引擎，部分能力还在完善'}</div>
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
                <div className="grid2 mt12" style={{ gap: 10 }}>
                  <Field label="模型" hint={<>想用自己的 Key？在 <a className="link" href="#/me?section=model">个人设置 › 我的模型配置</a> 里填写</>}>
                    <div className="inp sel"><Icon name="sparkle" size={14} /><span className="grow ell">{model?.engines?.[engine]?.source === 'personal' ? '使用我的模型配置（个人 Key）' : '使用我的模型配置'}</span></div>
                  </Field>
                  <Field label="上下文窗口" hint="按模型上下文推荐表自动带出">
                    <div className="inp sel"><span className="grow ell">{ctx ? `${Math.round(ctx.window / 1000)}k · 自动压缩阈值 ${ctx.compact_at_pct}%` : '按模型推荐值'}</span></div>
                  </Field>
                </div>
                <div className="sec-t" style={{ marginTop: 20 }}><Icon name="compass" size={14} />构建方式</div>
                <div className="grid2" style={{ gap: 12 }}>
                  <div className={cx('en-mode', buildMode === 'L2' && 'on')} onClick={() => setBuildMode('L2')}>
                    <div className="row"><span className="lv">L2</span><b style={{ fontSize: 15 }}>陪伴建模</b><span className="sp" /><span className={cx('rad', buildMode === 'L2' && 'on')} /></div>
                    <ul>
                      <li><Icon name="hand" size={12} />每个决定点发决策卡，等你选</li>
                      <li><Icon name="merge" size={12} />每一步你点「确认生效」才算完成</li>
                      <li><Icon name="target" size={12} />适合第一次建模、规则复杂的场景</li>
                    </ul>
                  </div>
                  <div className={cx('en-mode l4', buildMode === 'L4' && 'on')} onClick={() => setBuildMode('L4')}>
                    <div className="row"><span className="lv">L4</span><b style={{ fontSize: 15 }}>自主构建</b><span className="badge-new">新</span><span className="sp" /><span className={cx('rad', buildMode === 'L4' && 'on')} /></div>
                    <ul>
                      <li><Icon name="rocket" size={12} />在范围和预算内一路跑完，不停下来问</li>
                      <li><Icon name="bot" size={12} />拿不准的按策略决定并记账，产出标「需复核」</li>
                      <li><Icon name="target" size={12} />适合资料齐全、想先快速出一版</li>
                    </ul>
                  </div>
                </div>
                {buildMode === 'L4' ? (
                  <div className="en-l4n">
                    <div className="row g8"><Icon name="info" size={14} /><b className="sm">自主构建的范围、预算和生产库采集授权，在「项目设置 › 构建方式」里调</b></div>
                    <div className="xs muted mt4" style={{ lineHeight: 1.7, paddingLeft: 22 }}>资料分析和切片规划照常进行；蓝图确认后，助手按设置一路建完，结束后集中复核。它不会发布、不会删除已确认的内容。项目建好后可以在项目设置里先看看默认设置。</div>
                  </div>
                ) : null}
              </div>
            ) : null}
          </div>
          <div className="ftr">
            <Button kind="ghost" onClick={() => navigate('/projects')}>取消并返回项目列表</Button>
            <span className="sp" />
            <span className="xs muted">第 {step} 步，共 4 步</span>
            <Button icon="arrowl" disabled={step === 1} onClick={() => setStep((s) => Math.max(1, s - 1))}>上一步</Button>
            {step < 4 ? (
              <Button kind="primary" after={<Icon name="arrow" size={14} />} onClick={() => setStep((s) => s + 1)}>下一步</Button>
            ) : (
              <Button kind={buildMode === 'L4' ? 'l4' : 'primary'} icon={buildMode === 'L4' ? 'rocket' : 'play'} disabled={busy || uploading} onClick={create}>
                {conflict ? '改名并创建' : '创建并开始建模'}
              </Button>
            )}
          </div>
        </div>
      </div>
    </ShellG>
  );
}
