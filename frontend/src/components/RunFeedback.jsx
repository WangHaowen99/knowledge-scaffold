import { useCallback, useState } from 'react';
import { ThumbsUp, ThumbsDown, Check, Loader2 } from 'lucide-react';
import { authFetch } from '../utils/auth';

/**
 * Per-run feedback widget — thumbs up/down only.
 *
 * Votes adjust active_endorsements / rejection_count for fingerprints injected
 * into this turn via near_point_recall. New proposals come from
 * `memory` (active chat) or reflection (passive); this widget
 * only signals whether the recalled context helped.
 *
 * Props:
 * - runId: string — required to target the run
 * - status: 'finished' | 'completed' | other — only renders for terminal ok runs
 */
function RunFeedback({ runId, status }) {
  const [submitting, setSubmitting] = useState(false);
  const [choice, setChoice] = useState(null); // 'up' | 'down'
  const [sent, setSent] = useState(false);
  const [error, setError] = useState(null);

  const submit = useCallback(async (nextChoice) => {
    if (!runId || submitting) return;
    const vote = nextChoice === choice ? 'none' : nextChoice;
    setSubmitting(true);
    setError(null);
    try {
      const res = await authFetch(`/agentic/runs/${encodeURIComponent(runId)}/feedback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ vote }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `HTTP ${res.status}`);
      }
      setChoice(vote === 'none' ? null : vote);
      setSent(true);
    } catch (e) {
      setError(e.message || '提交失败');
    } finally {
      setSubmitting(false);
    }
  }, [choice, runId, submitting]);

  const isCompleted = status === 'finished' || status === 'completed';
  if (!runId || !isCompleted) {
    return null;
  }

  return (
    <div className="run-feedback">
      <button
        type="button"
        className={`run-feedback-btn ${choice === 'up' ? 'active' : ''}`}
        disabled={submitting}
        onClick={() => submit('up')}
        title="本轮的提示/记忆有帮助"
        aria-label="thumbs up"
      >
        <ThumbsUp size={14} />
      </button>
      <button
        type="button"
        className={`run-feedback-btn ${choice === 'down' ? 'active' : ''}`}
        disabled={submitting}
        onClick={() => submit('down')}
        title="本轮的提示/记忆没用或有误"
        aria-label="thumbs down"
      >
        <ThumbsDown size={14} />
      </button>
      {submitting && <Loader2 className="spin" size={12} />}
      {sent && !submitting && (
        <span className="run-feedback-sent">
          <Check size={12} />
          {choice === 'up' ? '有帮助' : choice === 'down' ? '待改进' : '已撤回'}
        </span>
      )}
      {error && <span className="run-feedback-error">{error}</span>}
    </div>
  );
}

export default RunFeedback;
