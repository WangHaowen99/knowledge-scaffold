export default function ChatPromptInput({
  inputRef,
  value,
  isSubmitting,
  onChange,
  onSend,
}) {
  return (
    <input
      className="ti-input"
      ref={inputRef}
      value={value}
      aria-label="对话内容"
      placeholder="输入问题，Enter 发送…"
      readOnly={isSubmitting}
      onChange={onChange}
      onKeyDown={(event) => {
        if (event.key === 'Enter' && !event.nativeEvent.isComposing) onSend();
      }}
    />
  );
}
