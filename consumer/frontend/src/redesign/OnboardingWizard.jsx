import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { BrainCircuit, Link2, MessageSquarePlus, PackageOpen, PlugZap, Settings2, Sparkles } from 'lucide-react';
import { clampSpotlightRect, getOnboardingTipStyle } from '../utils/onboardingLayout';
import { useAuthContext } from './useAuthContext';
import { markWizardSeen } from './wizardState';

// 蒙版聚光灯式操作向导：每步跳转到真实页面并高亮 [data-wiz=...] 元素。
// 每次新登录弹出（sessionStorage 标记），可随时跳过，顶栏 ❓ 重开。

function stepsFor(isAdmin) {
  return isAdmin ? [
    { ic: Sparkles, t: '欢迎使用知识工程工具', d: <>接下来用几步蒙版引导带你完成平台初始化：<b>配置模型 → 连接数据 → 导入知识包 → 绑定数据</b>。可随时跳过，之后从顶栏向导按钮重新打开。</> },
    { ic: BrainCircuit, t: '第 1 步 · 配置模型', d: <>高亮处是<b>全局模型池</b>：添加模型 profile、设置全局默认。密钥只保存在全局层，各工作空间通过“模型选用”引用。</>, nav: { surface: 'config', scope: 'global', sec: 'llm' }, target: 'cfg-llm' },
    { ic: PlugZap, t: '第 2 步 · 连接数据', d: <>高亮处是当前工作空间的<b>数据连接</b>：新建业务数据库连接，知识包将通过它查询真实业务数据。</>, nav: { surface: 'config', scope: 'workspace', sec: 'conn' }, target: 'cfg-conn' },
    { ic: PackageOpen, t: '第 3 步 · 导入知识包', d: <>点高亮的按钮导入 .zip 知识包，左侧树会出现包内的<b>业务对象 / 操作</b>，右侧可逐个查看内容。</>, nav: { surface: 'kb' }, target: 'kb-import' },
    { ic: Link2, t: '第 4 步 · 知识与数据绑定', d: <>高亮处把<b>知识包映射到数据连接</b>——绑定后，知识包中的业务对象才能查询真实业务数据。</>, nav: { surface: 'config', scope: 'workspace', sec: 'bind' }, target: 'cfg-bind' },
    { ic: MessageSquarePlus, t: '一切就绪', d: <>从高亮的入口<b>新建对话</b>，试试知识包的典型问题。</>, target: 'nav-chat' },
  ] : [
    { ic: Sparkles, t: '欢迎使用知识工程工具', d: <>用几步蒙版引导快速了解三个区域：<b>新建对话、知识包工作台、配置中心</b>（对你只读开放）。可随时跳过。</> },
    { ic: PackageOpen, t: '知识包工作台', d: <>高亮入口进入<b>知识包工作台</b>：查看已导入知识包的业务对象、操作和典型问题。</>, nav: { surface: 'kb' }, target: 'nav-kb' },
    { ic: Settings2, t: '个人设置', d: <>配置中心对你只读；但高亮处的<b>个人偏好</b>（默认工作空间、回复语言）可以自己修改。</>, nav: { surface: 'config', scope: 'personal', sec: 'prefs' }, target: 'cfg-prefs' },
    { ic: MessageSquarePlus, t: '开始对话', d: <>选好工作空间后从高亮入口直接提问，例如知识包里的典型问题。</>, target: 'nav-chat' },
  ];
}

export default function OnboardingWizard({ onNavigate, onOpenNavigation, onClose }) {
  const { isAdmin } = useAuthContext();
  const steps = stepsFor(isAdmin);
  const [step, setStep] = useState(0);
  const [rect, setRect] = useState(null);
  const [cardHeight, setCardHeight] = useState(0);
  const cardRef = useRef(null);
  const i = Math.min(step, steps.length - 1);
  const s = steps[i];
  const StepIcon = s.ic;

  const go = (nextIdx) => {
    const clamped = Math.max(0, nextIdx);
    if (clamped >= steps.length) { markWizardSeen(); onClose(); return; }
    const target = steps[clamped];
    if (target.nav) onNavigate(target.nav);
    setStep(clamped);
  };

  useLayoutEffect(() => {
    // 等目标页面渲染完成后测量（全部 setState 收进异步回调）
    const mobileNavTarget = window.matchMedia('(max-width: 900px)').matches
      && String(s.target || '').startsWith('nav-');
    if (mobileNavTarget) onOpenNavigation();
    const timer = setTimeout(() => {
      if (!s.target) { setRect(null); return; }
      const el = document.querySelector(`[data-wiz="${s.target}"]`);
      if (!el) { setRect(null); return; }
      el.scrollIntoView({ block: 'center', behavior: 'auto' });
      const r = el.getBoundingClientRect();
      setRect(clampSpotlightRect(r, { width: window.innerWidth, height: window.innerHeight }));
    }, mobileNavTarget ? 220 : 80);
    return () => clearTimeout(timer);
  }, [i, s.target, onOpenNavigation]);

  useEffect(() => {
    const onKey = (e) => {
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) return;
      if (e.key === 'ArrowLeft') go(i - 1);
      if (e.key === 'ArrowRight') go(i + 1);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  useLayoutEffect(() => {
    const element = cardRef.current;
    if (!element) return undefined;
    const observer = new ResizeObserver(() => {
      const nextHeight = Math.ceil(element.getBoundingClientRect().height || 0);
      setCardHeight((current) => current === nextHeight ? current : nextHeight);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [i, rect]);

  const skip = () => { markWizardSeen(); onClose(); };

  const card = (
    <div ref={cardRef} className="mwr-wizc" style={rect ? getOnboardingTipStyle(
      rect,
      { width: window.innerWidth, height: window.innerHeight },
      cardHeight,
    ) : undefined}>
      <div className="wzc-head">
        <span className="wz-step">操作向导 · {i + 1} / {steps.length}</span>
        <button className="wz-skip" onClick={skip}>跳过向导</button>
      </div>
      <div className="wzc-t"><span className="wzc-icon"><StepIcon size={18} /></span>{s.t}</div>
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
