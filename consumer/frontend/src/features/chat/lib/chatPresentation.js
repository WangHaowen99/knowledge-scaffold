export const formatDurationMs = (value) => {
  const milliseconds = Number(value);
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return '';
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  if (milliseconds < 60_000) return `${(milliseconds / 1000).toFixed(milliseconds < 10_000 ? 2 : 1)} s`;
  const minutes = Math.floor(milliseconds / 60_000);
  return `${minutes}m ${Math.round((milliseconds % 60_000) / 1000)}s`;
};
