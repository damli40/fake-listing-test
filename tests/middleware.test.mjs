import test from 'node:test';
import assert from 'node:assert/strict';
import middleware, { config } from '../middleware.js';

function capture(fn) {
  const lines = [];
  const orig = console.log;
  console.log = (...a) => lines.push(a.join(' '));
  try { return [fn(), lines]; } finally { console.log = orig; }
}

test('logs one LT_FETCH line with run id, slot, user agent and country, and no IP', () => {
  const req = new Request('https://x.test/r/abc234/b?utm=1', {
    headers: { 'user-agent': 'meta-externalfetcher/1.1', 'x-vercel-ip-country': 'US',
               'x-forwarded-for': '203.0.113.9', 'x-real-ip': '203.0.113.9', cookie: 'secret=1' },
  });
  const [res, lines] = capture(() => middleware(req));
  assert.equal(lines.length, 1);
  assert.ok(lines[0].startsWith('LT_FETCH '));
  const e = JSON.parse(lines[0].slice(9));
  assert.deepEqual(Object.keys(e).sort(), ['country', 'method', 'path', 'run', 'slot', 't', 'ua']);
  assert.equal(e.path, '/r/abc234/b');
  assert.equal(e.run, 'abc234');
  assert.equal(e.slot, 'b');
  assert.equal(e.ua, 'meta-externalfetcher/1.1');
  assert.equal(e.country, 'US');
  assert.ok(!lines[0].includes('203.0.113.9'));
  assert.ok(!lines[0].includes('secret'));
  assert.ok(!Number.isNaN(Date.parse(e.t)));
  assert.equal(res.headers.get('x-middleware-next'), '1');
});

test('non-run paths log with empty run id; missing headers do not crash', () => {
  const [, lines] = capture(() => middleware(new Request('https://x.test/about')));
  const e = JSON.parse(lines[0].slice(9));
  assert.equal(e.run, '');
  assert.equal(e.slot, '');
  assert.equal(e.country, '');
  assert.equal(e.path, '/about');
});

test('very long user agent is cut', () => {
  const [, lines] = capture(() => middleware(new Request('https://x.test/r/abc234/a',
    { headers: { 'user-agent': 'x'.repeat(5000) } })));
  assert.equal(JSON.parse(lines[0].slice(9)).ua.length, 300);
});

test('matcher covers run pages, about, buy and robots', () => {
  assert.ok(config.matcher.some((m) => m.startsWith('/r/')));
  for (const p of ['/about', '/buy', '/robots.txt']) assert.ok(config.matcher.includes(p));
});
