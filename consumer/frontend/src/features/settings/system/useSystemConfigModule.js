import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react';

import { saveSystemConfigSection, testSystemConfigSection } from './api';
import {
  configValuesEqual,
  createSystemConfigModuleState,
  reduceSystemConfigModuleState,
} from './model';

const identityPayload = (data) => data;

export function useSystemConfigModule({
  section,
  initialData = {},
  readOnly = false,
  request,
  onSaved,
  prepareData = identityPayload,
}) {
  const [state, dispatch] = useReducer(
    reduceSystemConfigModuleState,
    undefined,
    () => createSystemConfigModuleState(initialData, readOnly),
  );
  const sourceSignature = useMemo(() => JSON.stringify(initialData ?? {}), [initialData]);
  const previousSource = useRef({ section, sourceSignature });
  const inFlightAction = useRef('');

  useEffect(() => {
    const previous = previousSource.current;
    if (previous.section === section && previous.sourceSignature === sourceSignature) return;
    previousSource.current = { section, sourceSignature };
    dispatch({ type: 'SOURCE_CHANGED', data: initialData, readOnly });
  }, [initialData, readOnly, section, sourceSignature]);

  useEffect(() => {
    dispatch({ type: 'READ_ONLY_CHANGED', value: readOnly });
  }, [readOnly]);

  const patchDraft = useCallback((data) => {
    dispatch({ type: 'PATCH_DRAFT', data });
  }, []);

  const replaceDraft = useCallback((data) => {
    dispatch({ type: 'REPLACE_DRAFT', data });
  }, []);

  const resetDraft = useCallback(() => {
    dispatch({ type: 'RESET_DRAFT' });
  }, []);

  const save = useCallback(async (data = state.draft) => {
    if (state.readOnly || inFlightAction.current) return null;
    inFlightAction.current = 'save';
    dispatch({ type: 'BEGIN_ACTION', action: 'save' });
    try {
      const payload = await saveSystemConfigSection(section, prepareData(data, state.baseline), request);
      const saved = payload?.config || {};
      // Parent panels echo the saved public config back through initialData. Mark that
      // source as already consumed so the echo cannot immediately clear save feedback.
      previousSource.current = { section, sourceSignature: JSON.stringify(saved) };
      dispatch({ type: 'SAVE_SUCCEEDED', data: saved, message: '配置已保存' });
      onSaved?.(saved, payload);
      return payload;
    } catch (error) {
      dispatch({ type: 'ACTION_FAILED', error: error?.message || '保存系统配置失败' });
      return null;
    } finally {
      inFlightAction.current = '';
    }
  }, [onSaved, prepareData, request, section, state.baseline, state.draft, state.readOnly]);

  const testConnection = useCallback(async (extraData = {}) => {
    if (state.readOnly || inFlightAction.current) return null;
    inFlightAction.current = 'test';
    dispatch({ type: 'BEGIN_ACTION', action: 'test' });
    try {
      const testData = { ...prepareData(state.draft, state.baseline), ...extraData };
      const payload = await testSystemConfigSection(section, testData, request);
      dispatch({ type: 'TEST_FINISHED', result: payload });
      return payload;
    } catch (error) {
      dispatch({ type: 'ACTION_FAILED', error: error?.message || '测试系统配置失败' });
      return null;
    } finally {
      inFlightAction.current = '';
    }
  }, [prepareData, request, section, state.baseline, state.draft, state.readOnly]);

  const isDirty = useMemo(() => {
    try {
      return !configValuesEqual(
        prepareData(state.draft, state.baseline),
        prepareData(state.baseline, state.baseline),
      );
    } catch {
      return true;
    }
  }, [prepareData, state.baseline, state.draft]);

  return {
    ...state,
    isDirty,
    patchDraft,
    replaceDraft,
    resetDraft,
    save,
    testConnection,
  };
}
