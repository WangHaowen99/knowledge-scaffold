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

export const messageStatusForTerminalRun = (runStatus) => {
  const normalized = String(runStatus || '').trim().toLowerCase();
  if (normalized === 'awaiting' || normalized === 'awaiting_input') return 'awaiting';
  if (normalized === 'cancelled' || normalized === 'canceled') return 'cancelled';
  if (normalized === 'error' || normalized === 'failed') return 'error';
  return 'finished';
};

const TERMINAL_RUN_STATUSES = new Set([
  'success', 'completed', 'finished', 'error', 'failed',
  'cancelled', 'canceled', 'awaiting', 'awaiting_input',
]);

const silentAbortControllers = new WeakSet();

export const isTerminalRunStatus = (status) => (
  TERMINAL_RUN_STATUSES.has(String(status || '').trim().toLowerCase())
);

export const abortStreamController = (controller, { silent = false } = {}) => {
  if (!controller) return;
  if (silent) silentAbortControllers.add(controller);
  controller.abort();
};

export const abortStreamControllers = (controllers, options = {}) => {
  controllers?.forEach((controller) => abortStreamController(controller, options));
};

export const isSilentStreamAbort = (controller) => (
  Boolean(controller && silentAbortControllers.has(controller))
);
