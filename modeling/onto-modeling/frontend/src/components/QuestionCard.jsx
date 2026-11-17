import { CheckCircleFilled, CheckOutlined } from '@ant-design/icons';
import { Alert, Button, Input } from 'antd';
import { useId, useRef, useState } from 'react';
import { buildQuestionSubmission, isCustomAnswerOption } from '../lib/questionAnswers.js';

export default function QuestionCard({ req, resolved, resolution, onAnswer, onReask, trace = 0 }) {
  const questions = req.tool_input?.questions;
  const [selected, setSelected] = useState({});
  const [custom, setCustom] = useState({});
  const [submitted, setSubmitted] = useState(false);
  const [showValidation, setShowValidation] = useState(false);
  const [submitError, setSubmitError] = useState('');
  const [localResolution, setLocalResolution] = useState(null);
  const submitting = useRef(false);
  const inputs = useRef({});
  const cardId = useId();
  const submission = buildQuestionSubmission(questions, selected, custom);
  const result = localResolution || resolution;
  const expired = result?.behavior === 'expired';
  const preserveDraft = expired && (Object.keys(selected).length > 0 || Object.keys(custom).length > 0);
  const closed = Boolean(localResolution) || resolved;
  const locked = closed || submitted;
  const recordedAnswers = result?.answers;
  const stateLabel = result?.reason === 'timeout' ? '已超时'
    : result?.behavior === 'forwarded' ? '已补发'
    : result?.behavior === 'expired' ? '已失效'
    : result?.behavior === 'deny' ? '已取消'
    : closed ? '已答复' : submitted ? '提交中' : '等待你回答';
  const historyNotice = closed && result?.behavior === 'expired'
    ? `该问题已失效，请让助手重新提问。${preserveDraft ? '当前填写的回答已保留，尚未提交。' : ''}`
    : closed && (!result || result.behavior === 'allow') && !submission.invalid &&
      questions.some((question) => !Object.hasOwn(recordedAnswers || {}, question.question))
      ? '此历史记录未保存完整答案。' : '';

  const runSubmission = async (action) => {
    if (locked || submitting.current) return;
    submitting.current = true;
    setSubmitted(true);
    setSubmitError('');
    try {
      setLocalResolution(await action());
    } catch (error) {
      if (error.status === 410 || error.status === 404) {
        setLocalResolution({ behavior: 'expired' });
      } else {
        setSubmitError(error.message || '提交失败，回答已保留，请重试。');
      }
    } finally {
      setSubmitted(false);
      submitting.current = false;
    }
  };

  const submit = async (event) => {
    event.preventDefault();
    if (locked || submitting.current || submission.invalid) return;
    if (submission.missing.length) {
      setShowValidation(true);
      inputs.current[submission.missing[0]]?.focus();
      return;
    }
    await runSubmission(async () =>
      await onAnswer(req.request_id, submission.answers) || { behavior: 'allow', answers: submission.answers });
  };

  const choose = (index, question, label) => {
    if (locked) return;
    setSelected((previous) => {
      const choices = previous[index] || [];
      const next = question.multiSelect
        ? choices.includes(label) ? choices.filter((value) => value !== label) : [...choices, label]
        : [label];
      return { ...previous, [index]: next };
    });
    if (!question.multiSelect) setCustom((previous) => ({ ...previous, [index]: '' }));
  };

  return (
    <form
      className={`question-card${trace ? ' trace-card' : ''}${closed ? ' resolved' : ''}`}
      aria-labelledby={`${cardId}-title`}
      onSubmit={submit}
      onKeyDown={(event) => {
        if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.nativeEvent.isComposing) {
          event.preventDefault();
          if (!locked && !submission.invalid) event.currentTarget.requestSubmit();
        }
      }}
    >
      <div className="qc-head">
        <span className="qc-glyph">?</span>
        <span id={`${cardId}-title`} className="qc-title">{trace ? '走查分支选择' : '请专家确认'}</span>
        <span className={`qc-state ${locked ? 'done' : 'live'}`} role="status">
          {locked ? <CheckCircleFilled /> : <span className="qc-dot" />}
          {stateLabel}
        </span>
      </div>
      {submission.invalid ? (
        <Alert type="warning" showIcon message="问题格式不完整，无法回答。" description={submission.error} />
      ) : questions.map((question, index) => {
        const options = (question.options || []).filter((option) => !isCustomAnswerOption(option));
        const hasOptions = options.length > 0;
        const invalid = showValidation && submission.missing.includes(index);
        const inputId = `${cardId}-${index}-answer`;
        const questionId = `${cardId}-${index}-question`;
        const recordedAnswer = Object.hasOwn(recordedAnswers || {}, question.question)
          ? recordedAnswers[question.question] : null;
        return (
          <div key={index} className="question-item">
            <div id={questionId} className="question-text">
              {question.header && <span className="qc-topic">{question.header}</span>}
              <span>{question.question}</span>
            </div>
            {hasOptions && (
              <div className="question-options" role="group" aria-labelledby={questionId}>
                {options.map((option, optionIndex) => {
                  const checked = (selected[index] || []).includes(option.label);
                  return (
                    <button
                      type="button"
                      key={optionIndex}
                      aria-pressed={checked}
                      className={`question-option${checked ? ' selected' : ''}`}
                      disabled={locked}
                      style={{ animationDelay: `${Math.min(optionIndex * 45, 220)}ms` }}
                      onClick={() => choose(index, question, option.label)}
                    >
                      <span className="option-index">{optionIndex + 1}</span>
                      <span className="option-main">
                        <span className="option-label">{option.label}</span>
                        {typeof option.description === 'string' && option.description && <span className="option-desc">{option.description}</span>}
                      </span>
                      <span className="option-check"><CheckOutlined /></span>
                    </button>
                  );
                })}
              </div>
            )}
            {closed && !preserveDraft ? recordedAnswer != null && (
              <div className="qc-custom">
                <div className="question-answer">{recordedAnswer}</div>
              </div>
            ) : (
              <div className="qc-custom">
                <label id={`${inputId}-label`} htmlFor={inputId}>
                  {hasOptions ? question.multiSelect ? '其他回答（可与选项一起提交）' : '或填写其他回答' : '填写回答'}
                </label>
                <Input.TextArea
                  id={inputId}
                  name={`answer-${index}`}
                  aria-labelledby={`${questionId} ${inputId}-label`}
                  ref={(input) => { inputs.current[index] = input; }}
                  autoSize={{ minRows: 1, maxRows: 6 }}
                  disabled={locked && !preserveDraft}
                  readOnly={expired}
                  aria-invalid={invalid || undefined}
                  aria-describedby={invalid ? `${inputId}-error` : undefined}
                  value={custom[index] || ''}
                  onChange={(event) => {
                    setCustom((previous) => ({ ...previous, [index]: event.target.value }));
                    if (!question.multiSelect) setSelected((previous) => ({ ...previous, [index]: [] }));
                  }}
                />
                {invalid && <span id={`${inputId}-error`} className="qc-error" role="alert">请选择选项或填写该题回答</span>}
              </div>
            )}
          </div>
        );
      })}
      {historyNotice && <div role="status">{historyNotice}</div>}
      {!closed && (
        <div className="qc-submit">
          {submitError && <span className="qc-error" role="alert">{submitError}</span>}
          {submission.invalid ? (
            <Button size="small" htmlType="button" disabled={locked} onClick={() => runSubmission(async () => {
              await onReask(req.request_id, submission.error);
              return { behavior: 'deny' };
            })}>
              让助手重新提问
            </Button>
          ) : (
            <>
              <span>Ctrl/⌘+Enter 提交，Enter 换行</span>
              <Button size="small" type="primary" shape="round" htmlType="submit" disabled={locked} aria-keyshortcuts="Control+Enter Meta+Enter">
                {submitted ? '已提交' : '提交回答'}
              </Button>
            </>
          )}
        </div>
      )}
    </form>
  );
}
