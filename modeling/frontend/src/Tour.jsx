import { useEffect, useLayoutEffect, useState } from 'react';

// 全量功能蒙版引导：高亮目标元素（box-shadow 蒙版）+ 步骤气泡。
// 首次进入工作台自动播放；顶栏「❓ 引导」可重看。目标缺失的步骤自动跳过。
const STEPS = [
  { sel: '[data-tour="timeline"]', title: '建模流程', body: '九个主干节点自上而下走完一次建模：✓ 已完成、转圈进行中、灰色待解锁。已解锁的节点可随时点击切换查看。' },
  { sel: '[data-tour="node-ingest"]', title: '① 资料上传与解析', body: '上传 Excel 数据表（sheet=表、首行字段、次行可选注释）与业务规范文档。资料可删除——删除后下游结果会标记「可能过时」，由你决定何时重跑。' },
  { sel: '[data-tour="node-analyze"]', title: '② 领域分析 + 领域问题', body: 'agent 解读资料产出领域概览与候选对象，并自动生成「领域问题」候选（知识包建成后必须能答的问题）。勾选 ≥5 条才解锁建模访谈。' },
  { sel: '[data-tour="node-interview"]', title: '③ 建模访谈', body: 'agent 围绕对象划分/字段含义/关系/口径分批出题（一般 2~3 批），你的回答会沉淀为建模依据。出题在服务端执行，切换页面不影响。' },
  { sel: '[data-tour="node-generate"]', title: '④ 知识包生成', body: '基于数据字典、访谈结论与领域问题产出标准包（对象/操作/映射），并检查每条领域问题是否被操作认领。' },
  { sel: '[data-tour="node-review"]', title: '⑤ 知识包完善', body: '「包体检」用规则引擎+语义 agent 找问题（口径冲突、兜底缺失、路由重复…）；文件可直接编辑（✎ 修订稿优先生效）；右栏修缮对话支持自然语言改包——先出影响分析计划，确认后才落新版本。' },
  { sel: '[data-tour="node-testcases"]', title: '⑥ 评测用例录入', body: '每条用例 = 问题 + 标准答案，可从已选领域问题一键预填。' },
  { sel: '[data-tour="node-evaluate"]', title: '⑦ 评测运行', body: '在 consumer 的影子工作区导入当前包并逐条运行用例，LLM Judge 判分，支持人工改判。轮次历史按版本留存，进度实时可见。' },
  { sel: '[data-tour="node-optimize"]', title: '⑧ 优化建议与应用', body: '优化器按执行痕迹分诊未通过用例（缺知识/没走到/写错了），连同体检发现生成建议；勾选采纳一键生成新版本，再回评测验证提升。版本历史可逐版导出 zip 交付。' },
  { sel: '[data-tour="context"]', title: '右栏 · 上下文', body: '数据字典与已上传文档随时查阅；进入「知识包完善」时这里会切换为包修缮对话。' },
  { sel: '[data-tour="config"]', title: '配置中心', body: '大模型（url/key/模型，可测有效性）、consumer 服务地址、评测数据（评测库与数据连接默认值）都在这里配置，保存即生效。' },
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
