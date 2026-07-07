export function getLocalObservabilityRuntime(config) {
  const observability = config?.observability || {};
  const enabled = observability.enabled === true;
  const mode = String(observability.mode || 'local').toLowerCase() === 'langfuse' ? 'langfuse' : 'local';
  const isWritingLocal = enabled && mode === 'local';

  if (isWritingLocal) {
    return {
      loaded: true,
      enabled,
      mode,
      isWritingLocal,
      label: '当前正在写入本地观测日志',
      detail: '新运行完成后会进入本地观测分析。',
      tone: 'success',
    };
  }

  if (!enabled) {
    return {
      loaded: true,
      enabled,
      mode,
      isWritingLocal,
      label: '当前未开启观测',
      detail: '本页仍可分析历史本地日志；新运行不会写入本地观测数据。',
      tone: 'muted',
    };
  }

  return {
    loaded: true,
    enabled,
    mode,
    isWritingLocal,
    label: '当前观测写入 Langfuse',
    detail: '本页仅分析历史本地日志；新运行不会写入本地日志。',
    tone: 'warning',
  };
}

export function getUnknownLocalObservabilityRuntime() {
  return {
    loaded: false,
    enabled: null,
    mode: '',
    isWritingLocal: false,
    label: '正在读取观测配置',
    detail: '配置读取完成后会显示当前写入目标。',
    tone: 'muted',
  };
}

export function getSessionPerformanceEntryState({
  hasLocalData,
  isWritingLocal,
  configLoaded,
  loading,
}) {
  if (loading) {
    return {
      disabled: false,
      tone: 'loading',
      label: '性能分析',
      title: '正在检查当前会话是否有本地观测数据',
    };
  }

  if (hasLocalData) {
    return {
      disabled: false,
      tone: 'ready',
      label: '性能分析',
      title: '查看当前会话已有的本地性能分析',
    };
  }

  if (isWritingLocal || !configLoaded) {
    return {
      disabled: false,
      tone: 'empty-local',
      label: '性能分析',
      title: '当前会话暂无本地观测数据，运行后可刷新查看',
    };
  }

  return {
    disabled: true,
    tone: 'disabled',
    label: '暂无性能数据',
    title: '当前会话暂无本地观测数据，且当前未写入本地观测日志',
  };
}
