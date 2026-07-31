export function createProjectEventReplayGate(replayThrough = 0) {
  const rawBoundary = Number(replayThrough);
  const boundary = Number.isSafeInteger(rawBoundary) && rawBoundary > 0 ? rawBoundary : 0;
  return {
    replayThrough: boundary,
    replaying: boundary > 0,
    pendingRefresh: false,
  };
}

export function advanceProjectEventReplayGate(gate, event) {
  const rawSequence = Number(event?.seq);
  const hasSequence = Number.isSafeInteger(rawSequence) && rawSequence >= 0;
  const historical = Boolean(gate.replaying && hasSequence && rawSequence <= gate.replayThrough);
  let pendingRefresh = gate.pendingRefresh || historical;
  let replaying = gate.replaying;
  let refresh = false;

  if (replaying && hasSequence && rawSequence >= gate.replayThrough) {
    replaying = false;
    refresh = pendingRefresh || !historical;
    pendingRefresh = false;
  } else if (!replaying) {
    refresh = true;
  }

  return {
    gate: { ...gate, replaying, pendingRefresh },
    historical,
    refresh,
  };
}
