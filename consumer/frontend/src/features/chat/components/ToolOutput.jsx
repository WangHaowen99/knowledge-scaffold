import { useEffect, useMemo, useRef, useState } from 'react';
import { Braces, ChevronDown, Loader2, Maximize2, RefreshCw, TriangleAlert } from 'lucide-react';

import { authFetch } from '../../../shared/api/auth';
import { formatOutputBytes, resolveToolOutputUrl } from '../lib/toolOutput';
import { CopyButton } from './RunActions';

const parseFullOutput = async (response) => {
  const contentType = response.headers.get('Content-Type') || '';
  if (contentType.includes('application/json')) {
    const payload = await response.json().catch(() => ({}));
    const output = payload.output ?? payload.content ?? payload.data;
    if (typeof output === 'string') return output;
    if (output != null) return JSON.stringify(output, null, 2);
    if (payload.error || payload.detail) throw new Error(payload.error || payload.detail);
    return JSON.stringify(payload, null, 2);
  }
  return response.text();
};

export default function ToolOutput({ meta = {}, fallbackText = '', sessionId = '', runId = '' }) {
  const preview = String(meta.output_preview ?? fallbackText ?? '');
  const outputRef = resolveToolOutputUrl(meta.output_ref, sessionId, runId);
  const truncated = Boolean(meta.output_truncated);
  const outputSize = formatOutputBytes(meta.output_size);
  const [fullOutput, setFullOutput] = useState('');
  const [state, setState] = useState('idle');
  const [error, setError] = useState('');
  const controllerRef = useRef(null);
  const canLoadFull = Boolean(outputRef);

  useEffect(() => () => controllerRef.current?.abort(), []);

  const loadFullOutput = async () => {
    if (!canLoadFull || state === 'loading') return;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setState('loading');
    setError('');
    try {
      const response = await authFetch(outputRef, { signal: controller.signal });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.error || payload.detail || `完整输出加载失败（${response.status}）`);
      }
      const output = await parseFullOutput(response);
      if (controller.signal.aborted) return;
      setFullOutput(output);
      setState('loaded');
    } catch (loadError) {
      if (loadError.name === 'AbortError') return;
      setError(loadError.message || '完整输出加载失败');
      setState('error');
    }
  };

  const displayOutput = state === 'loaded' ? fullOutput : preview;
  const label = useMemo(() => {
    if (state === 'loaded') return `完整输出${outputSize ? ` · ${outputSize}` : ''}`;
    if (truncated) return `输出预览 · 已截断${outputSize ? ` · 完整 ${outputSize}` : ''}`;
    return `输出${outputSize ? ` · ${outputSize}` : ''}`;
  }, [outputSize, state, truncated]);

  if (!displayOutput && !truncated && !canLoadFull) return null;

  return (
    <section className={`tool-output ${truncated && state !== 'loaded' ? 'is-truncated' : ''}`}>
      <div className="tool-output-head">
        <span><Braces size={13} />{label}</span>
        <span className="tool-output-actions">
          <CopyButton text={displayOutput} label={state === 'loaded' ? '复制完整输出' : '复制输出'} />
          {canLoadFull && state !== 'loaded' && (
            <button type="button" onClick={loadFullOutput} disabled={state === 'loading'}>
              {state === 'loading' ? <Loader2 className="spin" size={12} /> : state === 'error' ? <RefreshCw size={12} /> : <Maximize2 size={12} />}
              {state === 'loading' ? '加载中' : state === 'error' ? '重试' : '查看完整输出'}
            </button>
          )}
        </span>
      </div>
      {displayOutput && (
        <details open={state === 'loaded'}>
          <summary><ChevronDown className="tool-output-chevron" size={12} />{state === 'loaded' ? '收起完整输出' : '展开输出预览'}</summary>
          <pre>{displayOutput}</pre>
        </details>
      )}
      {truncated && !canLoadFull && (
        <div className="tool-output-warning" role="status">
          <TriangleAlert size={13} />该历史记录仅保存了截断预览，无法读取完整输出。
        </div>
      )}
      {error && <div className="tool-output-warning" role="alert"><TriangleAlert size={13} />{error}</div>}
    </section>
  );
}
