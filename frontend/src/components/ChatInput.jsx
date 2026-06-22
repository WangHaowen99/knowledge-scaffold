/**
 * Chat input component with send button.
 */

import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { Bot, Check, ChevronDown, Loader2, Send, X } from 'lucide-react';

import { buildOntologySlashState, getInputAfterOntologyQuickSelect } from '../utils/ontologyCommand';

const AGENT_MODE_OPTIONS = [
  { value: 'quick', label: '快速模式', description: '模型优先快速返回结果' },
  { value: 'thinking', label: '思考模式', description: '模型会输出推理过程，响应更全面但可能更慢' },
  { value: 'multi_agent', label: '多智能体模式', description: '启用深度思考与多智能体协作，适合复杂任务' },
];

function ChatInput({
  input,
  setInput,
  isLoading,
  onSend,
  onStop,
  agentMode = 'quick',
  onChangeAgentMode = null,
  modelProfiles = [],
  selectedModelProfileId = '',
  onChangeModelProfile = null,
  modeMenuPlacement = 'top',
  ontologyOptions = [],
  lockedOntology = null,
  onSelectOntology = null,
  onClearOntology = null,
  disabled = false,
}) {
  const normalizedInput = String(input || '');
  const textareaRef = useRef(null);
  const optionRefs = useRef([]);
  const slashListboxId = useId();
  const [activeOntologyIndex, setActiveOntologyIndex] = useState(0);
  const [dismissedSlashValue, setDismissedSlashValue] = useState('');
  const slashState = useMemo(
    () => buildOntologySlashState(normalizedInput, ontologyOptions, 8),
    [normalizedInput, ontologyOptions]
  );
  const matchedOntologies = slashState.matchedOntologies;
  const showSlashPanel = slashState.isSlashMode && dismissedSlashValue !== normalizedInput;
  const effectiveActiveOntologyIndex = matchedOntologies.length > 0
    ? Math.min(activeOntologyIndex, matchedOntologies.length - 1)
    : 0;
  const activeOntology = matchedOntologies.length > 0
    ? matchedOntologies[effectiveActiveOntologyIndex]
    : null;
  const canSend = normalizedInput.trim().length > 0 || Boolean(activeOntology && showSlashPanel);
  const [modeDropdownOpen, setModeDropdownOpen] = useState(false);
  const [modelDropdownOpen, setModelDropdownOpen] = useState(false);
  const modeDropdownRef = useRef(null);
  const modelDropdownRef = useRef(null);
  const currentModeOption = AGENT_MODE_OPTIONS.find((opt) => opt.value === agentMode) || AGENT_MODE_OPTIONS[0];
  const currentModelProfile = modelProfiles.find((profile) => profile.id === selectedModelProfileId)
    || modelProfiles.find((profile) => profile.is_default)
    || modelProfiles[0]
    || null;

  useEffect(() => {
    if (!modeDropdownOpen) return;
    const handleClickOutside = (e) => {
      if (modeDropdownRef.current && !modeDropdownRef.current.contains(e.target)) {
        setModeDropdownOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [modeDropdownOpen]);

  useEffect(() => {
    if (!modelDropdownOpen) return;
    const handleClickOutside = (e) => {
      if (modelDropdownRef.current && !modelDropdownRef.current.contains(e.target)) {
        setModelDropdownOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [modelDropdownOpen]);

  useEffect(() => {
    if (!showSlashPanel || !activeOntology) return;
    optionRefs.current[effectiveActiveOntologyIndex]?.scrollIntoView({ block: 'nearest' });
  }, [activeOntology, effectiveActiveOntologyIndex, showSlashPanel]);

  const handleOntologyQuickSelect = (ontology) => {
    if (!ontology || !ontology.id || !onSelectOntology) return;
    onSelectOntology(ontology, { source: 'slash' });
    setInput(getInputAfterOntologyQuickSelect(normalizedInput));
    setDismissedSlashValue('');

    window.requestAnimationFrame(() => {
      textareaRef.current?.focus();
    });
  };

  const handleKeyDown = (e) => {
    if (e.nativeEvent.isComposing) return;

    if (showSlashPanel) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (matchedOntologies.length > 0) {
          setActiveOntologyIndex((currentIndex) => (currentIndex + 1) % matchedOntologies.length);
        }
        return;
      }

      if (e.key === 'ArrowUp') {
        e.preventDefault();
        if (matchedOntologies.length > 0) {
          setActiveOntologyIndex(
            (currentIndex) => (currentIndex - 1 + matchedOntologies.length) % matchedOntologies.length
          );
        }
        return;
      }

      if (e.key === 'Escape') {
        e.preventDefault();
        setDismissedSlashValue(normalizedInput);
        return;
      }

      if ((e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) && activeOntology) {
        e.preventDefault();
        handleOntologyQuickSelect(activeOntology);
        return;
      }

      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        return;
      }
    }

    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      onSend();
    }
  };

  const handleInputChange = (event) => {
    const nextValue = event.target.value;
    const nextSlashState = buildOntologySlashState(nextValue, ontologyOptions, 8);
    const currentLookupKey = `${slashState.source}:${slashState.lookupTerm}`;
    const nextLookupKey = `${nextSlashState.source}:${nextSlashState.lookupTerm}`;

    if (currentLookupKey !== nextLookupKey) {
      setActiveOntologyIndex(0);
    }
    if (dismissedSlashValue && nextValue !== dismissedSlashValue) {
      setDismissedSlashValue('');
    }
    setInput(nextValue);
  };

  const handlePrimaryAction = () => {
    if (isLoading) {
      onStop?.();
      return;
    }

    if (showSlashPanel && activeOntology) {
      handleOntologyQuickSelect(activeOntology);
      return;
    }

    onSend();
  };

  const handleAgentModeSelect = (mode) => {
    onChangeAgentMode?.(mode);
    setModeDropdownOpen(false);
    window.requestAnimationFrame(() => {
      textareaRef.current?.focus();
    });
  };

  const handleModelProfileSelect = (profileId) => {
    onChangeModelProfile?.(profileId);
    setModelDropdownOpen(false);
    window.requestAnimationFrame(() => {
      textareaRef.current?.focus();
    });
  };

  const handleModeSelectorKeyDown = (event) => {
    if (event.key === 'Escape') {
      setModeDropdownOpen(false);
      return;
    }

    if ((event.key === 'Enter' || event.key === ' ') && event.target === event.currentTarget) {
      event.preventDefault();
      if (!isLoading && onChangeAgentMode) {
        setModeDropdownOpen((prev) => !prev);
      }
    }
  };

  const renderAgentModeSelector = () => (
    <div className="action-chips input-setting-chips">
      <div
        className={`agent-mode-dropdown placement-${modeMenuPlacement}`}
        ref={modeDropdownRef}
        onKeyDown={handleModeSelectorKeyDown}
      >
        <button
          type="button"
          className={`action-chip agent-mode-trigger mode-${agentMode}`}
          onClick={() => !isLoading && onChangeAgentMode && setModeDropdownOpen((prev) => !prev)}
          disabled={!onChangeAgentMode || isLoading}
          title={currentModeOption.description}
          aria-haspopup="listbox"
          aria-expanded={modeDropdownOpen}
        >
          {currentModeOption.label}
          <ChevronDown size={14} className={`agent-mode-chevron ${modeDropdownOpen ? 'open' : ''}`} />
        </button>
        {modeDropdownOpen && (
          <div className="agent-mode-menu" role="listbox" aria-label="选择智能体模式">
            {AGENT_MODE_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                type="button"
                role="option"
                aria-selected={opt.value === agentMode}
                className={`agent-mode-option ${opt.value === agentMode ? 'selected' : ''}`}
                onClick={() => handleAgentModeSelect(opt.value)}
              >
                <span className="agent-mode-option-copy">
                  <span className="agent-mode-option-label">{opt.label}</span>
                  <span className="agent-mode-option-desc">{opt.description}</span>
                </span>
                {opt.value === agentMode && <Check size={16} className="agent-mode-option-check" />}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );

  const renderModelProfileSelector = () => {
    if (!Array.isArray(modelProfiles) || modelProfiles.length === 0) return null;
    const label = currentModelProfile?.name || currentModelProfile?.model || '默认模型';
    return (
      <div className="action-chips input-setting-chips">
        <div
          className={`agent-mode-dropdown model-profile-dropdown placement-${modeMenuPlacement}`}
          ref={modelDropdownRef}
        >
          <button
            type="button"
            className="action-chip agent-mode-trigger model-profile-trigger"
            onClick={() => !isLoading && onChangeModelProfile && setModelDropdownOpen((prev) => !prev)}
            disabled={!onChangeModelProfile || isLoading}
            title={currentModelProfile?.model || label}
            aria-haspopup="listbox"
            aria-expanded={modelDropdownOpen}
          >
            <Bot size={14} />
            <span className="model-profile-trigger-label">{label}</span>
            <ChevronDown size={14} className={`agent-mode-chevron ${modelDropdownOpen ? 'open' : ''}`} />
          </button>
          {modelDropdownOpen && (
            <div className="agent-mode-menu model-profile-menu" role="listbox" aria-label="选择模型">
              {modelProfiles.map((profile) => (
                <button
                  key={profile.id}
                  type="button"
                  role="option"
                  aria-selected={profile.id === currentModelProfile?.id}
                  className={`agent-mode-option ${profile.id === currentModelProfile?.id ? 'selected' : ''}`}
                  onClick={() => handleModelProfileSelect(profile.id)}
                >
                  <span className="agent-mode-option-copy">
                    <span className="agent-mode-option-label">
                      {profile.name || profile.model || profile.id}
                      {profile.is_default ? <span className="model-profile-default-mark">默认</span> : null}
                    </span>
                    <span className="agent-mode-option-desc">
                      {profile.model || profile.id} · {profile.provider || 'openai_compatible'}
                    </span>
                  </span>
                  {profile.id === currentModelProfile?.id && <Check size={16} className="agent-mode-option-check" />}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    );
  };

  const renderOntologyLock = () => {
    if (!lockedOntology?.id) return null;

    return (
      <div className="ontology-lock-chip" title={`当前本轮锁定本体：${lockedOntology.name || lockedOntology.id}`}>
        <span className="ontology-lock-label">本轮本体</span>
        <span className="ontology-lock-value">{lockedOntology.name || lockedOntology.id}</span>
        <button
          type="button"
          className="ontology-lock-clear"
          onClick={onClearOntology || undefined}
          disabled={!onClearOntology || isLoading}
          aria-label="清除本体锁定"
        >
          <X size={14} />
        </button>
      </div>
    );
  };

  return (
    <div className="input-stack">
      {showSlashPanel && (
        <div className="slash-ontology-panel">
          <div className="slash-ontology-title">
            输入 <code>/</code> 或 <code>/onto</code> 搜索本体，<code>↑↓</code> 选择，<code>Tab</code>/<code>Enter</code> 确认
          </div>
          <div className="slash-ontology-list" id={slashListboxId} role="listbox">
            {matchedOntologies.length > 0 ? matchedOntologies.map((ontology, index) => (
              <button
                key={ontology.id}
                type="button"
                id={`${slashListboxId}-option-${index}`}
                ref={(element) => {
                  optionRefs.current[index] = element;
                }}
                className={`slash-ontology-item ${index === effectiveActiveOntologyIndex ? 'active' : ''}`}
                onClick={() => handleOntologyQuickSelect(ontology)}
                onMouseDown={(event) => event.preventDefault()}
                onMouseEnter={() => setActiveOntologyIndex(index)}
                aria-selected={index === effectiveActiveOntologyIndex}
                role="option"
              >
                <span className="slash-ontology-item-name">{ontology.name || ontology.id}</span>
                <span className="slash-ontology-item-id">{ontology.id}</span>
              </button>
            )) : (
              <div className="slash-ontology-empty">未找到匹配本体，请继续输入更完整的 ID 或名称</div>
            )}
          </div>
        </div>
      )}
      <div className="input-box">
        <div className="ontology-lock-row">{renderOntologyLock()}</div>
        <textarea
          ref={textareaRef}
          className="input"
          placeholder="输入你的问题或任务..."
          value={input}
          onChange={handleInputChange}
          onKeyDown={handleKeyDown}
          disabled={isLoading || disabled}
          rows={1}
          aria-expanded={showSlashPanel}
          aria-controls={showSlashPanel ? slashListboxId : undefined}
          aria-activedescendant={
            showSlashPanel && activeOntology
              ? `${slashListboxId}-option-${effectiveActiveOntologyIndex}`
              : undefined
          }
        />
        <div className="input-actions">
          <div className="input-settings-group">
            {renderModelProfileSelector()}
            {renderAgentModeSelector()}
          </div>
          <button
            className={`send ${isLoading ? 'loading' : ''}`}
            onClick={handlePrimaryAction}
            disabled={disabled || (!isLoading && !canSend)}
            type="button"
            title={isLoading ? 'Stop' : (showSlashPanel && activeOntology ? '选择本体' : 'Send')}
          >
            {isLoading ? (
              <Loader2 className="spin" size={30} />
            ) : (
              <Send size={30} />
            )}
          </button>
        </div>
      </div>
    </div>
  );
}

export default ChatInput;
