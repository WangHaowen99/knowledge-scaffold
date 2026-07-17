export const streamStatusForAdmission = (runStatus) => (
  String(runStatus || '').trim().toLowerCase() === 'queued' ? 'queued' : 'reconnecting'
);

export const streamStatusForConnection = (reconnectAttempts = 0) => (
  Number(reconnectAttempts) > 0 ? 'reconnecting' : 'streaming'
);

export const streamStatusForTerminal = (runStatus) => {
  const normalized = String(runStatus || '').trim().toLowerCase();
  if (normalized === 'awaiting_input' || normalized === 'awaiting') return 'awaiting';
  if (normalized === 'cancelled' || normalized === 'canceled') return 'cancelled';
  if (normalized === 'error' || normalized === 'failed') return 'error';
  return 'finished';
};

export const streamStatusForError = (error) => (
  error?.name === 'AbortError' ? 'cancelled' : 'error'
);
