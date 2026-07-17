import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyServerPasswordViolations,
  evaluatePasswordRules,
  passwordFormState,
  passwordViolationLabels,
} from './passwordPolicy.js';

test('password rules mirror backend policy codes including old-password reuse', () => {
  const rules = evaluatePasswordRules(' admin123 ', {
    username: 'admin', oldPassword: ' admin123 ', includeSameAsOld: true,
  });
  const failed = rules.filter((rule) => !rule.met).map((rule) => rule.id);
  assert.ok(failed.includes('surrounding_whitespace'));
  assert.ok(failed.includes('contains_username'));
  assert.ok(failed.includes('same_as_old'));
});

test('unicode character classes follow backend islower/isupper/isdigit semantics', () => {
  const rules = evaluatePasswordRules('密码Aa１２!xyz');
  assert.equal(rules.find((rule) => rule.id === 'character_classes').met, true);
});

test('server violations override optimistic client validation and remain explainable', () => {
  const optimistic = evaluatePasswordRules('Good#Pass2026');
  const merged = applyServerPasswordViolations(optimistic, ['common_password', 'future_rule']);
  assert.equal(merged.find((rule) => rule.id === 'common_password').met, false);
  assert.deepEqual(passwordViolationLabels(['future_rule']), ['服务端规则：future_rule']);
});

test('password form submits only when confirmation and all rules pass', () => {
  assert.equal(passwordFormState({ password: 'Good#Pass2026', confirmation: 'different' }).canSubmit, false);
  assert.equal(passwordFormState({ password: 'Good#Pass2026', confirmation: 'Good#Pass2026' }).canSubmit, true);
});
