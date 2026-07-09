import { useEffect, useLayoutEffect, useState } from 'react';

// 全量功能蒙版引导：高亮目标元素（box-shadow 蒙版）+ 步骤气泡。
// 首次进入工作台自动播放；顶栏「❓ 引导」可重看。目标缺失的步骤自动跳过。
const STEPS = [
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
  { sel: '[data-tour="config"]', title: '配置中心', body: '使用前先在右上角「⚙ 配置中心」完成三项配置，保存即生效：\n· 大模型：Base URL、模型名、API Key、单次输出上限——分析、访谈、生成、判分都靠它，可一键测试有效性；\n· consumer 服务：评测时承载知识包运行的服务地址，可测试连通性；\n· 评测数据：评测库 MySQL 地址，以及数据连接默认值（provider、数据服务 URL、token、scene），开评测时会自动预填。' },
];

const DONE_KEY = 'mw_tour_done';

export default function Tour() {
  const [idx, setIdx] = useState(-1); // -1 = 关闭
  const [rect, setRect] = useState(null);

  // 首次自动播放 + 顶栏按钮重看
  useEffect(() => {
    if (!localStorage.getItem(DONE_KEY)) setIdx(0);
    const h = () => setIdx(0);
    window.addEventListener('mw-tour-start', h);
    return () => window.removeEventListener('mw-tour-start', h);
  }, []);

  const close = () => { localStorage.setItem(DONE_KEY, '1'); setIdx(-1); };

  // 定位当前步骤目标；目标缺失自动跳到下一个可用步骤
  useLayoutEffect(() => {
    if (idx < 0) return;
    let i = idx;
    let el = null;
    while (i < STEPS.length && !(el = document.querySelector(STEPS[i].sel))) i++;
    if (!el) { close(); return; }
    if (i !== idx) { setIdx(i); return; }
    el.scrollIntoView({ block: 'nearest' });
    const measure = () => setRect(el.getBoundingClientRect());
    measure();
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  }, [idx]);

  if (idx < 0 || !rect) return null;
  const step = STEPS[idx];
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
        <div className="tour-step">{idx + 1} / {STEPS.length}</div>
        <div className="tour-title">{step.title}</div>
        <div className="tour-body">{step.body}</div>
        <div className="tour-foot">
          <button className="tour-skip" onClick={close}>跳过引导</button>
          <span style={{ flex: 1 }} />
          {idx > 0 && <button className="btn" style={{ padding: '4px 14px', fontSize: 12 }} onClick={() => setIdx(idx - 1)}>上一步</button>}
          {idx < STEPS.length - 1
            ? <button className="btn btn-p" style={{ padding: '4px 14px', fontSize: 12 }} onClick={() => setIdx(idx + 1)}>下一步</button>
            : <button className="btn btn-p" style={{ padding: '4px 14px', fontSize: 12 }} onClick={close}>开始使用</button>}
        </div>
      </div>
    </div>
  );
}
