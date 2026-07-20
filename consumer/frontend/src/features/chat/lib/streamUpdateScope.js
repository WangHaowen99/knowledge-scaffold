export const isMessageUpdateScopeCurrent = (updateEpoch, currentEpoch) => (
  Number(updateEpoch) === Number(currentEpoch)
);
