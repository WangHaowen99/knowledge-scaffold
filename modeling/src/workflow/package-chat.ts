import { Type } from '@earendil-works/pi-ai';
import { getDb, nowIso } from '../db/index.js';
import { buildLlm } from '../llm/provider.js';
import { makeAgent, runAgent } from './agent.js';
import { checkCoverage } from './m2.js';
import * as repo from './m2-repo.js';
import type { PackageSpec, Project } from '../types.js';

// 知识包完善（节点 7）右栏的修缮 agent：
// 专家用自然语言描述修改 → agent 要么直接回答（咨询类），要么调 submit_revised_spec
// 提交修订后的全量规格 → 落为新版本。无状态：每轮把当前规格随消息带入。

export interface ChatTurn { role: 'user' | 'assistant'; text: string; }
export interface ChatResult { reply: string; version?: string; }

export async function packageChat(project: Project, history: ChatTurn[], message: string): Promise<ChatResult> {
  const latest = repo.latestPackageVersion(project.id);
  if (!latest) throw new Error('尚未生成知识包');

  const cap: { revised?: { objects: PackageSpec['objects']; operations: PackageSpec['operations'] } } = {};
  const submitRevised = {
    name: 'submit_revised_spec',
    label: '提交修订后的规格',
    description: '当专家要求修改知识包时调用：提交应用修改后的完整规格（全量 objects + operations，不是增量）。仅咨询/解释类问题不要调用。',
    parameters: Type.Object({
      objects: Type.Array(Type.Any()),
      operations: Type.Array(Type.Any()),
    }),
    async execute(_id: string, params: any) {
      cap.revised = { objects: params.objects ?? [], operations: params.operations ?? [] };
      return { content: [{ type: 'text' as const, text: '修订规格已提交。' }], details: {}, terminate: true };
    },
  };

  const sys = [
    '你是知识包修缮助手（package_chat）。专家在评审知识包，会用自然语言提出修改或提问。',
    '规则：① 咨询/解释类问题直接文字回答；② 要求修改包内容时，把修改落实到规格上，',
    '用 submit_revised_spec 提交完整规格（保留未涉及的对象与操作，不得丢失）；',
    '③ 修改要具体可落地：字段说明写清楚业务口径，操作的 flow 分步描述。',
    `项目背景：${project.background || project.domain || project.name}`,
  ].join('\n');

  const historyText = history.slice(-6).map((t) => `${t.role === 'user' ? '专家' : '助手'}：${t.text}`).join('\n');
  const body = [
    '【当前规格】',
    JSON.stringify({ objects: latest.spec.objects, operations: latest.spec.operations }),
    historyText ? `【此前对话】\n${historyText}` : '',
    `【专家消息】\n${message}`,
  ].filter(Boolean).join('\n');

  const agent = makeAgent(buildLlm(), sys, [submitRevised] as any);
  const messages = await runAgent(agent, body);

  if (cap.revised && (cap.revised.objects.length || cap.revised.operations.length)) {
    const spec = latest.spec;
    if (cap.revised.objects.length < spec.objects.length || cap.revised.operations.length < spec.operations.length) {
      return { reply: `修订被拒绝：agent 提交的规格丢失了既有内容（对象 ${spec.objects.length}→${cap.revised.objects.length}，操作 ${spec.operations.length}→${cap.revised.operations.length}）。请换个说法重试。` };
    }
    const version = `1.${repo.listPackageVersions(project.id).length}.0`;
    const newSpec: PackageSpec = { ...spec, version, objects: cap.revised.objects, operations: cap.revised.operations };
    const selected = repo.listCompetency(project.id).filter((q) => q.selected).map((q) => q.question);
    repo.insertPackageVersion(project.id, version, newSpec, checkCoverage(newSpec, selected), `包修缮对话：${message.slice(0, 60)}`);
    return { reply: `已按要求修订并生成新版本 v${version}（对象 ${newSpec.objects.length} / 操作 ${newSpec.operations.length}）。左侧文件树已是最新内容。`, version };
  }

  // 纯文字回答：取最后一条 assistant 文本
  const lastText = [...messages].reverse().find((m: any) => m?.role === 'assistant' && (typeof m.content === 'string' || Array.isArray(m.content)));
  const text = lastText
    ? (typeof (lastText as any).content === 'string'
      ? (lastText as any).content
      : ((lastText as any).content ?? []).map((c: any) => c.text ?? '').join(''))
    : '（agent 未给出答复）';
  return { reply: text || '（agent 未给出答复）' };
}

// ---- 包文件人工修订（覆盖渲染产物）----
export function getOverrides(projectId: string): Record<string, string> {
  const rows = getDb().prepare('SELECT path, content FROM package_overrides WHERE project_id = ?').all(projectId) as any[];
  return Object.fromEntries(rows.map((r) => [r.path, r.content]));
}
export function setOverride(projectId: string, path: string, content: string): void {
  getDb().prepare(`INSERT INTO package_overrides (project_id, path, content, updated_at) VALUES (?,?,?,?)
    ON CONFLICT(project_id, path) DO UPDATE SET content = excluded.content, updated_at = excluded.updated_at`)
    .run(projectId, path, content, nowIso());
}
export function clearOverride(projectId: string, path: string): void {
  getDb().prepare('DELETE FROM package_overrides WHERE project_id = ? AND path = ?').run(projectId, path);
}
