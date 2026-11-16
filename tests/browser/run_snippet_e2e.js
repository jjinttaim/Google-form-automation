// End-to-end test of browser/console_snippet.js against the local mock (tests/mock_forms_server.py).
// Nothing here talks to Google.   usage:  node tests/browser/run_snippet_e2e.js
// Needs node, python3 and Playwright (require('playwright') with a Chromium build).
'use strict';
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const readline = require('readline');
const { chromium } = require('playwright');

const ROOT = path.resolve(__dirname, '..', '..');
const SNIPPET = fs.readFileSync(path.join(ROOT, 'browser', 'console_snippet.js'), 'utf8');

function startMock(kwargs) {
  return new Promise((resolve, reject) => {
    const p = spawn('python3', [path.join(__dirname, 'mock_runner.py'), JSON.stringify(kwargs)], { stdio: ['pipe', 'pipe', 'inherit'] });
    const rl = readline.createInterface({ input: p.stdout });
    const waiting = [];
    let info = null;
    rl.on('line', (line) => {
      const msg = JSON.parse(line);
      if (!info) { info = msg; resolve(mock); return; }
      const w = waiting.shift(); if (w) w(msg);
    });
    p.on('error', reject);
    const mock = {
      get info() { return info; },
      cmd(obj) { return new Promise((r) => { waiting.push(r); p.stdin.write(JSON.stringify(obj) + '\n'); }); },
      async state() { const r = await fetch(info.origin + '/__mock/responses'); return r.json(); },
      stop() { p.stdin.end(); },
    };
  });
}

function isoUtc(ms) { return new Date(ms).toISOString().replace('Z', '+00:00'); }

async function runCase(browser, name, mockKw, cfgFn, check) {
  const mock = await startMock(mockKw);
  const page = await browser.newPage();
  page.on('dialog', (d) => d.accept());           // the snippet's confirm()
  try {
    await page.goto(mock.info.form_url);
    const now = (await mock.state()).now_ms;
    const T = Math.ceil((now + 14000) / 1000) * 1000;
    const cfg = cfgFn(T);
    await page.evaluate((c) => { window.GFO_CONFIG = c; }, cfg);
    await page.evaluate(SNIPPET);
    const res = await page.evaluate(() => window.gfo.done.then(() => window.gfo.result), null);
    const st = await mock.state();
    const problems = check({ T, res, st }) || [];
    if (st.violations.length) problems.push('GET/HEAD to /formResponse: ' + JSON.stringify(st.violations));
    return { name, ok: problems.length === 0, problems, res, st, T };
  } catch (e) {
    return { name, ok: false, problems: ['exception: ' + e.message] };
  } finally {
    await page.close();
    mock.stop();
  }
}

(async () => {
  const launch = {};
  if (process.env.GFO_CHROMIUM) launch.executablePath = process.env.GFO_CHROMIUM;
  const browser = await chromium.launch(launch);
  const answers = { '학년': '2', '이름': '홍 길동&테스트+1' };
  const base = { up_ms: 4, down_ms: 4, jitter_ms: 2, backend_ms: 25, backend_jitter_ms: 10, proc_ms: 8, date_lag_ms: 4 };
  const fireCheck = ({ T, res, st }) => {
    const p = [];
    if (st.responses.length !== 1) p.push('responses=' + st.responses.length);
    const r = st.responses[0];
    if (r) {
      if (r.arrival_ms < T) p.push('EARLY arrival ' + (r.arrival_ms - T).toFixed(1) + ' ms');
      if (r.stamp_ms < T) p.push('EARLY stamp ' + (r.stamp_ms - T).toFixed(1) + ' ms');
      if (r.stamp_ms - T > 600) p.push('late ' + (r.stamp_ms - T).toFixed(0) + ' ms');
      if ((r.fields['entry.536164697'] || [])[0] !== '홍 길동&테스트+1') p.push('name field ' + JSON.stringify(r.fields['entry.536164697']));
      if ((r.fields['entry.405927314'] || [])[0] !== '2') p.push('grade field ' + JSON.stringify(r.fields['entry.405927314']));
    }
    if (!res || !/^RECORDED/.test(res.status)) p.push('status ' + (res && res.status) + ' ' + (res && res.detail));
    return p;
  };
  const cases = [];
  for (const skew of [1234.5, -8765.4, 3600000.7]) {
    cases.push(['real fire, skew ' + skew, Object.assign({}, base, { skew_ms: skew }),
      (T) => ({ at: isoUtc(T), dryRun: false, answers }), fireCheck]);
  }
  cases.push(['real fire, no server_time_ms in pages', Object.assign({}, base, { skew_ms: -555, expose_server_time: false }),
    (T) => ({ at: isoUtc(T), dryRun: false, answers }), fireCheck]);
  cases.push(['dry run records nothing', Object.assign({}, base, { skew_ms: 999 }),
    (T) => ({ at: isoUtc(T), dryRun: true, answers }),
    ({ st }) => (st.responses.length ? ['recorded during dry run'] : [])]);
  cases.push(['bad option refused', Object.assign({}, base),
    (T) => ({ at: isoUtc(T), dryRun: false, answers: { '학년': '9', '이름': '가' } }),
    ({ st, res }) => [...(st.responses.length ? ['recorded'] : []), ...(res && /RECORDED/.test(res.status) ? ['status ' + res.status] : [])]]);
  cases.push(['bare HH:MM:SS refused', Object.assign({}, base),
    () => ({ at: '22:00:00', dryRun: false, answers }),
    ({ st, res }) => [...(st.responses.length ? ['recorded'] : []), ...(res && res.code !== 'AT_BARE_TIME' ? ['code ' + (res && res.code)] : [])]]);

  const rows = [];
  for (const [name, kw, cfg, check] of cases) {
    const r = await runCase(browser, name, kw, cfg, check);
    const st = r.st && r.st.responses[0];
    rows.push({ case: name, ok: r.ok ? 'PASS' : 'FAIL', 'stamp-T ms': st ? (st.stamp_ms - r.T).toFixed(1) : '-',
      status: r.res ? r.res.status : '-', problems: r.problems.join('; ') });
  }
  await browser.close();
  console.table(rows);
  const failed = rows.filter((r) => r.ok !== 'PASS').length;
  console.log(failed ? failed + ' case(s) FAILED' : 'all ' + rows.length + ' cases passed');
  process.exit(failed ? 1 : 0);
})();
