/**
 * Logging utility with log levels and structured output.
 * Set LOG_LEVEL via environment variable VITE_LOG_LEVEL (debug|info|warn|error)
 */

const LOG_LEVELS = {
  debug: 0,
  info: 1,
  warn: 2,
  error: 3,
};

const currentLevel = LOG_LEVELS[import.meta.env.VITE_LOG_LEVEL] ?? LOG_LEVELS.info;

const formatMessage = (level, context, message, data) => {
  const timestamp = new Date().toISOString();
  const prefix = `[${timestamp}] [${level.toUpperCase()}] [${context}]`;
  return { prefix, message, data };
};

const createLogger = (context) => ({
  debug: (message, data) => {
    if (currentLevel <= LOG_LEVELS.debug) {
      const { prefix } = formatMessage('debug', context, message, data);
      console.debug(`${prefix} ${message}`, data !== undefined ? data : '');
    }
  },

  info: (message, data) => {
    if (currentLevel <= LOG_LEVELS.info) {
      const { prefix } = formatMessage('info', context, message, data);
      console.info(`${prefix} ${message}`, data !== undefined ? data : '');
    }
  },

  warn: (message, data) => {
    if (currentLevel <= LOG_LEVELS.warn) {
      const { prefix } = formatMessage('warn', context, message, data);
      console.warn(`${prefix} ${message}`, data !== undefined ? data : '');
    }
  },

  error: (message, data) => {
    if (currentLevel <= LOG_LEVELS.error) {
      const { prefix } = formatMessage('error', context, message, data);
      console.error(`${prefix} ${message}`, data !== undefined ? data : '');
    }
  },

  // Log SSE events with structured data
  sse: (eventType, data) => {
    if (currentLevel <= LOG_LEVELS.debug) {
      const { prefix } = formatMessage('debug', context, `SSE:${eventType}`, data);
      console.debug(`${prefix} SSE Event: ${eventType}`, data);
    }
  },

  // Log API calls
  api: (method, url, status) => {
    if (currentLevel <= LOG_LEVELS.info) {
      const { prefix } = formatMessage('info', context, 'API', { method, url, status });
      console.info(`${prefix} ${method} ${url} -> ${status ?? 'pending'}`);
    }
  },
});

export default createLogger;
