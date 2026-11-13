export const MAX_NAME_LENGTH = 200;

export function limitProjectName(value) {
  return [...String(value ?? '')].slice(0, MAX_NAME_LENGTH).join('');
}

export function projectNameLengthState(value) {
  const length = [...String(value ?? '')].length;
  return {
    length,
    atLimit: length >= MAX_NAME_LENGTH,
    label: `${length} / ${MAX_NAME_LENGTH}`,
  };
}
