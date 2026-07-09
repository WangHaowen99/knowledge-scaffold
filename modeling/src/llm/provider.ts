import { streamSimple } from '@earendil-works/pi-ai/api/openai-completions';
import { getLlmSettings } from '../settings/repo.js';

// pi agent 的 LLM 依赖：一个 model 对象 + streamFn + getApiKey。
// 真实端点走 openai-completions；冒烟/测试可注入 faux（见 scripts/smoke-nodes.ts）。
export interface LlmDeps {
  model: any;
  streamFn: any;
  getApiKey: (provider: string) => string | undefined;
}

// 手构一个最小 openai-completions Model 对象（字段参照内置 provider 的 models）。
function buildModel(baseUrl: string, modelId: string, maxTokens: number): any {
  return {
    id: modelId,
    name: modelId,
    api: 'openai-completions',
    provider: 'modeling-llm',
    baseUrl,
    input: ['text'],
    reasoning: false,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: 128000,
    // 大规格（几十个对象）的 submit_package_spec 输出可能超 8k token，页面/env 可放宽
    maxTokens,
  };
}

// LLM 配置来源：配置中心（settings 表）优先，env 兜底——每次构建即时读取，页面改完即生效。
export function buildLlm(): LlmDeps {
  const llm = getLlmSettings();
  if (llm.baseUrl === 'faux') return buildFauxModeler();
  if (!llm.baseUrl || !llm.model) {
    throw new Error('未配置 LLM（请在配置中心填写 base url 与模型名，或设置 MODELING_LLM_BASE_URL / MODELING_LLM_MODEL）');
  }
  return {
    model: buildModel(llm.baseUrl, llm.model, llm.maxTokens),
    streamFn: streamSimple,
    getApiKey: () => llm.apiKey || undefined,
  };
}

// ---- faux 演示模式：脚本化"假建模者"，无需真实 key 即可端到端走完建模流程 ----
// 按 system prompt 关键词识别所处节点，按回合产出合理的工具调用。
async function loadFaux() {
  return import('@earendil-works/pi-ai/providers/faux');
}

function parseTablesFromToolResult(messages: any[]): { name: string; fields: string[] }[] {
  // list_tables 的结果形如 "STRUCTURE_LIST（3 行）: ID, ParentPart, ..."（每表一行）
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m?.role === 'toolResult' && m.toolName === 'list_tables') {
      const text = (m.content ?? []).map((c: any) => c.text ?? '').join('\n');
      return text.split('\n').map((line: string) => {
        const mm = line.match(/^(.+?)（\d+ 行）:\s*(.*)$/);
        return mm ? { name: mm[1].trim(), fields: mm[2].split(',').map((s: string) => s.trim()).filter(Boolean) } : null;
      }).filter(Boolean) as { name: string; fields: string[] }[];
    }
  }
  return [];
}

function parseNumberedList(prompt: string): string[] {
  return prompt.split('\n').map((l) => l.match(/^\d+\.\s*(.+)$/)?.[1]?.trim()).filter(Boolean) as string[];
}

export function buildFauxModeler(): LlmDeps {
  // 动态 import 是 ESM 顶层限制的折衷：faux 只在演示模式加载
  let handle: any = null;
  const factory = (fx: any) => async (context: any, _opts: any, _state: { callCount: number }) => {
    const sys: string = context.systemPrompt ?? '';
    const { fauxAssistantMessage, fauxToolCall } = fx;
    const hasToolResult = (name: string) => (context.messages ?? []).some((m: any) => m?.role === 'toolResult' && m.toolName === name);

    if (sys.includes('submit_analysis')) {
      if (!hasToolResult('list_tables')) return fauxAssistantMessage([fauxToolCall('list_tables', {})]);
      const tables = parseTablesFromToolResult(context.messages);
      return fauxAssistantMessage([fauxToolCall('submit_analysis', {
        domainOverview: `（演示模式）基于 ${tables.length} 张数据表的领域：${tables.map((t) => t.name).join('、')}。`,
        candidateObjects: tables.map((t) => ({ name: t.name, rationale: `来自数据表 ${t.name}（${t.fields.slice(0, 3).join('/')}…）`, sourceTables: [t.name] })),
        openQuestions: tables.slice(0, 2).map((t) => ({ topic: `${t.name} 关键字段语义`, why: `${t.fields[Math.min(1, t.fields.length - 1)] ?? t.fields[0]} 的业务口径需专家确认` })),
      })]);
    }
    if (sys.includes('ask_user')) {
      if (!hasToolResult('ask_user')) {
        return fauxAssistantMessage([fauxToolCall('ask_user', { questions: [
          { kind: 'single', q: '（演示）核心业务对象之间是父子结构还是引用关系？', options: ['父子结构', '引用关系', '两者都有'], topic: '关系' },
          { kind: 'multi', q: '（演示）以下哪些字段属于业务主键？', options: ['ID', '编码类字段', '名称类字段'], topic: '字段含义' },
          { kind: 'text', q: '（演示）请描述最重要的一个计算口径。', options: [], topic: '口径' },
        ] })]);
      }
      return fauxAssistantMessage([fauxToolCall('finish_interview', { summary: '（演示模式）关键决策点已澄清。' })]);
    }
    if (sys.includes('propose_competency_questions')) {
      if (!hasToolResult('list_tables')) return fauxAssistantMessage([fauxToolCall('list_tables', {})]);
      const tables = parseTablesFromToolResult(context.messages);
      const base = tables[0]?.name ?? '核心对象';
      return fauxAssistantMessage([fauxToolCall('propose_competency_questions', { questions: [
        `${base} 的某条记录明细是什么`, `${base} 按某字段汇总的结果`, `两个对象之间的关联关系如何查询`,
        `某编号对应的变更影响有哪些`, `某时间范围内的记录有多少`, `字段口径如何计算`,
      ] })]);
    }
    if (sys.includes('submit_package_spec')) {
      if (!hasToolResult('list_tables')) return fauxAssistantMessage([fauxToolCall('list_tables', {})]);
      const tables = parseTablesFromToolResult(context.messages);
      const selected = parseNumberedList(sys);
      return fauxAssistantMessage([fauxToolCall('submit_package_spec', {
        objects: tables.map((t) => ({
          key: t.name, title: t.name, description: `（演示）业务对象 ${t.name}，来源数据表 ${t.name}`,
          sourceTable: t.name, primaryKey: [t.fields[0] ?? 'ID'],
          fields: t.fields.map((f) => ({ key: f, cnName: f, type: 'string', required: f === t.fields[0], note: '' })),
        })),
        operations: selected.map((q, i) => ({
          key: `op_${i + 1}`, title: q.slice(0, 24), description: `（演示）回答：${q}`,
          inputs: [{ param: 'keyword', type: 'string', required: true, note: '查询关键字' }],
          outputs: [{ field: 'result', type: 'array', note: '查询结果' }],
          flow: ['定位相关业务对象', '按映射查询数据', '汇总返回'],
          relatedObjects: tables.slice(0, 1).map((t) => t.name),
          competencyQuestion: q,
        })),
      })]);
    }
    if (sys.includes('submit_suggestions')) {
      // faux 优化器：从最后一条用户消息解析未通过用例，逐条产出「沉淀业务口径」建议
      const um = [...(context.messages ?? [])].reverse().find((m: any) => m?.role === 'user');
      const text = typeof um?.content === 'string' ? um.content : (um?.content ?? []).map((c: any) => c.text ?? '').join('');
      const fails = [...text.matchAll(/\d+\.\s*问题：(.+?)\s*｜标准答案：(.+?)\s*｜判定：/g)]
        .map((m) => ({ question: m[1].trim(), groundTruth: m[2].trim() }));
      return fauxAssistantMessage([fauxToolCall('submit_suggestions', {
        suggestions: fails.map((f) => ({
          title: `沉淀「${f.question.slice(0, 18)}」的业务口径`,
          detail: `新增操作，把该问题的业务规则写入包内说明：${f.groundTruth}`,
          kind: 'add_operation',
          relatedQuestion: f.question,
        })),
      })]);
    }
    if (sys.includes('package_chat_plan')) {
      // faux 修缮·影响分析：带修改意图时给结构化计划，否则纯文字回复
      const um = [...(context.messages ?? [])].reverse().find((m: any) => m?.role === 'user');
      const text = typeof um?.content === 'string' ? um.content : (um?.content ?? []).map((c: any) => c.text ?? '').join('');
      const msg = (text.match(/【专家消息】\n([\s\S]*)$/) ?? [])[1]?.trim() ?? '';
      let spec: any = { objects: [], operations: [] };
      try { spec = JSON.parse((text.match(/【当前规格】\n(.*)/) ?? [])[1] ?? '{}'); } catch { /* 保底 */ }
      if (/修改|增加|新增|调整|删除|补充|改成|改为/.test(msg) && (spec.operations ?? []).length) {
        const removing = /删除/.test(msg);
        const target = spec.operations[0];
        return fauxAssistantMessage([fauxToolCall('submit_change_plan', {
          summary: `（演示计划）${msg.slice(0, 40)}`,
          changes: removing
            ? [{ targetKind: 'operation', targetKey: target.key, action: 'remove', description: `移除操作 ${target.key}` }]
            : [{ targetKind: 'operation', targetKey: `chat_${(spec.operations ?? []).length + 1}`, action: 'add', description: `（演示）按要求新增操作：${msg.slice(0, 60)}` }],
          risks: ['（演示）请确认改动不影响既有典型问题路由'],
        })]);
      }
      return fauxAssistantMessage(`（演示模式）已收到：「${msg.slice(0, 60)}」。当前包共 ${(spec.objects ?? []).length} 个对象、${(spec.operations ?? []).length} 个操作；如需修改请描述具体改动。`);
    }
    if (sys.includes('package_chat_apply')) {
      // faux 修缮·应用：按计划逐条落实（add 追加演示操作 / remove 移除指名条目）
      const um = [...(context.messages ?? [])].reverse().find((m: any) => m?.role === 'user');
      const text = typeof um?.content === 'string' ? um.content : (um?.content ?? []).map((c: any) => c.text ?? '').join('');
      let spec: any = { objects: [], operations: [] };
      let plan: any = { changes: [] };
      try { spec = JSON.parse((text.match(/【当前规格】\n(.*)/) ?? [])[1] ?? '{}'); } catch { /* 保底 */ }
      try { plan = JSON.parse((text.match(/【已确认的修缮计划】\n(.*)/) ?? [])[1] ?? '{}'); } catch { /* 保底 */ }
      let operations = [...(spec.operations ?? [])];
      for (const c of plan.changes ?? []) {
        if (c.targetKind !== 'operation') continue;
        if (c.action === 'remove') operations = operations.filter((o: any) => o.key !== c.targetKey);
        if (c.action === 'add') operations.push({
          key: c.targetKey, title: `（修缮）${String(c.description ?? '').slice(0, 18)}`,
          description: `（演示修缮）${c.description ?? ''}`,
          inputs: [], outputs: [], flow: ['按计划执行'], relatedObjects: (spec.objects ?? []).slice(0, 1).map((o: any) => o.key),
        });
      }
      return fauxAssistantMessage([fauxToolCall('submit_revised_spec', { objects: spec.objects ?? [], operations })]);
    }
    if (sys.includes('package_check')) {
      // faux 语义体检：返回一条演示发现（规则检查是确定性代码，不走这里）
      const um = [...(context.messages ?? [])].reverse().find((m: any) => m?.role === 'user');
      const text = typeof um?.content === 'string' ? um.content : (um?.content ?? []).map((c: any) => c.text ?? '').join('');
      let spec: any = { objects: [], operations: [] };
      try { spec = JSON.parse((text.match(/【当前规格】\n(.*)/) ?? [])[1] ?? '{}'); } catch { /* 保底 */ }
      const op = (spec.operations ?? [])[0];
      return fauxAssistantMessage([fauxToolCall('submit_findings', {
        findings: op ? [{
          severity: '警告', category: '兜底缺失', target: op.key,
          message: `（演示）操作「${op.title ?? op.key}」的流程引用查询结果时未见显式兜底`,
          suggestion: `为操作 ${op.key} 的查询步骤补充空结果兜底（跳过/默认值）`,
        }] : [],
      })]);
    }
    if (sys.includes('submit_revised_spec')) {
      // faux 修订：解析【当前规格】与【采纳的建议】，每条建议追加一个含业务规则的操作
      const um = [...(context.messages ?? [])].reverse().find((m: any) => m?.role === 'user');
      const text = typeof um?.content === 'string' ? um.content : (um?.content ?? []).map((c: any) => c.text ?? '').join('');
      let spec: any = { objects: [], operations: [] };
      let adopted: any[] = [];
      try { spec = JSON.parse((text.match(/【当前规格】\n([\s\S]*?)\n【采纳的建议】/) ?? [])[1] ?? '{}'); } catch { /* 保底空规格 */ }
      try { adopted = JSON.parse((text.match(/【采纳的建议】\n([\s\S]*?)\n请应用/) ?? [])[1] ?? '[]'); } catch { /* 保底空建议 */ }
      const baseCount = (spec.operations ?? []).length;
      const newOps = adopted.map((s: any, i: number) => ({
        key: `opt_${baseCount + i + 1}`,
        title: String(s.relatedQuestion ?? s.title ?? '优化操作').slice(0, 24),
        description: `（优化新增）${s.relatedQuestion ?? ''}：${s.detail ?? ''}`,
        inputs: [{ param: 'keyword', type: 'string', required: true, note: '查询关键字' }],
        outputs: [{ field: 'result', type: 'string', note: '结论' }],
        flow: ['按沉淀的业务规则直接作答', String(s.detail ?? '')],
        relatedObjects: (spec.objects ?? []).slice(0, 1).map((o: any) => o.key),
        competencyQuestion: s.relatedQuestion ?? undefined,
      }));
      return fauxAssistantMessage([fauxToolCall('submit_revised_spec', {
        objects: spec.objects ?? [],
        operations: [...(spec.operations ?? []), ...newOps],
      })]);
    }
    if (sys.includes('submit_verdict')) {
      // faux judge：标准答案的关键片段是否包含于回答（长度>1 的片段命中≥半数为 pass）
      const um = [...(context.messages ?? [])].reverse().find((m: any) => m?.role === 'user');
      const text = typeof um?.content === 'string' ? um.content : (um?.content ?? []).map((c: any) => c.text ?? '').join('');
      const gt = (text.match(/标准答案：([\s\S]*?)\n智能体回答：/) ?? [])[1] ?? '';
      const ans = (text.match(/智能体回答：([\s\S]*)$/) ?? [])[1] ?? '';
      const frags = gt.split(/[，。；、\s]+/).filter((f: string) => f.length > 1);
      const hit = frags.filter((f: string) => ans.includes(f)).length;
      const ratio = frags.length ? hit / frags.length : 0;
      const verdict = ratio >= 0.99 ? 'pass' : ratio >= 0.4 ? 'partial' : 'fail';
      return fauxAssistantMessage([fauxToolCall('submit_verdict', {
        verdict, reason: `（演示 judge）标准答案片段命中 ${hit}/${frags.length}`,
      })]);
    }
    return fauxAssistantMessage('（演示模式）收到。');
  };

  const streamFn = async (model: any, context: any, options: any) => {
    if (!handle) {
      const fx = await loadFaux();
      const core = fx.createFauxCore({ provider: 'faux', models: [{ id: 'faux-modeler' }] });
      // 关键：设置“无限工厂”——每次调用都按当前上下文动态生成响应
      const fn = factory(fx);
      const push = () => core.appendResponses([async (ctx: any, o: any, st: any, m: any) => { push(); return fn(ctx, o, st); }]);
      push();
      handle = core;
    }
    return handle.streamSimple(handle.getModel(), context, options);
  };

  return {
    model: { id: 'faux-modeler', name: 'faux-modeler', api: 'faux', provider: 'faux', input: ['text'], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 128000, maxTokens: 8192 },
    streamFn,
    getApiKey: () => 'faux',
  };
}
