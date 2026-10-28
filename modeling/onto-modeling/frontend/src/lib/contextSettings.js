// 上下文配置页辅助：表单提交体、留空生效值，以及模型上下文推荐表的预填逻辑。
// 推荐表只预填配置页表单（命中填入、未命中清空），用户可改；运行时仍是 配置页 > .env > 内置默认。
// 压缩窗口与触发百分比不在配置页，由服务端按生效的窗口与最大输出推算。
// 稳定引擎与内测引擎各存一组窗口与最大输出，推荐表两边共用。

// 配置页可填的项，也是推荐表给出的项
export const CONTEXT_FIELD_NAMES = ['max_context_tokens', 'max_output_tokens'];

// 各项的取值范围，与后端 model_settings.CONTEXT_FIELDS 一致
export const CONTEXT_LIMITS = {
  max_context_tokens: { min: 1000, max: 10000000 },
  max_output_tokens: { min: 1000, max: 1000000 },
};

export const PRESET_MISS_TEXT =
  '推荐表中没有该模型，已清空上下文窗口和最大输出，请按模型厂商公布的值填写；留空则按输入框里提示的值生效';

// 推荐表中没有该模型、但不清空输入框时的提示：打开页面时的已保存模型、改回已保存的模型、推荐表改过后重新查表
export const PRESET_ABSENT_HINT = {
  type: 'warning',
  text: '推荐表中没有该模型，上下文参数不会自动填入，请确认与模型厂商公布的值一致',
};

// 查表未命中、已清空过上下文参数的状态（matchedId 取此值）
export const PRESET_MISSED = Symbol('preset-missed');

const CLEARED_VALUES = Object.fromEntries(CONTEXT_FIELD_NAMES.map((name) => [name, null]));

export function contextPayload(values) {
  return Object.fromEntries(CONTEXT_FIELD_NAMES.map((name) => [name, values?.[name] ?? null]));
}

// 以下常量与推算须与后端 model_settings.derived_compaction 一致。
// CLI 的自动压缩规则：有效窗口 = min(压缩窗口, 模型窗口) − min(最大输出, 20000)，
// 触发点 = 有效窗口 − 13000；设了触发百分比时取 min(有效窗口 × 百分比, 上式)
const OUTPUT_RESERVE_CAP = 20000;
const AUTOCOMPACT_BUFFER = 13000;
// 压缩参数只认 .env，都没配时服务端按余量不少于窗口 10% 推算：
// 压缩窗口向下取 10000 的倍数，低于 CLI 下限时改推触发百分比（向下取 5 的倍数）
const COMPACT_MARGIN_RATIO = 0.1;
const COMPACT_WINDOW_MIN = 100000;
const COMPACT_WINDOW_MAX = 1000000;
const COMPACT_WINDOW_STEP = 10000;
const COMPACT_PCT_STEP = 5;

function derivedCompaction(real, output) {
  const none = { auto_compact_window: null, autocompact_pct: null };
  if (!output) return none;
  const thresholdCap = Math.floor(real * (1 - COMPACT_MARGIN_RATIO)) - output;
  const reserve = Math.min(output, OUTPUT_RESERVE_CAP);
  const window =
    Math.floor((thresholdCap + reserve + AUTOCOMPACT_BUFFER) / COMPACT_WINDOW_STEP) * COMPACT_WINDOW_STEP;
  if (window >= real) return none;
  if (window >= COMPACT_WINDOW_MIN) {
    return { ...none, auto_compact_window: Math.min(window, COMPACT_WINDOW_MAX) };
  }
  const effective = real - reserve;
  if (effective <= 0) return none;
  const pct = Math.floor(Math.floor((thresholdCap * 100) / effective) / COMPACT_PCT_STEP) * COMPACT_PCT_STEP;
  if (pct >= 1 && Math.floor((effective * pct) / 100) < effective - AUTOCOMPACT_BUFFER) {
    return { ...none, autocompact_pct: pct };
  }
  return none;
}

// 按输入框里的值取生效的窗口与最大输出；留空的项按 defaults（.env 或内置默认）。
// 两项都留空或窗口未知时返回 null；最大输出不小于窗口时返回 { error }（这组值保存不了）
function pickContext(values, defaults) {
  const filled = contextPayload(values);
  if (CONTEXT_FIELD_NAMES.every((name) => filled[name] === null)) return null;
  const pick = (name) => filled[name] ?? defaults?.[name] ?? null;
  const real = pick('max_context_tokens');
  const output = pick('max_output_tokens');
  if (!real) return null;
  if (output && output >= real) {
    return {
      error:
        filled.max_output_tokens === null
          ? `最大输出留空时按 ${fmt(output)} 生效，须小于上下文窗口，请填写最大输出`
          : '最大输出必须小于上下文窗口',
    };
  }
  return { real, output, pick };
}

// 按输入框里的窗口与最大输出实时估算稳定引擎的自动压缩触发点。
// 两项都留空时返回 null 不显示。返回 { error } 表示这组值保存不了或压缩点算不出来；
// 否则 { compactAt, percent（占上下文窗口）, margin（压缩前请求的余量，最大输出未知时为 null）}
export function estimateCompaction(values, defaults) {
  const picked = pickContext(values, defaults);
  if (!picked || picked.error) return picked;
  const { real, output, pick } = picked;
  let compact = { auto_compact_window: pick('auto_compact_window'), autocompact_pct: pick('autocompact_pct') };
  if (!compact.auto_compact_window && !compact.autocompact_pct) compact = derivedCompaction(real, output);
  const reserve = Math.min(output || OUTPUT_RESERVE_CAP, OUTPUT_RESERVE_CAP);
  const effective = Math.min(compact.auto_compact_window || real, real) - reserve;
  let compactAt = effective - AUTOCOMPACT_BUFFER;
  if (compact.autocompact_pct) {
    compactAt = Math.min(compactAt, Math.floor((effective * compact.autocompact_pct) / 100));
  }
  if (compactAt <= 0) return { error: '上下文窗口过小，留不出回答和自动压缩所需的空间' };
  return {
    compactAt,
    percent: Math.round((compactAt / real) * 100),
    margin: output ? real - compactAt - output : null,
  };
}

// 内测引擎的压缩器在上下文达到「窗口 × 触发比例」时压缩，须与后端 model_settings.jiuwen_trigger_ratio 一致：
// 比例 = (窗口 × 90% − 最大输出) / 窗口，限定在 0.3–0.8，向下取到 0.01
const JIUWEN_TRIGGER_RATIO_MIN = 0.3;
const JIUWEN_TRIGGER_RATIO_MAX = 0.8;

export function jiuwenTriggerRatio(real, output) {
  const ratio = (real * (1 - COMPACT_MARGIN_RATIO) - output) / real;
  const clamped = Math.min(Math.max(ratio, JIUWEN_TRIGGER_RATIO_MIN), JIUWEN_TRIGGER_RATIO_MAX);
  return Math.floor(clamped * 100) / 100;
}

// 内测引擎的自动压缩估算，返回格式同 estimateCompaction；最大输出总有生效值（留空按 .env 或内置默认）
export function estimateJiuwenCompaction(values, defaults) {
  const picked = pickContext(values, defaults);
  if (!picked || picked.error) return picked;
  const { real, output } = picked;
  if (!output) return null;
  const compactAt = Math.floor(real * jiuwenTriggerRatio(real, output));
  return {
    compactAt,
    percent: Math.round((compactAt / real) * 100),
    margin: real - compactAt - output,
  };
}

// 服务端 context_defaults（每项 { value, source, env }）→ 各项留空时的生效值
export function defaultValuesOf(contextDefaults) {
  return Object.fromEntries(
    Object.entries(contextDefaults || {}).map(([name, item]) => [name, item?.value ?? null]),
  );
}

// 填入推荐值：覆盖原模型留下的旧值
export function presetValues(preset) {
  return Object.fromEntries(CONTEXT_FIELD_NAMES.map((name) => [name, preset[name]]));
}

const fmt = (n) => n.toLocaleString();

// 命中的是数据目录下自定义表里的条目时注明，便于看出数值不是内置推荐值
function presetName(preset) {
  return preset.origin === 'custom' ? `${preset.id}（自定义表）` : preset.id;
}

function describePreset(preset) {
  return `上下文窗口 ${fmt(preset.max_context_tokens)}，最大输出 ${fmt(preset.max_output_tokens)}`;
}

export function filledHint(preset) {
  return {
    type: 'success',
    text: `已识别 ${presetName(preset)}，已填入${describePreset(preset)}；可修改，保存后生效`,
  };
}

// 模型名变化后的查表结果 → 是否重填。命中时只在匹配到的模型变了才重填，
// 同一模型继续输入不会冲掉用户手改的值；未命中时清空（原值属于之前的模型），
// 已清空过的继续未命中不再清空，保留用户随后手填的值。返回 null 表示保持现状。
export function presetChange(preset, matchedId) {
  if (!preset) {
    if (matchedId === PRESET_MISSED) return null;
    return {
      matchedId: PRESET_MISSED,
      fill: { ...CLEARED_VALUES },
      hint: { type: 'warning', text: PRESET_MISS_TEXT },
    };
  }
  if (preset.id === matchedId) return null;
  return { matchedId: preset.id, fill: presetValues(preset), hint: filledHint(preset) };
}

// 打开配置页时不自动填，只在已保存模型命中推荐表、且生效值与推荐值不同时提示
export function presetSuggestion(preset, values, defaults) {
  if (!preset) return null;
  const same = CONTEXT_FIELD_NAMES.every(
    (name) => (values?.[name] ?? defaults?.[name] ?? null) === preset[name],
  );
  if (same) return null;
  return { type: 'secondary', text: `推荐表中 ${presetName(preset)}：${describePreset(preset)}`, preset };
}

// 模型名改回已保存的模型、推荐表填入或清空过的值恢复成已保存值时提示，说明输入框里的值来自已保存的配置
// 而非推荐表（推荐表删掉该模型后仍会恢复）；推荐值与已保存的不同时一并给出，可点「填入推荐值」
export function restoredHint(model, preset, saved, defaults) {
  if (!model) return { type: 'success', text: '已恢复为已保存的上下文参数' };
  const text = '已恢复为该模型已保存的上下文参数';
  const suggestion = presetSuggestion(preset, saved, defaults);
  if (suggestion) return { type: 'success', text: `${text}；${suggestion.text}`, preset };
  return { type: 'success', text: `${text}${preset ? '，与推荐表一致' : '（推荐表中没有该模型）'}` };
}
