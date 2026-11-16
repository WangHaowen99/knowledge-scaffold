import { CheckCircleFilled, CheckOutlined } from '@ant-design/icons';
import { Alert, Button, Input } from 'antd';
import { useId, useRef, useState } from 'react';
import { buildQuestionSubmission, isCustomAnswerOption } from '../lib/questionAnswers.js';

export default function QuestionCard({ req, resolved, resolution, onAnswer, trace = 0 }) {
  const questions = req.tool_input?.questions;
  const [selected, setSelected] = useState({});
  const [custom, setCustom] = useState({});
  const [submitted, setSubmitted] = useState(false);
  const [showValidation, setShowValidation] = useState(false);
  const [submitError, setSubmitError] = useState(false);
  const [acceptedAnswers, setAcceptedAnswers] = useState(null);
  const submitting = useRef(false);
  const inputs = useRef({});
  const cardId = useId();
  const submission = buildQuestionSubmission(questions, selected, custom);
  const locked = resolved || submitted;
  const recordedAnswers = resolution?.answers || acceptedAnswers;
  const stateLabel = resolved
    ? resolution?.behavior === 'expired' ? '已失效' : resolution?.behavior === 'deny' ? '已取消' : '已答复'
    : submitted ? '已提交' : '等待你回答';

  const submit = async (event) => {
    event.preventDefault();
    if (locked || submitting.current || submission.invalid) return;
    if (submission.missing.length) {
      setShowValidation(true);
      inputs.current[submission.missing[0]]?.focus();
      return;
    }
    submitting.current = true;
    setSubmitted(true);
    setSubmitError(false);
    try {
      await onAnswer(req.request_id, submission.answers);
      setAcceptedAnswers(submission.answers);
    } catch {
      setSubmitted(false);
      setSubmitError(true);
    } finally {
      submitting.current = false;
    }
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
      className={`question-card${trace ? ' trace-card' : ''}${resolved ? ' resolved' : ''}`}
      aria-labelledby={`${cardId}-title`}
      onSubmit={submit}
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
        <Alert type="warning" showIcon message="问题格式不完整或问题重复，请让助手重新提问。" />
      ) : questions.map((question, index) => {
        const options = (question.options || []).filter((option) => !isCustomAnswerOption(option));
        const hasOptions = options.length > 0;
        const invalid = showValidation && submission.missing.includes(index);
        const inputId = `${cardId}-${index}-answer`;
        const questionId = `${cardId}-${index}-question`;
        return (
          <div key={index} className="question-item">
            <div id={questionId} className="question-text">
              {question.header && <span className="qc-topic">{question.header}</span>}
              <span>{question.question}</span>
            </div>
            {resolved ? (
              <div className="question-answer">
                {recordedAnswers?.[question.question] ?? (resolution?.behavior === 'expired'
                  ? '该问题已失效，请让助手重新提问。'
                  : resolution?.behavior === 'deny' ? '未提交答案' : '此历史记录未保存答案')}
              </div>
            ) : (
              <>
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
                            {option.description && <span className="option-desc">{option.description}</span>}
                          </span>
                          <span className="option-check"><CheckOutlined /></span>
                        </button>
                      );
                    })}
                  </div>
                )}
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
                    disabled={locked}
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
              </>
            )}
          </div>
        );
      })}
      {!resolved && !submission.invalid && (
        <div className="qc-submit">
          {submitError && <span className="qc-error" role="alert">提交失败，回答已保留，请重试。</span>}
          <Button size="small" type="primary" shape="round" htmlType="submit" disabled={locked}>
            {submitted ? '已提交' : '提交回答'}
          </Button>
        </div>
      )}
    </form>
  );
}
