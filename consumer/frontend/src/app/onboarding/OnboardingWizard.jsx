// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
/* eslint-disable react-refresh/only-export-components -- colocated focus helpers are direct unit-test seams. */
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import {
  BrainCircuit,
  FolderKanban,
  MessageSquarePlus,
  PackageOpen,
  PlugZap,
  Settings2,
  Sparkles,
} from 'lucide-react';
import { useAuthContext } from '../../features/auth/useAuthContext';
import { clampSpotlightRect, getOnboardingTipStyle } from './layout';
import { markWizardSeen } from './wizardState';
import './onboarding.css';

// 蒙版聚光灯式操作向导：每步跳转到真实页面并高亮 [data-wiz=...] 元素。
// 每次新登录弹出（sessionStorage 标记），可随时跳过，顶栏 ❓ 重开。
const WIZARD_FOCUSABLE_SELECTOR = [
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  'a[href]',
  '[tabindex]:not([tabindex="-1"])',
].join(', ');

export function resolveHasNoWorkspace(
  explicitValue,
  { workspacesLoaded, workspacesLoading, workspacesError, workspaces },
) {
  if (typeof explicitValue === 'boolean') {
    return explicitValue;
  }
  return Boolean(
    workspacesLoaded &&
      !workspacesLoading &&
      !workspacesError &&
      Array.isArray(workspaces) &&
      workspaces.length === 0,
  );
}

export function getWizardFocusableElements(
  card,
  resolveStyle = (element) => window.getComputedStyle(element),
) {
  return Array.from(
    card?.querySelectorAll(WIZARD_FOCUSABLE_SELECTOR) || [],
  ).filter((element) => {
    const style = resolveStyle(element);
    return style.display !== 'none' && style.visibility !== 'hidden';
  });
}

export function trapWizardTab(event, card, activeElement, resolveStyle) {
  if (event.key !== 'Tab' || !card) {
    return false;
  }
  event.stopPropagation();
  const items = getWizardFocusableElements(card, resolveStyle);
  if (!items.length) {
    event.preventDefault();
    card.focus();
    return true;
  }

  const [first, last] = [items[0], items.at(-1)];
  let target = null;
  if (!card.contains(activeElement)) {
    target = event.shiftKey ? last : first;
  } else if (event.shiftKey && activeElement === first) {
    target = last;
  } else if (!event.shiftKey && activeElement === last) {
    target = first;
  } else {
    return false;
  }

  if (!target) {
    return false;
  }
  event.preventDefault();
  target.focus();
  return true;
}

export function activateWizardFocus(card, onDismiss, options = {}) {
  if (!card) {
    return () => {};
  }
  const documentRef = options.documentRef || document;
  const resolveStyle =
    options.resolveStyle || ((element) => window.getComputedStyle(element));
  const previouslyFocused = documentRef.activeElement;
  const [initialTarget] = getWizardFocusableElements(card, resolveStyle);
  (initialTarget || card).focus();

  const onKeyDown = (event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      onDismiss();
      return;
    }
    trapWizardTab(event, card, documentRef.activeElement, resolveStyle);
  };
  documentRef.addEventListener('keydown', onKeyDown);

  return () => {
    documentRef.removeEventListener('keydown', onKeyDown);
    const canRestoreFocus =
      previouslyFocused &&
      previouslyFocused !== documentRef.body &&
      previouslyFocused.isConnected !== false &&
      typeof previouslyFocused.focus === 'function';
    if (canRestoreFocus) {
      previouslyFocused.focus();
    }
  };
}

export function dismissWizardAsSeen(onClose) {
  markWizardSeen();
  onClose();
}

function adminWithoutWorkspaceSteps() {
  return [
    {
      ic: Sparkles,
      t: '欢迎使用知识工程工具',
      d: (
        <>
            当前平台尚无可用工作空间。先完成<b>创建工作空间</b>
            ，再继续模型、知识包和数据源初始化。
        </>
      ),
    },
    {
      ic: FolderKanban,
      t: '第 1 步 · 创建工作空间',
      d: (
        <>
            创建首个工作空间并填写名称与 ID。创建成功后，顶栏会自动选中该空间。
        </>
      ),
      nav: { surface: 'config', scope: 'global', sec: 'wsmgmt' },
      target: 'cfg-wsmgmt',
    },
    {
      ic: BrainCircuit,
      t: '第 2 步 · 配置模型',
      d: (
        <>
            在<b>全局模型池</b>添加模型 profile
            并设置默认模型。密钥只保存在全局层。
        </>
      ),
      nav: { surface: 'config', scope: 'global', sec: 'llm' },
      target: 'cfg-llm',
    },
    {
      ic: PackageOpen,
      t: '第 3 步 · 导入知识包',
      d: <>导入 .zip 知识包，在工作台查看业务对象、操作和映射。</>,
      nav: { surface: 'kb' },
      target: 'kb-import',
    },
    {
      ic: PlugZap,
      t: '第 4 步 · 配置数据源',
      d: (
        <>
            在知识包的<b>数据源</b>页签填写 Provider
            与连接参数；配置只属于当前知识包。
        </>
      ),
      nav: { surface: 'kb', tab: 'data' },
      target: 'kb-data-source',
    },
    {
      ic: MessageSquarePlus,
      t: '一切就绪',
      d: <>从高亮入口新建对话，开始验证知识与数据能力。</>,
      target: 'nav-chat',
    },
  ];
}

function adminSteps() {
  return [
    {
      ic: Sparkles,
      t: '欢迎使用知识工程工具',
      d: (
        <>
              接下来用几步蒙版引导带你完成平台初始化：
          <b>配置模型 → 导入知识包 → 配置数据源</b>
              。可随时跳过，之后从顶栏向导按钮重新打开。
        </>
      ),
    },
    {
      ic: BrainCircuit,
      t: '第 1 步 · 配置模型',
      d: (
        <>
              高亮处是<b>全局模型池</b>：添加模型
              profile、设置全局默认。密钥只保存在全局层，各工作空间通过“模型选用”引用。
        </>
      ),
      nav: { surface: 'config', scope: 'global', sec: 'llm' },
      target: 'cfg-llm',
    },
    {
      ic: PackageOpen,
      t: '第 2 步 · 导入知识包',
      d: (
        <>
              点高亮的按钮导入 .zip 知识包，左侧树会出现包内的
          <b>业务对象 / 操作</b>，右侧可逐个查看内容。
        </>
      ),
      nav: { surface: 'kb' },
      target: 'kb-import',
    },
    {
      ic: PlugZap,
      t: '第 3 步 · 配置数据源',
      d: (
        <>
              在知识包的<b>数据源</b>页签填写 Provider
              与连接参数；配置只属于当前知识包，保存时覆盖原配置。
        </>
      ),
      nav: { surface: 'kb', tab: 'data' },
      target: 'kb-data-source',
    },
    {
      ic: MessageSquarePlus,
      t: '一切就绪',
      d: (
        <>
              从高亮的入口<b>新建对话</b>，试试知识包的典型问题。
        </>
      ),
      target: 'nav-chat',
    },
  ];
}

function memberSteps() {
  return [
    {
      ic: Sparkles,
      t: '欢迎使用知识工程工具',
      d: (
        <>
              用几步蒙版引导快速了解三个区域：
          <b>新建对话、知识包工作台、配置中心</b>
              （对你只读开放）。可随时跳过。
        </>
      ),
    },
    {
      ic: PackageOpen,
      t: '知识包工作台',
      d: (
        <>
              高亮入口进入<b>知识包工作台</b>
              ：查看已导入知识包的业务对象、操作和典型问题。
        </>
      ),
      nav: { surface: 'kb' },
      target: 'nav-kb',
    },
    {
      ic: Settings2,
      t: '个人设置',
      d: (
        <>
              配置中心对你只读；但高亮处的<b>个人偏好</b>
              （默认工作空间、回复语言）可以自己修改。
        </>
      ),
      nav: { surface: 'config', scope: 'personal', sec: 'prefs' },
      target: 'cfg-prefs',
    },
    {
      ic: MessageSquarePlus,
      t: '开始对话',
      d: <>选好工作空间后从高亮入口直接提问，例如知识包里的典型问题。</>,
      target: 'nav-chat',
    },
  ];
}

function stepsFor(isAdmin, hasNoWorkspace) {
  if (isAdmin && hasNoWorkspace) {
    return adminWithoutWorkspaceSteps();
  }
  return isAdmin ? adminSteps() : memberSteps();
}

// eslint-disable-next-line max-lines-per-function -- Preserve established boundary.
export default function OnboardingWizard({
  onNavigate,
  onOpenNavigation,
  onClose,
  hasNoWorkspace: explicitHasNoWorkspace,
}) {
  const {
    isAdmin,
    workspaces,
    workspacesLoaded,
    workspacesLoading,
    workspacesError,
  } = useAuthContext();
  const hasNoWorkspace = resolveHasNoWorkspace(explicitHasNoWorkspace, {
    workspaces,
    workspacesLoaded,
    workspacesLoading,
    workspacesError,
  });
  const steps = stepsFor(isAdmin, hasNoWorkspace);
  const [step, setStep] = useState(0);
  const [rect, setRect] = useState(null);
  const [cardHeight, setCardHeight] = useState(0);
  const cardRef = useRef(null);
  const closeRef = useRef(onClose);
  const titleId = useId();
  const i = Math.min(step, steps.length - 1);
  const s = steps[i];
  const StepIcon = s.ic;

  const dismissWizard = () => {
    dismissWizardAsSeen(closeRef.current);
  };

  useLayoutEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);

  const go = (nextIdx) => {
    const clamped = Math.max(0, nextIdx);
    if (clamped >= steps.length) {
      dismissWizard();
      return;
    }
    const target = steps[clamped];
    if (target.nav) {
      onNavigate(target.nav);
    }
    setStep(clamped);
  };

  useLayoutEffect(() => {
    // 等目标页面渲染完成后测量（全部 setState 收进异步回调）
    const mobileNavTarget =
      window.matchMedia('(max-width: 900px)').matches &&
      String(s.target || '').startsWith('nav-');
    if (mobileNavTarget) {
      onOpenNavigation();
    }
    const timer = setTimeout(
      () => {
        if (!s.target) {
          setRect(null);
          return;
        }
        const el = document.querySelector(`[data-wiz="${s.target}"]`);
        if (!el) {
          setRect(null);
          return;
        }
        el.scrollIntoView({ block: 'center', behavior: 'auto' });
        const r = el.getBoundingClientRect();
        setRect(
          clampSpotlightRect(r, {
            width: window.innerWidth,
            height: window.innerHeight,
          }),
        );
      },
      mobileNavTarget ? 220 : 80,
    );
    return () => clearTimeout(timer);
  }, [i, s.target, onOpenNavigation]);

  useEffect(() => {
    const onKey = (e) => {
      if (
        ['INPUT', 'TEXTAREA', 'SELECT'].includes(
          document.activeElement?.tagName,
        )
      ) {
        return;
      }
      if (e.key === 'ArrowLeft') {
        go(i - 1);
      }
      if (e.key === 'ArrowRight') {
        go(i + 1);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  useLayoutEffect(
    () =>
      activateWizardFocus(cardRef.current, () => {
        dismissWizardAsSeen(closeRef.current);
      }),
    [],
  );

  useLayoutEffect(() => {
    const element = cardRef.current;
    if (!element) {
      return undefined;
    }
    const observer = new ResizeObserver(() => {
      const nextHeight = Math.ceil(element.getBoundingClientRect().height || 0);
      setCardHeight((current) =>
        current === nextHeight ? current : nextHeight,
      );
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [i, rect]);

  const card = (
    <div
      key="wizard-card"
      ref={cardRef}
      className="mwr-wizc"
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      tabIndex={-1}
      style={
        rect ?
          getOnboardingTipStyle(
            rect,
            { width: window.innerWidth, height: window.innerHeight },
            cardHeight,
          ) :
          undefined
      }
    >
      <div className="wzc-head">
        <span className="wz-step">
          操作向导 · {i + 1} / {steps.length}
        </span>
        <button className="wz-skip" onClick={dismissWizard}>
          跳过向导
        </button>
      </div>
      <div id={titleId} className="wzc-t">
        <span className="wzc-icon">
          <StepIcon size={18} />
        </span>
        {s.t}
      </div>
      <div className="wzc-d">{s.d}</div>
      <div className="wz-dots">
        {steps.map((_, k) => (
          <span key={k} className={k === i ? 'on' : ''} />
        ))}
      </div>
      <div className="wzc-foot">
        <button className="btn" disabled={i === 0} onClick={() => go(i - 1)}>
          上一步
        </button>
        <button className="btn btn-p" onClick={() => go(i + 1)}>
          {i === steps.length - 1 ? '完成' : '下一步'}
        </button>
      </div>
    </div>
  );

  if (!rect) {
    return <div className="mwr-wiz-ov">{card}</div>;
  }
  return (
    <div className="mwr-wiz-mask">
      {card}
      <div
        key="wizard-hole"
        className="mwr-wiz-hole"
        style={{ left: rect.x, top: rect.y, width: rect.w, height: rect.h }}
      />
    </div>
  );
}
