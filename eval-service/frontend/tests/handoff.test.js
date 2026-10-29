import { test } from 'node:test';
import assert from 'node:assert/strict';
import { HANDOFF, READY, isTrustedOrigin, receiveHandoff } from '../src/handoff.js';

// All origins and tokens are synthetic.
const here = 'http://eval.example.com:8090';
function harness() {
  const listeners = new Set();
  const opener = { sent: [], postMessage(message, origin) { this.sent.push({ message, origin }); } };
  const target = {
    addEventListener: (type, fn) => listeners.add(fn),
    removeEventListener: (type, fn) => listeners.delete(fn),
    dispatch: (event) => [...listeners].forEach((fn) => fn(event)),
    get listening() { return listeners.size; },
  };
  return { opener, target };
}

test('explicit trusted origins are matched exactly; otherwise only the same host name', () => {
  assert.equal(isTrustedOrigin('https://modeling.example.com', ['https://modeling.example.com'], here), true);
  assert.equal(isTrustedOrigin('https://other.example.com', ['https://modeling.example.com'], here), false);
  assert.equal(isTrustedOrigin('http://eval.example.com:5100', [], here), true);
  assert.equal(isTrustedOrigin('http://other.example.com:5100', [], here), false);
  assert.equal(isTrustedOrigin('null', [], here), false);
});

test('loopback host names are trusted as one host only between themselves', () => {
  assert.equal(isTrustedOrigin('http://localhost:5100', [], 'http://127.0.0.1:8090'), true);
  assert.equal(isTrustedOrigin('http://[::1]:5100', [], 'http://127.0.0.1:8090'), true);
  assert.equal(isTrustedOrigin('http://localhost:5100', [], here), false);
  assert.equal(isTrustedOrigin('http://modeling.example.com:5100', [], 'http://127.0.0.1:8090'), false);
});

test('a trusted opener hands over tokens after the ready signal', async () => {
  const { opener, target } = harness();
  const pending = receiveHandoff({ opener, trusted: [], here, target });
  assert.deepEqual(opener.sent, [{ message: { type: READY }, origin: '*' }]);
  target.dispatch({ source: opener, origin: 'http://eval.example.com:5100',
    data: { type: HANDOFF, access_token: 'synthetic-access', refresh_token: 'synthetic-refresh' } });
  assert.deepEqual(await pending, { access_token: 'synthetic-access', refresh_token: 'synthetic-refresh' });
  assert.equal(target.listening, 0);
});

test('messages from other windows, untrusted origins or with a wrong shape are ignored', async () => {
  const { opener, target } = harness();
  const pending = receiveHandoff({ opener, trusted: ['https://modeling.example.com'], here, target, timeoutMs: 50 });
  const tokens = { type: HANDOFF, access_token: 'synthetic-access' };
  target.dispatch({ source: {}, origin: 'https://modeling.example.com', data: tokens });
  target.dispatch({ source: opener, origin: 'https://attacker.example.net', data: tokens });
  target.dispatch({ source: opener, origin: 'https://modeling.example.com', data: { type: HANDOFF } });
  target.dispatch({ source: opener, origin: 'https://modeling.example.com', data: { type: 'other', access_token: 'x' } });
  assert.equal(await pending, null);
  assert.equal(target.listening, 0);
});

test('without an opener no handoff is requested', async () => {
  const { target } = harness();
  assert.equal(await receiveHandoff({ opener: null, trusted: [], here, target }), null);
  assert.equal(target.listening, 0);
});
