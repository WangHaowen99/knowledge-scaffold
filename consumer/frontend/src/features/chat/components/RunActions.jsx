import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Check,
  Clipboard,
  Loader2,
  RefreshCw,
  ThumbsDown,
  ThumbsUp,
  TriangleAlert,
} from 'lucide-react';

import { authFetch } from '../../../shared/api/auth';
import { writeClipboardText } from '../lib/clipboard';

export function CopyButton({ text, label = '复制', className = '' }) {
  const [state, setState] = useState('idle');
  const [error, setError] = useState('');
  const resetTimerRef = useRef(null);

  useEffect(() => () => clearTimeout(resetTimerRef.current), []);

  const copy = useCallback(async () => {
    clearTimeout(resetTimerRef.current);
    const result = await writeClipboardText(text);
    if (!result.ok) {
      setState('error');
      setError(result.error);
      return;
    }
    setState('copied');
    setError('');
    resetTimerRef.current = setTimeout(() => setState('idle'), 1800);
  }, [text]);

  return (
    <span className={`chat-copy-action ${className}`.trim()}>
      <button
        type="button"
        onClick={copy}
        disabled={!String(text ?? '')}
        aria-label={state === 'copied' ? '已复制' : label}
        title={error || label}
      >
        {state === 'copied' ? (
          <Check size={13} />
        ) : state === 'error' ? (
          <TriangleAlert size={13} />
        ) : (
          <Clipboard size={13} />
        )}
        <span>
          {state === 'copied' ?
            '已复制' :
            state === 'error' ?
              '复制失败' :
              label}
        </span>
      </button>
      {state === 'error' && (
        <span className="chat-action-error" role="status">
          {error}
        </span>
      )}
    </span>
  );
}

export function RunFeedback({ runId, status = 'finished' }) {
  const [choice, setChoice] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [lastAttempt, setLastAttempt] = useState(null);
  const isCompleted = ['finished', 'completed', 'success'].includes(
    String(status || '').toLowerCase(),
  );

  const submit = useCallback(
    async (requestedChoice) => {
      if (!runId || submitting) {
        return;
      }
      const previousChoice = choice;
      const nextChoice = requestedChoice === choice ? null : requestedChoice;
      const vote = nextChoice || 'none';
      setChoice(nextChoice);
      setSubmitting(true);
      setError('');
      setLastAttempt(requestedChoice);
      try {
        const response = await authFetch(
          `/agentic/runs/${encodeURIComponent(runId)}/feedback`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ vote }),
          },
        );
        if (!response.ok) {
          const payload = await response.json().catch(() => ({}));
          throw new Error(
            payload.error ||
              payload.detail ||
              `反馈提交失败（${response.status}）`,
          );
        }
        setLastAttempt(null);
      } catch (submitError) {
        setChoice(previousChoice);
        setError(submitError.message || '反馈提交失败');
      } finally {
        setSubmitting(false);
      }
    },
    [choice, runId, submitting],
  );

  if (!runId || !isCompleted) {
    return null;
  }

  return (
    <span className="chat-run-feedback" aria-label="本轮反馈">
      <button
        type="button"
        aria-label="有帮助"
        aria-pressed={choice === 'up'}
        className={choice === 'up' ? 'active positive' : ''}
        disabled={submitting}
        onClick={() => submit('up')}
        title="本轮回答有帮助"
      >
        <ThumbsUp size={13} />
      </button>
      <button
        type="button"
        aria-label="待改进"
        aria-pressed={choice === 'down'}
        className={choice === 'down' ? 'active negative' : ''}
        disabled={submitting}
        onClick={() => submit('down')}
        title="本轮回答需要改进"
      >
        <ThumbsDown size={13} />
      </button>
      {submitting && (
        <Loader2 className="spin" size={12} aria-label="正在提交反馈" />
      )}
      {error && (
        <span className="chat-action-error" role="alert">
          {error}
          <button
            type="button"
            disabled={submitting}
            onClick={() => submit(lastAttempt || 'up')}
          >
            <RefreshCw size={11} />
            重试
          </button>
        </span>
      )}
    </span>
  );
}
