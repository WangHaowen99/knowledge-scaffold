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
    body: '领域分析、访谈出题、包生成、评测判分都靠这个模型。填 OpenAI-compatible 的 Base URL、模型名和 API Key，点「测试有效性」一键验证。\n想先试玩流程？Base URL 直接填 faux，不需要 Key 就能走完全流程。',
  },
  {
    sel: '[data-tour="cfg-consumer"]', enter: openSettings('consumer'),
    title: '配置中心 · consumer 服务',
    body: '评测时知识包会导入这个 consumer 服务实际运行。地址一般部署时已配好，点「测试连接」检查三项：服务可达、认证通过、模型就绪——三项全绿评测才能跑起来。',
  },
  {
    sel: '[data-tour="cfg-eval"]', enter: openSettings('eval'),
    title: '配置中心 · 评测数据',
    body: '上面是评测库 MySQL 地址：上传的 Excel 数据会自动落到这个库，供查询类用例使用。下面是评测数据源的默认值（provider、数据服务 URL、token、scene），开评测时会自动预填。改完记得点「保存全部」。',
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
  { sel: '[data-tour="timeline"]', title: '建模流程', body: '从上到下按顺序走完这九个步骤，就完成一次建模：✓ 绿色表示已完成，转圈表示正在执行，灰色表示还没解锁。做过的步骤随时可以点回去查看。' },
  { sel: '[data-tour="node-ingest"]', title: '① 资料上传与解析', body: '把 Excel 数据表和业务文档（规则、口径、流程说明等）上传进来。Excel 里每个 sheet 是一张表，第一行是字段名，第二行可以写中文说明。传错了可以删除；删除后已算出的结果会标「可能过时」，由你决定何时重新分析。' },
  { sel: '[data-tour="node-analyze"]', title: '② 领域分析 + 领域问题', body: '系统自动阅读资料，整理出业务概览和候选业务对象，并推荐一批「领域问题」——也就是知识包建好后必须能答对的业务问题。勾选至少 5 条，才能进入知识萃取访谈。' },
  { sel: '[data-tour="node-interview"]', title: '③ 知识萃取访谈', body: '系统像顾问一样向你提问（一般 2~3 批）：对象怎么划分、字段是什么含义、口径怎么算。你的回答会成为建模的依据。提问在服务器上进行，切换页面不会中断。' },
  { sel: '[data-tour="node-generate"]', title: '④ 知识包生成', body: '根据数据字典、访谈结论和领域问题，自动生成知识包（业务对象、操作、字段映射），并逐条检查每个领域问题是否都有对应的操作能回答。' },
  { sel: '[data-tour="node-review"]', title: '⑤ 知识包完善', body: '「知识包体检」自动检查常见问题（口径冲突、兜底缺失、路由重复等）；每个文件都可以直接查看和编辑（✎ 表示人工修订稿，优先生效）；右栏的修缮对话支持用大白话提修改——先给出影响分析，你确认后才会生成新版本。' },
  { sel: '[data-tour="node-testcases"]', title: '⑥ 评测用例录入', body: '每条用例就是一道考题：问题 + 标准答案。可以从已勾选的领域问题一键预填，再逐条补上标准答案。' },
  { sel: '[data-tour="node-evaluate"]', title: '⑦ 评测运行', body: '把当前知识包放进独立的测试环境，逐条运行用例并自动判分；判得不准可以人工改判。每一轮结果都按包版本保存，随时回看对比。' },
  { sel: '[data-tour="node-optimize"]', title: '⑧ 优化建议与应用', body: '对没通过的用例，系统会分析原因（缺知识、没用上、写错了）并给出修改建议；勾选认可的建议，一键生成新版本，再回到评测验证是否有提升。每个版本都能导出 zip 交付。' },
  { sel: '[data-tour="context"]', title: '右栏 · 上下文', body: '数据字典和已上传的文档随时在这里查阅；进入「知识包完善」步骤时，这里会切换成修缮对话。' },
  { sel: '[data-tour="config"]', title: '配置中心', body: '大模型、consumer 服务与评测数据都在右上角的配置中心统一管理，保存即生效。接下来打开它逐项看一下。' },
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
