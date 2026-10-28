import { useEffect, useRef, useState } from 'react';
import { api } from '../lib/api.js';
import {
  PRESET_ABSENT_HINT,
  PRESET_MISSED,
  contextPayload,
  defaultValuesOf,
  filledHint,
  presetChange,
  presetSuggestion,
  presetValues,
  restoredHint,
} from '../lib/contextSettings.js';

// 模型名停止输入后再查推荐表
const PRESET_LOOKUP_DELAY = 400;

// 已保存的模型不在推荐表中，或命中推荐表、且生效值与推荐值不同时的提示
const savedHint = (preset, saved) =>
  saved?.model && !preset
    ? PRESET_ABSENT_HINT
    : presetSuggestion(preset, saved?.context, defaultValuesOf(saved?.context_defaults));

// 按模型名查推荐表、预填上下文参数；两个引擎的配置各用一份。
// saved 为该引擎已保存的配置 { model, context, context_defaults }，modelForm 里有 model 字段
export default function useContextPreset(modelForm, contextForm, saved) {
  // 当前模型名匹配到的条目 id、查表提示、预填后尚未保存的上下文参数
  const [hint, setHint] = useState(null);
  const matchedPresetId = useRef(null);
  const contextPending = useRef(false);
  const lookupTimer = useRef(null);
  const lookupSeq = useRef(0);

  useEffect(() => () => clearTimeout(lookupTimer.current), []);

  // 打开页面不自动填：只记下已保存模型匹配的条目，与推荐值不同时给出提示
  const init = async (data) => {
    const seq = lookupSeq.current;
    if (!data?.model) return;
    let preset;
    try {
      ({ preset } = await api.matchModelContextPreset(data.model));
    } catch {
      return;
    }
    if (seq !== lookupSeq.current) return;
    matchedPresetId.current = preset?.id ?? null;
    setHint(savedHint(preset, data));
  };

  const fill = (preset) => {
    contextForm.setFieldsValue(presetValues(preset));
    contextPending.current = true;
    setHint(filledHint(preset));
  };

  // 模型名改回已保存的模型：推荐表填入或清空过的上下文参数恢复成已保存的值，
  // 避免只想改 Key 时顺手碰了模型名，保存后把已保存的上下文参数冲掉；恢复了就提示，免得误以为是推荐表给的
  const restoreSaved = (model, preset) => {
    const restored = contextPending.current;
    if (restored) {
      contextForm.setFieldsValue(contextPayload(saved?.context));
      contextPending.current = false;
    }
    matchedPresetId.current = preset?.id ?? null;
    setHint(
      restored
        ? restoredHint(model, preset, saved?.context, defaultValuesOf(saved?.context_defaults))
        : savedHint(preset, saved),
    );
  };

  const lookup = (model) => {
    clearTimeout(lookupTimer.current);
    const seq = ++lookupSeq.current;
    const isSavedModel = model === (saved?.model || '').trim();
    if (!model) {
      if (isSavedModel) {
        restoreSaved(model, null);
      } else {
        matchedPresetId.current = null;
        setHint(null);
      }
      return;
    }
    lookupTimer.current = setTimeout(async () => {
      let preset;
      try {
        ({ preset } = await api.matchModelContextPreset(model));
      } catch {
        return;
      }
      if (seq !== lookupSeq.current) return;
      if (isSavedModel) {
        restoreSaved(model, preset);
        return;
      }
      const change = presetChange(preset, matchedPresetId.current);
      if (!change) return;
      matchedPresetId.current = change.matchedId;
      if (change.fill) {
        contextForm.setFieldsValue(change.fill);
        contextPending.current = true;
      }
      setHint(change.hint);
    }, PRESET_LOOKUP_DELAY);
  };

  // 推荐表在弹窗里改过（含停用）或保存后：按输入框里的模型名重新查表，只更新提示、不改输入框里的值；
  // 推荐值与输入框不同时提示，可点「填入推荐值」。未命中时提示表中没有该模型，并保留「已清空过」的状态，
  // 免得随后输入又清空手填的值
  const recheck = async () => {
    clearTimeout(lookupTimer.current);
    const seq = ++lookupSeq.current;
    const model = (modelForm.getFieldValue('model') || '').trim();
    if (!model) return;
    let preset;
    try {
      ({ preset } = await api.matchModelContextPreset(model));
    } catch {
      return;
    }
    if (seq !== lookupSeq.current) return;
    if (preset || matchedPresetId.current !== PRESET_MISSED) matchedPresetId.current = preset?.id ?? null;
    setHint(
      preset
        ? presetSuggestion(preset, contextForm.getFieldsValue(), defaultValuesOf(saved?.context_defaults))
        : PRESET_ABSENT_HINT,
    );
  };

  const afterSave = () => {
    contextPending.current = false;
    setHint(null);
    recheck();
  };

  return { hint, init, fill, lookup, recheck, afterSave };
}
