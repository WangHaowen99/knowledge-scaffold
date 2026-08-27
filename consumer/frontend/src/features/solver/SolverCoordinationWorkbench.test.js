import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const currentDir = dirname(fileURLToPath(import.meta.url));
const workbenchSource = readFileSync(join(currentDir, 'SolverCoordinationWorkbench.jsx'), 'utf8');
const solverStyles = readFileSync(join(currentDir, 'solver.css'), 'utf8');
const appShellSource = readFileSync(join(currentDir, '../../app/AppShell.jsx'), 'utf8');

test('legacy solver surface is read-only history and evidence', () => {
  assert.equal(workbenchSource.includes('createCandidateSet('), false);
  assert.equal(workbenchSource.includes('createValidationRun('), false);
  assert.equal(workbenchSource.includes('<input'), false);
  assert.equal(workbenchSource.includes('求解历史'), true);
  assert.equal(workbenchSource.includes('验证证据'), true);
  assert.equal(workbenchSource.includes('getSolverHistory('), true);
  assert.equal(workbenchSource.includes('getSolverEvidence('), true);
});

test('solver is absent from primary navigation and retained as a lazy read-only surface', () => {
  assert.equal(
    appShellSource.includes("const SolverCoordinationWorkbench = lazy(() => import('../features/solver/SolverCoordinationWorkbench'))"),
    true,
  );
  assert.equal(appShellSource.includes("navBtn('solver'"), false);
  assert.equal(appShellSource.includes("surface === 'solver' && workspaceDataReady"), true);
});

test('read-only solver history remains responsive and restrained', () => {
  assert.match(solverStyles, /@media \(max-width: 900px\)/);
  assert.equal(solverStyles.includes('.solver-plan-table input'), false);
  assert.equal(solverStyles.includes('border-radius: 8px'), true);
});
