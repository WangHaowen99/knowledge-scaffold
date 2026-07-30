function text(value) {
  return typeof value === 'string' ? value : '';
}

function sameStream(state, payload) {
  return state && state.streamId === text(payload?.streamId);
}

export function advanceDiagramEventCursor(cursor, event) {
  const current = Number(cursor);
  const safeCurrent = Number.isSafeInteger(current) && current >= 0 ? current : 0;
  const rawSequence = event?.seq;
  if (rawSequence === null || rawSequence === undefined || rawSequence === '') return safeCurrent;
  const sequence = Number(rawSequence);
  return Number.isSafeInteger(sequence) && sequence >= 0
    ? Math.max(safeCurrent, sequence)
    : safeCurrent;
}

export function createProjectEventReplayGate(replayThrough = 0) {
  const rawBoundary = Number(replayThrough);
  const boundary = Number.isSafeInteger(rawBoundary) && rawBoundary > 0 ? rawBoundary : 0;
  return {
    replayThrough: boundary,
    replaying: boundary > 0,
    pendingRefresh: false,
  };
}

export function advanceProjectEventReplayGate(gate, event, { diagramEvent = false } = {}) {
  const rawSequence = Number(event?.seq);
  const hasSequence = Number.isSafeInteger(rawSequence) && rawSequence >= 0;
  const historical = Boolean(gate.replaying && hasSequence && rawSequence <= gate.replayThrough);
  let pendingRefresh = gate.pendingRefresh || (historical && !diagramEvent);
  let replaying = gate.replaying;
  let refresh = false;

  if (replaying && hasSequence && rawSequence >= gate.replayThrough) {
    replaying = false;
    refresh = pendingRefresh || (!historical && !diagramEvent);
    pendingRefresh = false;
  } else if (!replaying && !diagramEvent) {
    refresh = true;
  }

  return {
    gate: { ...gate, replaying, pendingRefresh },
    historical,
    refresh,
  };
}

export function reduceDiagramStream(state, event) {
  const payload = event?.payload ?? {};
  if (event?.type === 'process.diagram.stream.started') {
    const streamId = text(payload.streamId);
    if (!streamId) return state;
    return {
      streamId,
      status: 'streaming',
      phase: text(payload.phase) || 'drafting',
      message: text(payload.message) || '正在整理流程草稿',
      activePath: '',
      files: [],
    };
  }
  if (!sameStream(state, payload)) return state;
  if (event.type === 'process.diagram.stream.file_started') {
    const path = text(payload.path);
    if (!path) return state;
    const current = state.files.find((file) => file.path === path);
    return {
      ...state,
      status: 'streaming',
      phase: 'rendering',
      activePath: path,
      files: current
        ? state.files.map((file) => file.path === path
          ? { ...file, title: text(payload.title) || file.title }
          : file)
        : [...state.files, { path, title: text(payload.title) || path, content: '' }],
    };
  }
  if (event.type === 'process.diagram.stream.delta') {
    const path = text(payload.path);
    const chunk = text(payload.chunk);
    if (!path || !chunk) return state;
    const current = state.files.find((file) => file.path === path);
    const next = current
      ? state.files.map((file) => file.path === path
        ? { ...file, content: `${file.content}${chunk}` }
        : file)
      : [...state.files, { path, title: text(payload.title) || path, content: chunk }];
    return { ...state, status: 'streaming', phase: 'rendering', activePath: path, files: next };
  }
  if (event.type === 'process.diagram.stream.completed') {
    return { ...state, status: 'completed', phase: 'completed' };
  }
  if (event.type === 'process.diagram.stream.failed') {
    return {
      ...state,
      status: 'failed',
      phase: 'failed',
      message: text(payload.message) || '流程图生成失败',
    };
  }
  return state;
}
