// 评测报告、优化建议、完整回答的正文与读取状态（照 develop 的 PackageEvaluationModals / CenterPane 报告预览）
import { useEffect, useState } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { Button, Modal, Note } from '../../ui/index.jsx';
import { ANSWER_LINK, INLINE_PATH_RE, linkifyPaths } from './evaluationText.js';

export const message = (e) => e?.message || errorText(e);


// 报告内容读自评测服务：工作区文件链接在这里不跳转，提示到建模工作区查看
const showFileHint = (path) => toast(`请在建模工作区中查看 ${path}`, 'info');

function FileLink({ path }) {
  return <span className="ev-file" role="button" tabIndex={0} title={path} onClick={() => showFileHint(path)} onKeyDown={(e) => e.key === 'Enter' && showFileHint(path)}>{path.split('/').pop()}</span>;
}

export function Doc({ content, onOpenAnswer }) {
  const components = {
    a: ({ href, children }) => {
      if (href?.startsWith('#file:')) return <FileLink path={decodeURIComponent(href.slice('#file:'.length))} />;
      const answer = ANSWER_LINK.exec(href || '');
      if (answer) {
        const open = () => onOpenAnswer?.(answer[2], answer[1]) || showFileHint(answer[1]);
        return <span className="ev-link" role="button" tabIndex={0} onClick={open} onKeyDown={(e) => e.key === 'Enter' && open()}>{children}</span>;
      }
      return <a href={href} target="_blank" rel="noreferrer noopener">{children}</a>;
    },
    code: ({ className, children }) => {
      const text = String(children || '');
      if (!className && !text.includes('\n') && INLINE_PATH_RE.test(text)) return <FileLink path={text} />;
      return <code className={className}>{children}</code>;
    },
  };
  return <div className="ev-doc"><Markdown remarkPlugins={[remarkGfm]} components={components}>{linkifyPaths(content)}</Markdown></div>;
}

export function useLoad(load, key) {
  const [state, setState] = useState({ loading: !!key, error: '', data: null });
  const [round, setRound] = useState(0);
  useEffect(() => {
    if (!key) return undefined;
    let alive = true;
    setState({ loading: true, error: '', data: null });
    load()
      .then((data) => { if (alive) setState({ loading: false, error: '', data }); })
      .catch((e) => { if (alive) setState({ loading: false, error: message(e), data: null }); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, round]);
  return { ...state, retry: () => setRound((n) => n + 1) };
}

// 读取中转圈；失败给错误与「重试」；有内容才画正文
export function ArtifactBody({ state, onOpenAnswer }) {
  if (state.loading) return <div className="ev-loading"><span className="spin" /></div>;
  if (state.error) {
    return (
      <Note tone="red">
        <span className="row"><span className="grow">{state.error}</span><Button size="xs" kind="ghost" onClick={state.retry}>重试</Button></span>
      </Note>
    );
  }
  if (!state.data?.content) return null;
  return <Doc content={state.data.content} onOpenAnswer={onOpenAnswer} />;
}

// 完整回答预览（develop 的 useAnswerPreview）
export function useAnswerPreview(read, { title = '完整回答', failure = '完整回答读取失败：' } = {}) {
  const [answer, setAnswer] = useState(null);
  const open = (resultId, path) => {
    read(resultId, path).then(setAnswer).catch((e) => toast(`${failure}${message(e)}`, 'alert'));
    return true; // 已处理，不再按文件打开
  };
  const modal = (
    <Modal open={!!answer} onClose={() => setAnswer(null)} title={title} w={860}>
      {answer ? (
        <>
          <div className="ev-answer-meta">
            {answer.originalLength} 字符{answer.truncated ? `，附件仅保留前 ${answer.fullAnswer.length} 字符` : ''}
          </div>
          <Doc content={answer.fullAnswer} />
        </>
      ) : null}
    </Modal>
  );
  return { open, modal };
}

// 工作区报告里的「完整回答」附件（develop CenterPane 的 openReportAnswer）：读报告同目录的 answers.json，
// 弹「原始最终答复」。给报告页原文视图用：onOpenAnswer={attachment.open}，再把 attachment.modal 放进页面
export function useAttachmentAnswer(api, pid) {
  return useAnswerPreview(async (resultId, path) => {
    const file = await api.fileContent(pid, path);
    const payload = JSON.parse(file?.content || '{}');
    const record = (payload.answers || []).find((item) => String(item.resultId || item.result_id || '') === String(resultId));
    if (!record) throw new Error('完整回答证据不存在或不属于当前评测轮次');
    const fullAnswer = String(record.finalAnswer || '');
    return { resultId: String(resultId), question: record.question || '', fullAnswer, originalLength: fullAnswer.length };
  }, { title: '原始最终答复', failure: '' });
}
