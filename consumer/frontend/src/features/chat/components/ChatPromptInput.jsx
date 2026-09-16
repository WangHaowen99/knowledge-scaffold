// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import { useLayoutEffect } from 'react';

const MAX_PROMPT_HEIGHT = 240;

export default function ChatPromptInput({
  inputRef,
  value,
  isSubmitting,
  onChange,
  onSend,
}) {
  useLayoutEffect(() => {
    const element = inputRef.current;
    if (!element) {
      return;
    }

    element.style.height = 'auto';
    if (element.scrollHeight <= 0) {
      return;
    }
    element.style.height = `${Math.min(element.scrollHeight, MAX_PROMPT_HEIGHT)}px`;
    element.style.overflowY =
      element.scrollHeight > MAX_PROMPT_HEIGHT ? 'auto' : 'hidden';
  }, [inputRef, value]);

  return (
    <textarea
      className="ti-input"
      ref={inputRef}
      value={value}
      rows={1}
      aria-label="对话内容"
      placeholder="输入问题，Enter 发送…"
      readOnly={isSubmitting}
      onChange={onChange}
      onKeyDown={(event) => {
        if (
          event.key !== 'Enter' ||
          event.shiftKey ||
          event.nativeEvent.isComposing
        ) {
          return;
        }
        event.preventDefault();
        onSend();
      }}
    />
  );
}
