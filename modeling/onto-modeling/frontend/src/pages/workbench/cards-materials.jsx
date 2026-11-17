// 资料分析与建模蓝图的对话卡片（契约 4.7：upload_prompt、material_map、research_summary、blueprint、build_choice）
// 设计稿 wb-materials、wb-blueprint；弹窗在 materials/ 里，由工作台统一挂（useOpenDialog）
import { useContext, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import {
  Button, Chip, Icon, Prog, cx,
} from '../../ui/index.jsx';
import { CC, WbCtx } from './parts.jsx';
import {
  MatIcon, pageCommand, useMaterialUpload, useOpenDialog,
} from './materials/parts.jsx';
import {
  acceptLabel, goalPending, miniStatus, purposeLabel, readState,
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
  const acts = card.actions || ['upload', 'import_package'];
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
        {acts.includes('import_package') ? <Button icon="pkg" size="sm" onClick={importPackage}>从已有知识包导入</Button> : null}
        <span className="sp" />
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

// ---------- 建模蓝图卡：要解决的问题、术语口径、待你回答（F8 起不分段、不用确认；双击或点链接打开蓝图去答） ----------
function BlueprintCard({ card }) {
  const open = useOpenDialog();
  const project = useProject();
  const terms = card.terms || {};
  // 要解决的问题只有一份：项目的建模目标（改了目标，卡片跟着变）；旧卡片退回卡上的文字
  const goal = project?.meta?.goal || card.goal_md;
  const asking = goalPending(card);
  // 目标问题在「要解决的问题」下面答，不再算进「待你回答」
  const others = Math.max(0, (card.questions_pending || 0) - (asking ? 1 : 0));
  const toDialog = () => open('blueprint');
  return (
    <CC icon="layers" title={card.title || `建模蓝图 · ${card.version_label}`} dbl onDoubleClick={toDialog}>
      <div className="en-kv">
        <span>要解决的问题</span>
        <div>
          {goal}
          {asking ? <div className="xs mt4" style={{ color: 'var(--amber)' }}>{card.goal_question.title} <a className="link" onClick={toDialog}>去回答</a></div> : null}
        </div>
      </div>
      <div className="en-kv mt12">
        <span>术语与口径</span>
        <div>
          {terms.aligned ?? 0} 条已对齐
          {terms.ambiguous ? <>，<a className="link" onClick={toDialog}>{terms.ambiguous} 条有歧义待你统一</a></> : null}
        </div>
      </div>
      {others ? (
        <div className="en-kv mt12">
          <span>待你回答</span>
          <div><a className="link" onClick={toDialog}>{others} 个问题</a></div>
        </div>
      ) : null}
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
