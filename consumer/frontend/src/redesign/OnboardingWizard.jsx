import { useEffect, useLayoutEffect, useState } from 'react';
import { useAuthContext } from './useAuthContext';
import { markWizardSeen } from './wizardState';

// 蒙版聚光灯式操作向导：每步跳转到真实页面并高亮 [data-wiz=...] 元素。
// 每次新登录弹出（sessionStorage 标记），可随时跳过，顶栏 ❓ 重开。

function stepsFor(isAdmin) {
  return isAdmin ? [
    { ic: '👋', t: '欢迎使用 meta-wise', d: <>接下来用几步蒙版引导带你完成平台初始化：<b>配置模型 → 连接数据 → 导入知识包 → 绑定数据</b>。可随时跳过，之后从顶栏 ❓ 重新打开。</> },
    { ic: '🧠', t: '第 1 步 · 配置模型', d: <>高亮处是<b>全局模型池</b>：添加模型 profile、设置全局默认。密钥只保存在全局层，各工作空间通过“模型选用”引用。</>, nav: { surface: 'config', scope: 'global', sec: 'llm' }, target: 'cfg-llm' },
    { ic: '🔌', t: '第 2 步 · 连接数据', d: <>高亮处是当前工作空间的<b>数据连接</b>：新建业务数据库连接，知识包将通过它查询真实业务数据。</>, nav: { surface: 'config', scope: 'workspace', sec: 'conn' }, target: 'cfg-conn' },
    { ic: '📦', t: '第 3 步 · 导入知识包', d: <>点高亮的按钮导入 .zip 知识包，左侧树会出现包内的<b>业务对象 / 操作</b>，右侧可逐个查看内容。</>, nav: { surface: 'kb' }, target: 'kb-import' },
    { ic: '🔗', t: '第 4 步 · 知识与数据绑定', d: <>高亮处把<b>知识包映射到数据连接</b>——绑定后，知识包中的业务对象才能查询真实业务数据。</>, nav: { surface: 'config', scope: 'workspace', sec: 'bind' }, target: 'cfg-bind' },
    { ic: '💬', t: '一切就绪', d: <>从高亮的入口进入<b>知识应用</b>，试试知识包的典型问题。</>, target: 'nav-chat' },
  ] : [
    { ic: '👋', t: '欢迎使用 meta-wise', d: <>用几步蒙版引导快速了解三个区域：<b>知识应用、知识包工作台、配置中心</b>（对你只读开放）。可随时跳过。</> },
    { ic: '📦', t: '知识包工作台', d: <>高亮入口进入<b>知识包工作台</b>：查看已导入知识包的业务对象、操作和典型问题。</>, nav: { surface: 'kb' }, target: 'nav-kb' },
    { ic: '⚙️', t: '个人设置', d: <>配置中心对你只读；但高亮处的<b>个人偏好</b>（默认工作空间、回复语言）可以自己修改。</>, nav: { surface: 'config', scope: 'personal', sec: 'prefs' }, target: 'cfg-prefs' },
    { ic: '💬', t: '开始对话', d: <>选好工作空间后从高亮入口直接提问，例如知识包里的典型问题。</>, target: 'nav-chat' },
  ];
}

export default function OnboardingWizard({ onNavigate, onClose }) {
  const { isAdmin } = useAuthContext();
  const steps = stepsFor(isAdmin);
  const [step, setStep] = useState(0);
  const [rect, setRect] = useState(null);
  const i = Math.min(step, steps.length - 1);
  const s = steps[i];

  const go = (nextIdx) => {
    const clamped = Math.max(0, nextIdx);
    if (clamped >= steps.length) { markWizardSeen(); onClose(); return; }
    const target = steps[clamped];
    if (target.nav) onNavigate(target.nav);
    setStep(clamped);
  };

  useLayoutEffect(() => {
    // 等目标页面渲染完成后测量（全部 setState 收进异步回调）
    const timer = setTimeout(() => {
      if (!s.target) { setRect(null); return; }
      const el = document.querySelector(`[data-wiz="${s.target}"]`);
      if (!el) { setRect(null); return; }
      el.scrollIntoView({ block: 'center', behavior: 'auto' });
      const r = el.getBoundingClientRect();
      const pad = 8;
      setRect({ x: r.left - pad, y: r.top - pad, w: r.width + pad * 2, h: r.height + pad * 2 });
    }, 80);
    return () => clearTimeout(timer);
  }, [i, s.target]);

  useEffect(() => {
    const onKey = (e) => {
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) return;
      if (e.key === 'ArrowLeft') go(i - 1);
      if (e.key === 'ArrowRight') go(i + 1);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  const skip = () => { markWizardSeen(); onClose(); };

  const card = (
    <div className="mwr-wizc" style={rect ? tipStyle(rect) : { width: 460 }}>
      <div className="wzc-head">
        <span className="wz-step">操作向导 · {i + 1} / {steps.length}</span>
        <button className="wz-skip" onClick={skip}>跳过向导</button>
      </div>
      <div className="wzc-t">{s.ic} {s.t}</div>
      <div className="wzc-d">{s.d}</div>
      <div className="wz-dots">{steps.map((_, k) => <span key={k} className={k === i ? 'on' : ''} />)}</div>
      <div className="wzc-foot">
        <button className="btn" disabled={i === 0} onClick={() => go(i - 1)}>上一步</button>
        <button className="btn btn-p" onClick={() => go(i + 1)}>{i === steps.length - 1 ? '完成' : '下一步'}</button>
      </div>
    </div>
  );

  if (!rect) return <div className="mwr-wiz-ov">{card}</div>;
  return (
    <div className="mwr-wiz-mask">
      <div className="mwr-wiz-hole" style={{ left: rect.x, top: rect.y, width: rect.w, height: rect.h }} />
      {card}
    </div>
  );
}

function tipStyle(rect) {
  const tipW = 420;
  const tipH = 230;
  const gap = 14;
  const below = rect.y + rect.h + gap;
  const top = below + tipH < window.innerHeight - 12 ? below : Math.max(12, rect.y - tipH - gap);
  const left = Math.min(Math.max(12, rect.x), window.innerWidth - tipW - 12);
  return { position: 'fixed', left, top, width: tipW };
}
