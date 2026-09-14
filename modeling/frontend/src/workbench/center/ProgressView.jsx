import { useEffect } from 'react';
import { store, actions } from '../lib/appStore.js';
import { useStore } from '../lib/store.js';
import * as F from '../lib/mock/fixtures.js';
import { stepCounts } from '../shell/PhaseRail.jsx';

const selDemo = (s) => s.demo;
const selScene = (s) => s.scene;
const selWorkflow = (s) => s.workflow;
const selPhases = (s) => s.phases;
const selProgress = (s) => s.progress;
const selProgressError = (s) => s.progressError;
const selFiles = (s) => s.files;
const selMaterials = (s) => s.materials;

/**
 * 进度全景：阶段轨的第三级。
 *
 * 每一步交付了什么、凭哪条真实事实算通过、还差什么，纵向摊开一页。
 * 这里画的是门禁顺序，不是助手实际走过的路——返工和回头在过程抽屉里看。
 */
export default function ProgressView() {
  const demo = useStore(store, selDemo);
  const scene = useStore(store, selScene);
  const workflow = useStore(store, selWorkflow);
  const realPhases = useStore(store, selPhases);
  const progress = useStore(store, selProgress);
  const progressError = useStore(store, selProgressError);
  const files = useStore(store, selFiles);
  const materials = useStore(store, selMaterials);

  useEffect(() => { if (!demo) void actions.loadProgress(); }, [demo]);

  const phases = demo ? F.railPhases : realPhases;
  const steps = phases.filter((item) => item.linear !== false);
  const currentPhase = demo ? F.railScenePhase[scene] : workflow?.phase;
  const execution = demo ? F.railSceneExecution[scene] : workflow?.execution;
  const gateById = new Map((demo
    ? (F.railGates[scene] || F.railGates.default)
    : (progress?.gates || [])).map((g) => [g.id, g]));
  const currentIndex = steps.findIndex((item) => item.phase === currentPhase);

  if (!steps.length) {
    return <p className="wb-progress-empty">建模助手还没有报告阶段，等它跑起来这一页就有内容了。</p>;
  }

  return (
    <div className="wb-progress">
      {steps.map((step, index) => {
        const status = (() => { if (currentIndex < 0 || index < currentIndex) return 'passed'; return index > currentIndex ? 'future'
            : 'current'; })();
        const gate = gateById.get(step.gate);
        const counts = stepCounts(step.phase, files, materials);
        const next = steps[index + 1];
        const nextGate = next ? gateById.get(next.gate) : null;
        return (
          <article className={`wb-progress-row ${status}`} key={step.phase}>
            <div className="gut">
              <span className="bead" />
              {next && <span className={`wire${(() => { if (nextGate) return ((() => { if (nextGate.pass) return ' ok'; return ' shut'; })()); return ''; })()}`} />}
            </div>
            <div className="body">
              <h4>
                {index + 1}. {step.label}
                <span className="meta">
                  {(() => { if (status === 'passed') return '已通过'; return status === 'current' ? ((() => { if (execution === 'waiting') return '进行中 · 等你'; return '进行中'; })())
                      : '未开始'; })()}
                  {counts ? ` · ${counts}` : ''}
                </span>
              </h4>
              <GateLine gate={gate} first={index === 0} />
            </div>
          </article>
        );
      })}
      <p className="wb-progress-foot">
        {progressError || '门禁顺序是硬的，通往门禁的路径是软的——助手实际怎么走不在这里画。'}
      </p>
    </div>
  );
}

function GateLine({ gate, first }) {
  if (first) return <p className="wb-progress-gate">起点 · 没有入口闸</p>;
  if (!gate) return <p className="wb-progress-gate">门禁结果还没送到</p>;
  return (
    <p className={`wb-progress-gate ${gate.pass ? 'ok' : 'shut'}`}>
      {gate.pass ? '✓' : '⦸'} 闸 · {gate.label}
      {gate.pass ? ' · 已通过' : ` · ${gate.blockers.join(' · ')}`}
    </p>
  );
}
