import { streamSimple } from '@earendil-works/pi-ai/api/openai-completions';
import { getLlmSettings, getTaskLlmSettings, type LlmSettings } from '../settings/repo.js';

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

function buildConfiguredLlm(llm: LlmSettings, maxTokensCap?: number, task = false): LlmDeps {
  const cap = (deps: LlmDeps): LlmDeps => maxTokensCap
    ? { ...deps, model: { ...deps.model, maxTokens: Math.min(deps.model.maxTokens, maxTokensCap) } }
    : deps;
  if (llm.baseUrl === 'faux') return cap(buildFauxModeler());
  if (!llm.baseUrl || !llm.model) {
    throw new Error(task
      ? '任务 LLM 配置不完整（Base URL 与模型名必须在同一配置作用域内同时设置）'
      : '未配置 LLM（请在配置中心填写 Base URL 与模型名，或设置 MODELING_LLM_BASE_URL / MODELING_LLM_MODEL）');
  }
  const maxTokens = maxTokensCap ? Math.min(llm.maxTokens, maxTokensCap) : llm.maxTokens;
  return {
    model: buildModel(llm.baseUrl, llm.model, maxTokens),
    streamFn: streamSimple,
    getApiKey: () => llm.apiKey || undefined,
  };
}

// 每次构建解析个人配置或已固化的用户快照；当前平台模板不参与运行时兜底。
export function buildLlm(): LlmDeps {
  return buildConfiguredLlm(getLlmSettings());
}

export interface TaskLlmOptions {
  maxTokensCap?: number;
}

/**
 * 用户覆盖了端点/模型/Key 时，领域分析和知识生成使用该用户模型；否则优先使用
 * 部署侧任务模型，未配置任务模型时回退部署主模型。
 * 部分配置直接报错，避免后台任务静默跑到错误端点。
 */
export function buildTaskLlm(options: TaskLlmOptions = {}): LlmDeps {
  return buildConfiguredLlm(getTaskLlmSettings(), options.maxTokensCap, true);
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

export function nextRequiredDocumentRead(messages: any[], fauxToolCall: (name: string, params: unknown) => unknown) {
  const listResult = [...messages].reverse().find((message: any) => (
    message?.role === 'toolResult' && message?.toolName === 'list_documents'
  ));
  if (!listResult) return fauxToolCall('list_documents', {});
  const listText = (listResult.content ?? []).map((part: any) => part?.text ?? '').join('\n');
  const filenames = [...listText.matchAll(/【(.+?)】共\s*\d+\s*字符/gu)].map((match) => match[1]);
  if (!filenames.length) return null;

  const reads = messages.filter((message: any) => (
    message?.role === 'toolResult' && message?.toolName === 'read_document'
  ));
  const previousPageSummary = reads.length ? '（演示模式）已记录上一页的章节要点和重要证据。' : undefined;
  for (const filename of filenames) {
    const latest = [...reads].reverse().find((message: any) => message?.details?.filename === filename);
    if (!latest) return fauxToolCall('read_document', { filename, offset: 0, previousPageSummary });
    if (!latest.details?.eof) {
      return fauxToolCall('read_document', {
        filename,
        offset: latest.details?.nextOffset ?? latest.details?.endOffset ?? 0,
        previousPageSummary,
      });
    }
  }
  return null;
}

function latestUserJson(messages: any[]): any {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message?.role !== 'user') continue;
    const raw = typeof message.content === 'string'
      ? message.content
      : Array.isArray(message.content)
        ? message.content.map((part: any) => part?.text ?? '').join('')
        : '';
    try { return JSON.parse(raw); } catch { return undefined; }
  }
  return undefined;
}

export function buildFauxModeler(): LlmDeps {
  // 动态 import 是 ESM 顶层限制的折衷：faux 只在演示模式加载
  let handle: any = null;
  const factory = (fx: any) => async (context: any, _opts: any, _state: { callCount: number }) => {
    const sys: string = context.systemPrompt ?? '';
    const { fauxAssistantMessage, fauxToolCall } = fx;
    const hasToolResult = (name: string) => (context.messages ?? []).some((m: any) => m?.role === 'toolResult' && m.toolName === name);

    if (sys.includes('所有文本材料必须') || sys.includes('专家明确要求全文阅读')) {
      const nextRead = nextRequiredDocumentRead(context.messages ?? [], fauxToolCall);
      if (nextRead) return fauxAssistantMessage([nextRead]);
    }

    if (sys.includes('submit_modeling_goal_brief')) {
      const goal = String(latestUserJson(context.messages ?? [])?.modelingGoal ?? '').trim();
      const problem = goal.split(/[，,。；;]/u).map((part) => part.trim()).find(Boolean);
      return fauxAssistantMessage([fauxToolCall('submit_modeling_goal_brief', {
        problem: problem || '建立业务申请处理知识模型',
        targetUsers: '业务经办人员、审核人员',
        scenarios: '根据申请资料、业务规则和异常条件形成可执行处理方案',
        successCriteria: '生成可执行、可追溯且约束明确的业务处理方案',
      })]);
    }

    if (sys.includes('submit_process_draft')) {
      const candidate = latestUserJson(context.messages ?? [])?.candidateProcess;
      return fauxAssistantMessage([fauxToolCall('submit_process_draft', candidate ? {
        title: candidate.title,
        trigger: candidate.trigger,
        actors: candidate.actors,
        inputs: candidate.inputs,
        steps: candidate.steps,
        exceptions: candidate.exceptions,
        outputs: candidate.outputs,
        handoffs: candidate.handoffs,
      } : {
        title: '业务申请处理流程',
        trigger: '收到业务申请',
        actors: ['业务经办人员', '审核人员'],
        inputs: ['业务申请资料', '适用规则和处理约束'],
        steps: ['校验申请资料', '按规则生成候选处理方案', '审核并确认处理结果'],
        exceptions: ['关键约束缺失时标记待确认'],
        outputs: ['已确认的业务处理结果'],
        handoffs: ['业务经办人员提交审核人员确认'],
      })]);
    }

    if (sys.includes('submit_analysis')) {
      if (!hasToolResult('list_tables')) return fauxAssistantMessage([fauxToolCall('list_tables', {})]);
      const tables = parseTablesFromToolResult(context.messages);
      return fauxAssistantMessage([fauxToolCall('submit_analysis', {
        domainOverview: `（演示模式）基于 ${tables.length} 张数据表的领域：${tables.map((t) => t.name).join('、')}。`,
        candidateObjects: tables.map((t) => ({ name: t.name, rationale: `来自数据表 ${t.name}（${t.fields.slice(0, 3).join('/')}…）`, sourceTables: [t.name] })),
        openQuestions: tables.slice(0, 2).map((t) => ({ topic: `${t.name} 关键字段语义`, why: `${t.fields[Math.min(1, t.fields.length - 1)] ?? t.fields[0]} 的业务口径需专家确认` })),
      })]);
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
    if (sys.includes('submit_quality_gaps')) {
      // faux 资料缺口分析：返回一条演示缺口
      return fauxAssistantMessage([fauxToolCall('submit_quality_gaps', { gaps: [{
        kind: '口径不明', target: '演示字段',
        message: '（演示）关键枚举列缺少口径说明，统计边界不明', how: '访谈确认各枚举值口径',
      }] })]);
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
          diagnosisIds: [],
          evidence: ['faux 演示模式中的评测失败记录'],
          generalizationPlan: '将确认后的业务口径沉淀为可复用的知识包操作。',
          validationPlan: '使用独立问题、空输入和缺失字段场景验证操作路由与兜底行为。',
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
            ? [
              { targetKind: 'operation', targetKey: target.key, action: 'remove', description: `移除操作 ${target.key}` },
              ...(target.script?.key ? [{
                targetKind: 'script',
                targetKey: target.script.key,
                action: 'remove',
                description: `移除操作 ${target.key} 绑定的脚本 ${target.script.key}`,
              }] : []),
            ]
            : [{ targetKind: 'operation', targetKey: `chat_${(spec.operations ?? []).length + 1}`, action: 'add', description: `（演示）按要求新增操作：${msg.slice(0, 60)}` }],
          risks: ['（演示）请确认改动不影响既有典型问题路由'],
        })]);
      }
      return fauxAssistantMessage(`（演示模式）已收到：「${msg.slice(0, 60)}」。当前包共 ${(spec.objects ?? []).length} 个对象、${(spec.operations ?? []).length} 个操作；如需修改请描述具体改动。`);
    }
    if (sys.includes('package_chat_apply')) {
      // faux 修缮·应用：按当前原子批次只提交计划内的完整资源定义。
      const um = [...(context.messages ?? [])].reverse().find((m: any) => m?.role === 'user');
      const text = typeof um?.content === 'string' ? um.content : (um?.content ?? []).map((c: any) => c.text ?? '').join('');
      let changeContext: any = { targets: [], referenceIndex: {} };
      try {
        changeContext = JSON.parse(
          (text.match(/【原子变更上下文】\n([^\n]+)/) ?? [])[1] ?? '{}',
        );
      } catch { /* 保底为空补丁，由调用方给出明确缺项错误 */ }
      const patch: Record<string, any[]> = {};
      const add = (property: string, value: any) => {
        (patch[property] ??= []).push(value);
      };
      const firstObject = changeContext.referenceIndex?.objects?.[0]?.key;
      const firstOperation = changeContext.targets?.find((target: any) => (
        target.targetKind === 'operation' && target.action !== 'remove'
      ))?.targetKey ?? changeContext.referenceIndex?.operations?.[0]?.key ?? 'operation';
      for (const target of changeContext.targets ?? []) {
        if (target.action === 'remove') continue;
        const description = String(target.description ?? '按已确认方案修缮').trim();
        if (target.current) {
          const current = { ...target.current };
          if (target.targetKind === 'operation') current.description = `${current.description ?? ''}；${description}`;
          else if (target.targetKind === 'rule') current.content = `${current.content ?? ''}；${description}`;
          else if (target.targetKind === 'metric') current.caliber = `${current.caliber ?? ''}；${description}`;
          else current.description = `${current.description ?? ''}；${description}`;
          add(`${target.targetKind}s`, current);
          continue;
        }
        if (target.targetKind === 'object') {
          add('objects', {
            key: target.targetKey,
            title: `（修缮）${description.slice(0, 18)}`,
            description: `（演示修缮）${description}`,
            fields: [{ key: 'id', cnName: '标识', type: 'string' }],
          });
        } else if (target.targetKind === 'operation') {
          add('operations', {
            key: target.targetKey,
            title: `（修缮）${description.slice(0, 18)}`,
            description: `（演示修缮）${description}`,
            inputs: [], outputs: [], flow: ['按已确认方案执行'],
            relatedObjects: firstObject ? [firstObject] : [],
          });
        } else if (target.targetKind === 'rule') {
          add('rules', {
            key: target.targetKey,
            title: `（修缮）${description.slice(0, 18)}`,
            content: description,
            appliesToObjects: firstObject ? [firstObject] : [],
          });
        } else if (target.targetKind === 'metric') {
          add('metrics', {
            key: target.targetKey,
            title: `（修缮）${description.slice(0, 18)}`,
            caliber: description,
            boundaries: ['仅使用已确认的知识包数据'],
            usesObjects: firstObject ? [firstObject] : [],
          });
        } else if (target.targetKind === 'script') {
          add('scripts', {
            key: target.targetKey,
            title: `（修缮）${description.slice(0, 18)}`,
            description: `（演示修缮）${description}`,
            boundOperation: firstOperation,
            runtime: 'python', entrypoint: `${target.targetKey}.py`, timeoutSeconds: 30,
            parameters: [{ name: 'input_json', flag: '--input-json', type: 'string', required: true }],
            outputs: [{ key: 'result', type: 'object' }],
            boundaries: ['仅处理结构化输入'],
            sourceCode: [
              'import argparse', 'import json', 'parser = argparse.ArgumentParser()',
              'parser.add_argument("--input-json", required=True)', 'args = parser.parse_args()',
              'print(json.dumps({"result": json.loads(args.input_json)}))',
            ].join('\n'),
          });
        }
      }
      return fauxAssistantMessage([fauxToolCall('submit_resource_patch', patch)]);
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
        rules: spec.rules ?? [],
        metrics: spec.metrics ?? [],
        scripts: spec.scripts ?? [],
      })]);
    }
    if (sys.includes('submit_verdict')) {
      // faux judge：标准答案的关键片段是否包含于回答（长度>1 的片段命中≥半数为 pass）
      const um = [...(context.messages ?? [])].reverse().find((m: any) => m?.role === 'user');
      const text = typeof um?.content === 'string' ? um.content : (um?.content ?? []).map((c: any) => c.text ?? '').join('');
      const gt = (text.match(/标准答案：([\s\S]*?)\n智能体(?:完整)?回答：/) ?? [])[1] ?? '';
      const ans = (text.match(/智能体(?:完整)?回答：([\s\S]*)$/) ?? [])[1] ?? '';
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
