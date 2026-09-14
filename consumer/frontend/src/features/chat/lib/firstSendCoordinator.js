export const createFirstSendCoordinator = ({
  onPendingChange = () => {},
} = {}) => {
  let activeFlight = null;

  const clearFlight = (token) => {
    if (activeFlight?.token !== token) {
      return;
    }
    activeFlight = null;
    onPendingChange(false);
  };

  return {
    run(operation) {
      if (activeFlight) {
        return { started: false, promise: activeFlight.promise };
      }
      if (typeof operation !== 'function') {
        throw new TypeError('First-send operation must be a function');
      }

      const token = Symbol('first-send');
      const promise = Promise.resolve().then(operation);
      activeFlight = { token, promise };
      onPendingChange(true);
      promise.then(
        () => clearFlight(token),
        () => clearFlight(token),
      );
      return { started: true, promise };
    },

    reset() {
      if (!activeFlight) {
        return;
      }
      activeFlight = null;
      onPendingChange(false);
    },

    isPending() {
      return Boolean(activeFlight);
    },
  };
};

export const isBlankChatSubmitting = (currentChatId, firstSendPending) =>
  !currentChatId && Boolean(firstSendPending);

export const scheduleBlankChatFocus = ({
  getCurrentChatId,
  getInput,
  scheduleFrame,
}) => {
  if (
    typeof getCurrentChatId !== 'function' ||
    typeof getInput !== 'function'
  ) {
    return false;
  }
  if (typeof scheduleFrame !== 'function' || getCurrentChatId()) {
    return false;
  }

  scheduleFrame(() => {
    if (!getCurrentChatId()) {
      getInput()?.focus();
    }
  });
  return true;
};
