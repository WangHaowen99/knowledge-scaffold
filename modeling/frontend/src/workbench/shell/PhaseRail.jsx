import { useEffect, useRef, useState } from 'react';
import { store, actions } from '../lib/appStore.js';
import { useStore } from '../lib/store.js';
import * as F from '../lib/mock/fixtures.js';
import Icon from '../ui/Icon.jsx';

const selDemo = (s) => s.demo;
const selScene = (s) => s.scene;
const selWorkflow = (s) => s.workflow;
const selPhases = (s) => s.phases;
const selProgress = (s) => s.progress;
const selProgressError = (s) => s.progressError;
const selFiles = (s) => s.files;
const selMaterials = (s) => s.materials;
const selFocus = (s) => s.focus;

/** 每一步交付了什么：从已有的文件清单投影，不再问后端要一份计数。 */
export function stepCounts(phase, files = [], materials = []) {
  const count = (prefix, suffix) => files.filter((path) => (
    path.startsWith(prefix) && path.endsWith(suffix) && !path.endsWith('/index.md')
  )).length;
  const label = (value, unit) => (value ? `${value} ${unit}` : '');
  switch (phase) {
    case 'intake': return label(materials.length, '份资料');
    case 'process': return label(count('diagrams/', '.mmd'), '条流程');
    case 'knowledge': return label(
      ['objects/', 'operations/', 'rules/', 'metrics/'].reduce((sum, p) => sum + count(p, '.md'), 0),
      '项知识',
    );
    case 'validation': return label(count('scripts/', '.py'), '个脚本');
    default: return '';
  }
}

/**
 * 从当前步往后数，第一道没通过的闸。一条轨上只亮一道——
 * 同时亮六个红叉等于没亮，用户还是不知道下一个坎在哪。
 */
export function blockingGateIndex(steps, gateById, statusAt) {
  return steps.findIndex((step, index) => {
    const gate = gateById.get(step.gate);
    if (!gate || gate.pass) return false;
    if (index === 0) return false;
    const previous = statusAt(index - 1);
    return previous === 'passed' || previous.startsWith('current');
  });
}

/**
 * 阶段轨：步骤是节点，门禁是节点之间的闸。
 *
 * 它是投影不是驱动器——阶段来自 modeling-agent 的 state，闸来自确定性核心的
 * gate_status，产出计数来自文件清单。轨上没有确认按钮：阻断项只做导航，
 * 跳回对话流的确认卡或中栏 diff，因为没有 diff 就没有确认。
 */
export default function PhaseRail() {
  const demo = useStore(store, selDemo);
  const scene = useStore(store, selScene);
  const workflow = useStore(store, selWorkflow);
  const realPhases = useStore(store, selPhases);
  const progress = useStore(store, selProgress);
  const progressError = useStore(store, selProgressError);
  const files = useStore(store, selFiles);
  const materials = useStore(store, selMaterials);
  const focus = useStore(store, selFocus);
  const [open, setOpen] = useState(null);
  const railRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (event) => { if (event.key === 'Escape') setOpen(null); };
    const onClick = (event) => {
      if (!railRef.current?.contains(event.target)) setOpen(null);
    };
    window.addEventListener('keydown', onKey);
    window.addEventListener('pointerdown', onClick);
    return () => {
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('pointerdown', onClick);
    };
  }, [open]);

  const phases = demo ? F.railPhases : realPhases;
  const steps = phases.filter((item) => item.linear !== false);
  if (!steps.length) return null;

  const currentPhase = demo ? F.railScenePhase[scene] : workflow?.phase;
  const execution = demo ? F.railSceneExecution[scene] : workflow?.execution;
  const gateList = demo ? (F.railGates[scene] || F.railGates.default) : (progress?.gates || []);
  const gateById = new Map(gateList.map((gate) => [gate.id, gate]));
  const currentIndex = steps.findIndex((item) => item.phase === currentPhase);
  // 增量再入口时线性部分整体已走完，当前位置落在支线上。
  const statusAt = (index) => {
    if (currentIndex < 0) return 'passed';
    if (index < currentIndex) return 'passed';
    if (index > currentIndex) return 'future';
    return execution === 'waiting' ? 'current wait' : 'current';
  };
  const blockIndex = blockingGateIndex(steps, gateById, statusAt);
  const block = blockIndex >= 0 ? steps[blockIndex] : null;
  const blockGate = block ? gateById.get(block.gate) : null;
  const alarm = execution === 'waiting';
  const branched = currentPhase === 'increment';

  const toggle = (phase, kind) => {
    setOpen((current) => (current?.phase === phase && current.kind === kind ? null : { phase, kind }));
    if (!demo) void actions.loadProgress();
  };
  const openPanorama = () => {
    setOpen(null);
    store.set({ focus: { kind: 'progress', target: '建模进度' }, follow: false, pendingFocus: null });
    if (!demo) void actions.loadProgress();
  };

  return (
    <div ref={railRef}
      className={`wb-rail${branched ? ' branching' : ''}${alarm ? ' alarm' : ''}`}>
      <div className="wb-rail-main">
        <nav className="wb-rail-line" aria-label="建模进度">
          {steps.flatMap((step, index) => {
            const status = statusAt(index);
            const counts = stepCounts(step.phase, files, materials);
            const stepOpen = open?.phase === step.phase && open.kind === 'step';
            const marks = [];
            if (index > 0) {
              marks.push(<GateMark key={`gate-${step.phase}`} step={step} gate={gateById.get(step.gate)}
                blocking={index === blockIndex}
                opened={open?.phase === step.phase && open.kind === 'gate'}
                onOpen={() => toggle(step.phase, 'gate')} />);
            }
            marks.push(
              <button key={`step-${step.phase}`} type="button"
                className={`wb-rail-step ${status}${stepOpen ? ' on' : ''}`}
                aria-expanded={stepOpen}
                title={`第 ${index + 1} 步 · ${step.label}`}
                onClick={() => toggle(step.phase, 'step')}>
                {status === 'current' && execution === 'running' && <i className="wb-rail-run" />}
                <span className="num">{status === 'passed' ? '✓' : index + 1}</span>
                {step.label}
                {counts && <span className="cnt">{counts}</span>}
              </button>,
            );
            return marks;
          })}
        </nav>
        <div className="wb-rail-tail">
          {block && blockGate && (
            <button type="button" className="wb-rail-why" onClick={() => toggle(block.phase, 'gate')}>
              {alarm ? '待你处理 · ' : '卡在'}「{blockGate.label}」· {blockGate.blockers.length} 项
            </button>
          )}
          <button type="button"
            className={`wb-rail-pano${focus.kind === 'progress' ? ' on' : ''}`}
            onClick={openPanorama}>全景</button>
        </div>
      </div>

      {branched && (
        <div className="wb-rail-branch">
          <span className="hook" />
          <span className="tag">增量 · 新资料已入库</span>
          <span>正式基线保持不变，本轮只处理受影响内容</span>
        </div>
      )}

      {open && (
        <RailPanel steps={steps} gateById={gateById} open={open} statusAt={statusAt}
          files={files} materials={materials} demo={demo}
          gatesLoaded={demo || Boolean(progress)} progressError={progressError}
          onClose={() => setOpen(null)} />
      )}
    </div>
  );
}

function GateMark({ step, gate, blocking, opened, onOpen }) {
  const tone = (() => { if (!gate) return 'far'; return gate.pass ? 'ok' : (() => { if (blocking) return 'shut'; return 'far'; })(); })();
  return (
    <button type="button" className={`wb-rail-gate ${tone}${opened ? ' on' : ''}`}
      aria-expanded={opened}
      title={gate ? `闸 · ${gate.label}` : `进入「${step.label}」的门禁`}
      onClick={onOpen}>
      <i>{(() => { if (tone === 'ok') return ''; return tone === 'shut' ? <>⦸<b>{gate.blockers.length}</b></> : ''; })()}</i>
    </button>
  );
}

function RailPanel({
  steps, gateById, open, statusAt, files, materials, demo, gatesLoaded, progressError, onClose,
}) {
  const index = steps.findIndex((item) => item.phase === open.phase);
  if (index < 0) return null;
  const step = steps[index];
  const status = statusAt(index);
  return (
    <section className="wb-rail-panel" role="dialog" aria-label={step.label}>
      {open.kind === 'gate'
        ? <GateCard from={steps[index - 1]} to={step} gate={gateById.get(step.gate)}
          gatesLoaded={gatesLoaded} progressError={progressError} onClose={onClose} />
        : <StepCard index={index} step={step} status={status} exit={steps[index + 1]}
          gateById={gateById} files={files} materials={materials} demo={demo}
          gatesLoaded={gatesLoaded} progressError={progressError} onClose={onClose} />}
    </section>
  );
}

function GateCard({ from, to, gate, gatesLoaded, progressError, onClose }) {
  return (
    <>
      <header>
        <b>闸 · {gate?.label || `进入「${to.label}」`}</b>
        <span className="spacer" />
        <CloseButton onClose={onClose} />
      </header>
      <p className="what">
        {from.label} <span className="arrow">→</span> {to.label}。
        门禁是纯谓词，只回答能不能过，不推进任何东西。
      </p>
      <GateBox gate={gate} gatesLoaded={gatesLoaded} progressError={progressError} />
    </>
  );
}

function StepCard({
  index, step, status, exit, gateById, files, materials, demo, gatesLoaded, progressError, onClose,
}) {
  const counts = stepCounts(step.phase, files, materials);
  const exitGate = exit ? gateById.get(exit.gate) : null;
  const statusText = (() => { if (status === 'passed') return '已通过'; return status.startsWith('current') ? ((() => { if (status.endsWith('wait')) return '进行中 · 等你'; return '进行中'; })())
      : '未开始'; })();
  return (
    <>
      <header>
        <b>{index + 1}. {step.label}</b>
        <span className={`st ${status.split(' ')[0]}`}>{statusText}</span>
        <span className="spacer" />
        <CloseButton onClose={onClose} />
      </header>
      {counts && <p className="what">已产出 {counts}</p>}
      {exit ? (
        <>
          <h5>出这一步的闸 · {exitGate?.label || `进入「${exit.label}」`}</h5>
          <GateBox gate={exitGate} gatesLoaded={gatesLoaded} progressError={progressError} />
        </>
      ) : (
        <p className="note">最后一步，没有下一道闸。发布后有新资料时，会进入独立的增量轮次。</p>
      )}
      {!demo && <p className="note">
        阻断项上的按钮只做导航，跳回对话流或中栏 diff；轨上不能就地确认。
      </p>}
    </>
  );
}

function GateBox({ gate, gatesLoaded, progressError }) {
  if (!gatesLoaded) return <p className="note">正在读取门禁结果…</p>;
  if (!gate) {
    return <p className="note">{progressError || '这道闸的结果还没送到，稍后再看。'}</p>;
  }
  const failed = gate.blockers.length;
  return (
    <div className={`wb-rail-gatebox ${gate.pass ? 'ok' : 'shut'}`}>
      <div className="gh">
        {gate.pass ? '✓' : '⦸'} <b>{gate.label}</b>
        <span className="spacer" />
        {gate.pass ? '已通过' : `${failed} 项未满足`}
      </div>
      {gate.pass
        ? <div className="pred p"><span className="m">✓</span><span className="txt">条件全部满足</span></div>
        : gate.blockers.map((text) => (
          <div className="pred f" key={text}>
            <span className="m">✕</span>
            <span className="txt">{text}</span>
            <button type="button" className="go"
              onClick={() => void actions.send(`请说明“${text}”还差什么，并把需要我确认的内容放到中栏。`)}>
              去处理 →
            </button>
          </div>
        ))}
      {(gate.warnings || []).map((text) => (
        <div className="pred w" key={text}><span className="m">!</span><span className="txt">{text}</span></div>
      ))}
    </div>
  );
}

function CloseButton({ onClose }) {
  return (
    <button type="button" className="btn btn-ghost btn-sm" aria-label="关闭" onClick={onClose}>
      <Icon name="x" size={12} />
    </button>
  );
}
