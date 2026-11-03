// 资料分析与建模蓝图的对话卡片（契约 4.7：upload_prompt、material_map、research_summary、blueprint、build_choice）
// 设计稿 wb-materials、wb-blueprint；弹窗在 materials/ 里，由工作台统一挂（useOpenDialog）
import { useContext, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import {
  Button, Chip, Icon, Prog, cx,
} from '../../ui/index.jsx';
import { evidenceInProject } from '../../state/evidence.js';
import { CC, WbCtx } from './parts.jsx';
import {
  MatIcon, pageCommand, useMaterialUpload, useOpenDialog,
} from './materials/parts.jsx';
import {
  acceptLabel, confirmOptions, depLabel, goalPending, miniStatus, orderLabel, purposeLabel, readState,
} from './materials/model.js';
import '../../styles/materials.css';

// 拖到上传区：直接作为资料上传（R-02），不进输入框附件；顺手收起工作台的整页拖放遮罩
function hideDropMask(el) {
  el.closest('.wbc')?.dispatchEvent(new DragEvent('dragleave', { bubbles: true }));
}
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');

// ---------- 欢迎与上传卡（还没传资料） ----------
function UploadPromptCard({ card }) {
  const { api } = useApp();
  const { pid } = useContext(WbCtx);
  const up = useMaterialUpload(pid, card.accept);
  const [over, setOver] = useState(false);
  const project = useProject();
  // 「连接业务系统」只在服务端给了这个动作、且证据源可用时出现；证据源本期只留接口（ADR-0056）
  const evidenceOn = evidenceInProject(project);
  const acts = (card.actions || ['upload', 'import_package']).filter((a) => a !== 'connect_system' || evidenceOn);
  const importPackage = () => pageCommand(api, pid, {
    command: '/ontology', text: '从已有知识包导入：列出可选的已发布知识包',
    label: '欢迎卡', done: '已发给助手',
  });
  return (
    <CC icon="upload" title={card.title || '先把资料给我'}>
      <div
        className="en-up" style={over ? { borderColor: 'var(--blue3)', background: '#F0F6FF' } : undefined}
        onClick={up.pick}
        onDragOver={(e) => { if (!hasFiles(e)) return; e.preventDefault(); e.stopPropagation(); if (!over) { setOver(true); hideDropMask(e.currentTarget); } }}
        onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setOver(false); }}
        onDrop={(e) => { if (!hasFiles(e)) return; e.preventDefault(); e.stopPropagation(); setOver(false); up.upload(e.dataTransfer.files); }}
      >
        <div className="ic">{up.busy ? <span className="spin" /> : <Icon name="upload" size={20} />}</div>
        <b style={{ color: 'var(--ink)' }}>{up.busy ? '正在上传…' : over ? '松开就上传到资料区' : '把资料拖到这里，或点击选择'}</b>
        <div className="xs mt4">{acceptLabel(card.accept, card.max_bytes)}</div>
      </div>
      {up.input}
      <div className="row mt12" style={{ gap: 8 }}>
        {acts.includes('upload') ? <Button icon="upload" kind="primary" size="sm" disabled={up.busy} onClick={up.pick}>上传资料</Button> : null}
        {acts.includes('connect_system') ? <Button icon="plug" size="sm" onClick={() => navigate(pagePath('sources', pid))}>连接业务系统</Button> : null}
        {acts.includes('import_package') ? <Button icon="pkg" size="sm" onClick={importPackage}>从已有知识包导入</Button> : null}
        <span className="sp" />
        {evidenceOn && card.sources_available ? <span className="xs muted">已登记 {card.sources_available} 个数据源可用</span> : null}
      </div>
    </CC>
  );
}

// ---------- 资料地图卡 ----------
function MiniMaterial({ m }) {
  const st = readState(m);
  const s = miniStatus(m);
  const ai = m.purpose === 'auto';
  let line;
  if (s.tone === 'amber') line = <span style={{ color: 'var(--amber)' }}><Icon name="alert" size={12} />{s.text}</span>;
  else if (s.spin) line = <span className="row g4" style={{ color: 'var(--blue2)' }}><span className="spin" />{s.text}</span>;
  else line = <span className="muted">{s.icon ? <Icon name={s.icon} size={12} /> : null}{s.text}</span>;
  return (
    <div className={cx('en-mt', st === 'reading' && 'run', m.blocked && 'blk')}>
      <div className="row g8"><MatIcon kind={m.kind} size={14} /><span className="nm ell grow" title={m.label || m.name}>{m.label || m.name}</span></div>
      <div className="row g8 mt8">
        <span className="xs ell" style={{ color: ai ? 'var(--indigo)' : 'var(--ink3)' }}>{ai ? <Icon name="sparkle" size={12} /> : null}{purposeLabel(m.purpose)}</span>
        <span className="sp" /><span className="xs num muted">{m.read_pct}%</span>
      </div>
      <div className="mt4"><Prog p={m.read_pct} thin tone={m.blocked ? 'amber' : undefined} /></div>
      <div className="sl2">{line}</div>
    </div>
  );
}

function MaterialMapCard({ card }) {
  const { pid } = useContext(WbCtx);
  const open = useOpenDialog();
  const up = useMaterialUpload(pid);
  const pb = card.problems || {};
  const nProblems = (pb.contradiction || 0) + (pb.missing || 0);
  const pbText = [pb.contradiction ? `资料矛盾 ${pb.contradiction}` : '', pb.missing ? `疑似缺失 ${pb.missing}` : ''].filter(Boolean).join('、');
  return (
    <CC
      icon="map" title={card.title || '资料地图'}
      chip={<Chip tone="line" sm>{card.total} 份 · 已读 {card.read_pct}%</Chip>}
      dbl onDoubleClick={() => open('material_map')}
      foot={(
        <>
          {card.unverifiable ? <Chip tone="amber" sm icon="shieldok" style={{ cursor: 'pointer' }} onClick={() => open('verification')}>{card.unverifiable} 条来源无法核验</Chip> : null}
          {nProblems ? <Chip tone="line" sm icon="alert">{nProblems} 个问题：{pbText}</Chip> : null}
          <span className="sp" />
          <Button size="xs" icon="maximize" onClick={() => open('material_map')}>打开资料地图</Button>
        </>
      )}
    >
      <div className="en-mini">
        {(card.items || []).map((m) => <MiniMaterial key={m.path} m={m} />)}
        <div className="en-mt add" onClick={up.pick} onDoubleClick={(e) => e.stopPropagation()}>
          {up.busy ? <span className="spin" /> : <Icon name="plus" size={14} />}{up.busy ? '正在上传…' : '追加资料'}
        </div>
      </div>
      {up.input}
    </CC>
  );
}

// ---------- 资料分析小结卡：点卡片去报告页，「资料地图」打开弹窗 ----------
function ResearchSummaryCard({ card }) {
  const { pid } = useContext(WbCtx);
  const open = useOpenDialog();
  const toReport = () => navigate(pagePath('reports', pid), card.report_id ? { report: card.report_id } : undefined);
  return (
    <div className="cc en-rs" onClick={toReport} title="打开资料分析报告">
      <div className="cc-h">
        <span className="ik"><Icon name="map" size={14} /></span>
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="row g8">
            <span className="ti">{card.title || '资料分析完成'}</span>
            {card.verified ? <Chip tone="teal" sm icon="check">已核验</Chip> : <Chip tone="amber" sm icon="alert">待核验</Chip>}
          </div>
          <div className="xs muted">{card.files} 份资料 · 覆盖 {card.coverage_pct}% · 核验 {card.verified_anchors} 条出处 · {card.problems_carried} 个问题已带进蓝图</div>
        </div>
        <span className="link sm row g4" onClick={(e) => { e.stopPropagation(); open('material_map'); }}>资料地图<Icon name="chev" size={12} /></span>
      </div>
    </div>
  );
}

// ---------- 建模蓝图卡 ----------
function OrderTag({ slice, index }) {
  const o = orderLabel({ ...slice, origin: index }, index);
  if (o.tone === 'blue') return <span className="chip blue sm">{o.text}</span>;
  if (o.tone === 'amber') return <span className="chip amber sm">{o.text}</span>;
  return <span className="xs faint">{o.text}</span>;
}

function BlueprintCard({ card }) {
  const open = useOpenDialog();
  const project = useProject();
  const slices = card.slices || [];
  const confirmed = card.state === 'confirmed';
  const terms = card.terms || {};
  const options = confirmOptions(card);
  const l4 = options.find((o) => o.mode === 'L4');
  const l2 = options.find((o) => o.mode === 'L2');
  // 要解决的问题只有一份：项目的建模目标（改了目标，卡片跟着变）；旧卡片退回卡上的文字
  const goal = project?.meta?.goal || card.goal_md;
  const asking = goalPending(card);
  const blocked = asking ? '先回答关于建模目标的问题' : undefined;
  return (
    <CC
      icon="layers" title={card.title || `建模蓝图 · ${card.version_label}`}
      chip={card.research_verified ? <Chip tone="teal" sm icon="shieldok">资料分析报告 · 已核验</Chip> : <Chip tone="amber" sm icon="alert">资料分析报告 · 待核验</Chip>}
      dbl onDoubleClick={() => open('blueprint')}
      foot={(
        <>
          <Button size="sm" icon="layers" onClick={() => open('blueprint')}>调整分段…</Button>
          <span className="sp" />
          {confirmed ? <span className="wx-ok"><Icon name="check" size={14} />蓝图已确认 · {card.version_label}</span> : (
            <>
              {l4 ? <Button size="sm" icon="rocket" disabled={asking} title={blocked} onClick={() => open({ kind: 'build', next: 'L4' })}>{l4.label || '确认，一路建完…'}</Button> : null}
              {l2 ? <Button size="sm" kind="primary" icon="hand" disabled={asking} title={blocked} onClick={() => open({ kind: 'build', next: 'L2' })}>{l2.label || '确认，一段一段来'}</Button> : null}
            </>
          )}
        </>
      )}
    >
      <div className="en-kv">
        <span>要解决的问题</span>
        <div>
          {goal}
          {asking ? <div className="xs mt4" style={{ color: 'var(--amber)' }}>{card.goal_question.title} <a className="link" onClick={() => open('blueprint')}>去回答</a></div> : null}
        </div>
      </div>
      <div className="mt12">
        <div className="en-bps hd"><span /><span>分段</span><span>中心流程</span><span>规模</span><span>依赖 · 顺序</span></div>
        {slices.map((s, i) => {
          const dep = depLabel(s, slices);
          return (
            <div className={cx('en-bps', s.excluded && 'ex')} key={s.id}>
              <span className="ix">{i + 1}</span>
              <b className="ell" style={{ fontWeight: 600 }}>{s.name}</b>
              <span className="muted ell">{s.core_flow}</span>
              <span className="xs num muted">对象 ≈{s.scale?.objects ?? '—'} · 规则 ≈{s.scale?.rules ?? '—'}</span>
              <span className="row g8">
                {dep === '无' ? <span className="xs faint">不依赖其他段</span> : <span className="xs muted">依赖 {dep}</span>}
                <OrderTag slice={s} index={i} />
              </span>
            </div>
          );
        })}
      </div>
      <div className="en-kv mt12">
        <span>术语与口径</span>
        <div>
          {terms.aligned ?? 0} 条已对齐
          {terms.ambiguous ? <>，<a className="link" onClick={() => open('blueprint')}>{terms.ambiguous} 条有歧义待你统一</a></> : null}
          {card.questions_pending ? <>；另有 <a className="link" onClick={() => open('blueprint')}>{card.questions_pending} 个问题待回答</a></> : null}
        </div>
      </div>
    </CC>
  );
}

// ---------- 「确认后怎么建」卡已取消（R3，后端不再发）；旧历史里的这张卡不画 ----------
const BuildChoiceCard = () => null;

export const MATERIAL_CARDS = {
  upload_prompt: UploadPromptCard,
  material_map: MaterialMapCard,
  research_summary: ResearchSummaryCard,
  blueprint: BlueprintCard,
  build_choice: BuildChoiceCard,
};
