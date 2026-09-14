// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import { passwordViolationLabels } from './passwordPolicy';

export default function PasswordRuleList({ rules, confirmationEntered = false, matches = true, violations = [] }) {
  const unknownViolations = passwordViolationLabels(violations).filter(
    (_label, index) => !(rules || []).some((rule) => rule.id === violations[index]),
  );
  return (
    <div className="settings-password-rules" aria-live="polite">
      <ul>
        {(rules || []).map((rule) => (
          <li className={rule.met ? 'is-met' : 'is-unmet'} key={rule.id}>
            <span aria-hidden="true">{rule.met ? '✓' : '○'}</span>{rule.label}
          </li>
        ))}
        {confirmationEntered && (
          <li className={matches ? 'is-met' : 'is-unmet'}>
            <span aria-hidden="true">{matches ? '✓' : '○'}</span>两次输入的新密码一致
          </li>
        )}
      </ul>
      {unknownViolations.length > 0 && (
        <div className="settings-password-server" role="alert">{unknownViolations.join('；')}</div>
      )}
    </div>
  );
}
