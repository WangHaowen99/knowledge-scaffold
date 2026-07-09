// 极简 YAML 解析（仅供 smoke 校验 manifest 的扁平 key: value；非通用 YAML）。
export function parse(text: string): Record<string, any> {
  const out: Record<string, any> = {};
  for (const line of text.split('\n')) {
    const m = line.match(/^([a-zA-Z_][\w-]*):\s*(.*)$/);
    if (!m) continue;
    let v: any = m[2].trim();
    if (v === '') continue;
    if (/^-?\d+(\.\d+)?$/.test(v)) v = Number(v);
    else if (v === 'true') v = true;
    else if (v === 'false') v = false;
    else if (v.startsWith('"') && v.endsWith('"')) { try { v = JSON.parse(v); } catch { /* keep */ } }
    out[m[1]] = v;
  }
  return out;
}
