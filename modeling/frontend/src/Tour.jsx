import { useEffect, useState } from 'react';

// 蒙版引导（多场景）：高亮目标元素（box-shadow 蒙版）+ 步骤气泡。
// 每个场景（项目列表 / 建模工作台）首次进入自动播放一次，顶栏「❓ 引导」可重看当前页引导。
// 步骤可带 enter 钩子（如打开配置中心并切到指定 tab）；目标元素未就绪时短暂轮询，仍找不到则跳过该步。

// 配置中心三个 tab 的逐项讲解：enter 钩子经 App 打开弹窗并切 tab（见 App.jsx 的 mw-settings-open 监听）
const openSettings = (tab) => () =>
  window.dispatchEvent(new CustomEvent('mw-settings-open', { detail: { tab } }));

export const CONFIG_STEPS = [
  {
    sel: '[data-tour="cfg-llm"]', enter: openSettings('llm'),
    title: '配置中心 · 大模型',
    body: '填写模型地址、名称和密钥，再点「测试」。演示模式可将地址填为 faux。',
  },
  {
    sel: '[data-tour="cfg-consumer"]', enter: openSettings('consumer'),
    title: '配置中心 · consumer 服务',
    body: '填写评测服务地址，再测试连接。',
  },
  {
    sel: '[data-tour="cfg-eval"]', enter: openSettings('eval'),
    title: '配置中心 · 评测数据',
    body: '填写评测数据库和数据源，保存后用于评测。',
  },
];

export const PROJECT_STEPS = [
  {
    sel: '[data-tour="pj-new"]',
    title: '新建建模项目',
    body: '欢迎使用知识包建模工具！从这里创建你的第一个建模项目：填写项目名、业务领域和场景背景（背景写得越具体，后面的分析和访谈越准）。',
  },
  {
    sel: '[data-tour="pj-grid"]',
    title: '项目卡片',
    body: '每个建模项目一张卡片：进度条显示建模走到了第几步，下方标签是当前阶段和业务领域。点击卡片随时回到工作台继续。',
  },
  {
    sel: '[data-tour="config"]',
    title: '配置中心',
    body: '开始建模前，先在右上角的配置中心完成三项配置。接下来逐个看一下。',
  },
  ...CONFIG_STEPS,
];

export const WORKBENCH_STEPS = [
  { sel: '[data-tour="timeline"]', title: '建模流程', body: '按建模步骤推进。可点击的步骤能直接打开。' },
  { sel: '[data-tour="node-ingest"]', title: '建模资料上传', body: '上传数据表和业务资料。' },
  { sel: '[data-tour="node-analyze"]', title: '分析与提取', body: '查看调研、提取和问题结果。' },
  { sel: '[data-tour="node-interview"]', title: '专家访谈', body: '回答需要确认的问题。' },
  { sel: '[data-tour="node-generate"]', title: '知识包生成', body: '生成知识包并查看进度。' },
  { sel: '[data-tour="node-data_source"]', title: '数据源绑定', body: '连接评测需要的业务数据。' },
  { sel: '[data-tour="node-evaluate"]', title: '评测', body: '录入问题并运行评测。' },
  { sel: '[data-tour="node-optimize"]', title: '诊断与优化', body: '查看失败原因，应用修改。' },
  { sel: '[data-tour="context"]', title: '右栏', body: '查看资料、待办和版本。' },
  { sel: '[data-tour="config"]', title: '配置中心', body: '设置模型和评测服务。' },
  ...CONFIG_STEPS,
];

export const AI_CONSTRUCTION_WORKBENCH_STEPS = [
  { sel: '[data-tour="timeline"]', title: '建模流程', body: '准备、AI辅助构建、验证、改进和交付集中在同一工作台，可随时回看已完成步骤。' },
  { sel: '[data-tour="node-ingest"]', title: '建模资料上传', body: '上传数据表和业务资料，为 AI 辅助构建提供可追溯证据。' },
  { sel: '[data-tour="node-ai_construction"]', title: 'AI辅助构建', body: '建模助手会主动推进目标确认、流程访谈、流程图确认、知识构建和确定性能力确认。' },
  { sel: '[data-tour="node-scenario"]', title: '场景推演', body: '用 AI 构造的场景检查分支、异常、规则和脚本覆盖情况。' },
  { sel: '[data-tour="node-data_source"]', title: '数据源绑定', body: '连接业务数据；不需要查询数据时可明确选择跳过。' },
  { sel: '[data-tour="node-evaluate"]', title: '评测', body: '在同一页面完成评测用例、运行与结果、问题分析和优化更新。' },
  { sel: '[data-tour="node-release"]', title: '发布交付', body: '检查版本和发布门禁，确认后交付知识包。' },
  { sel: '[data-tour="config"]', title: '配置中心', body: '设置建模模型、Consumer 服务和评测数据。' },
  ...CONFIG_STEPS,
];

export default function Tour({ scene, steps }) {
  const doneKey = `mw_tour_done_${scene}`;
  const [idx, setIdx] = useState(-1); // -1 = 关闭
  const [rect, setRect] = useState(null);

  // 首次自动播放 + 顶栏按钮重看（按场景过滤）
  useEffect(() => {
    if (!localStorage.getItem(doneKey)) setIdx(0);
    const h = (e) => { if (!e.detail?.scene || e.detail.scene === scene) setIdx(0); };
    window.addEventListener('mw-tour-start', h);
    return () => window.removeEventListener('mw-tour-start', h);
  }, []);

  const close = () => {
    localStorage.setItem(doneKey, '1');
    setIdx(-1); setRect(null);
    // 通知宿主收尾（如关闭引导中打开的配置中心弹窗）
    window.dispatchEvent(new Event('mw-tour-end'));
  };

  // 执行步骤 enter 钩子并定位目标：弹窗/切 tab 后目标下帧才出现，轮询重试；超时跳过该步
  useEffect(() => {
    if (idx < 0 || idx >= steps.length) return;
    steps[idx].enter?.();
    let cancelled = false;
    let tries = 0;
    let el = null;
    const measure = () => { if (el) setRect(el.getBoundingClientRect()); };
    const locate = () => {
      if (cancelled) return;
      el = document.querySelector(steps[idx].sel);
      if (el) {
        el.scrollIntoView({ block: 'nearest' });
        measure();
        window.addEventListener('resize', measure);
        return;
      }
      if (++tries < 12) setTimeout(locate, 60);
      else if (idx < steps.length - 1) setIdx(idx + 1);
      else close();
    };
    setRect(null);
    locate();
    return () => { cancelled = true; window.removeEventListener('resize', measure); };
  }, [idx]);

  if (idx < 0 || !rect) return null;
  const step = steps[idx];
  const pad = 6;
  // 气泡放高亮区右侧，空间不足放左侧/下方
  const bubbleW = 340;
  const spaceRight = window.innerWidth - rect.right;
  const bubbleStyle = spaceRight > bubbleW + 40
    ? { left: rect.right + 18, top: Math.max(16, Math.min(rect.top, window.innerHeight - 260)) }
    : rect.left > bubbleW + 40
      ? { left: rect.left - bubbleW - 18, top: Math.max(16, Math.min(rect.top, window.innerHeight - 260)) }
      : { left: Math.max(16, Math.min(rect.left, window.innerWidth - bubbleW - 16)), top: Math.min(rect.bottom + 14, window.innerHeight - 240) };

  return (
    <div className="tour-ov" onClick={(e) => { if (e.target === e.currentTarget) close(); }}>
      <div className="tour-hl" style={{ left: rect.left - pad, top: rect.top - pad, width: rect.width + pad * 2, height: rect.height + pad * 2 }} />
      <div className="tour-bubble" style={{ ...bubbleStyle, width: bubbleW }}>
        <div className="tour-step">{idx + 1} / {steps.length}</div>
        <div className="tour-title">{step.title}</div>
        <div className="tour-body">{step.body}</div>
        <div className="tour-foot">
          <button className="tour-skip" onClick={close}>跳过引导</button>
          <span style={{ flex: 1 }} />
          {idx > 0 && <button className="btn" style={{ padding: '4px 14px', fontSize: 12 }} onClick={() => setIdx(idx - 1)}>上一步</button>}
          {idx < steps.length - 1
            ? <button className="btn btn-p" style={{ padding: '4px 14px', fontSize: 12 }} onClick={() => setIdx(idx + 1)}>下一步</button>
            : <button className="btn btn-p" style={{ padding: '4px 14px', fontSize: 12 }} onClick={close}>开始使用</button>}
        </div>
      </div>
    </div>
  );
}
