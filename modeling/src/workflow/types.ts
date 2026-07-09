// pi AgentTool 的本地宽松别名（避免到处引 typebox 泛型）。
export interface AgentTool {
  name: string;
  label: string;
  description: string;
  parameters: unknown;
  execute: (id: string, params: any, signal?: AbortSignal, onUpdate?: unknown) => Promise<{
    content: { type: 'text'; text: string }[];
    details: unknown;
    terminate?: boolean;
  }>;
}
