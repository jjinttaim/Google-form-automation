#!/usr/bin/env node
'use strict';
/*
 * READ-ONLY rehearsal of browser/console_snippet.js against a real Google Form.
 * Manual tool - not part of the automated test (it needs the network and talks to Google).
 *
 *   node tests/browser/real_form_dryrun.js [--url=<viewform url>] [--at=+30] [--fine=false] [--guard=10]
 *                                          [--budget=60] [--count-file=<path>] [--grade=2] [--name=<text>] [--json=<path>]
 *                                          [--answers='{"질문 제목":"값"}']   (use SETTINGS.answers instead of the widgets)
 *
 * What it does: opens the form in headless Chromium, runs the snippet once with inspectOnly (no network) and once
 * as a dry run (dryRun:true), and prints what the snippet measured.
 *
 * Safety (installed BEFORE the first navigation, independent of the snippet's own rules):
 *   - every request that is not GET/HEAD is aborted
 *   - every request whose URL contains "formResponse" is aborted, whatever the method
 *   - every host other than the form's own is aborted (so the form's own scripts do not load; the answers are
 *     therefore put into the server-rendered widgets by hand, the way the page's script would mark them)
 *   - at most --budget requests are let through (--count-file keeps the count across invocations)
 * So nothing here can record a response. The run fails if the guard had to abort anything the snippet sent.
 */
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const ARGS = Object.fromEntries(process.argv.slice(2).map((a) => { const m = /^--([^=]+)(?:=(.*))?$/.exec(a); return m ? [m[1], m[2] === undefined ? true : m[2]] : [a, true]; }));
const URL_ = ARGS.url || 'https://docs.google.com/forms/d/e/1FAIpQLSexwbISrhBuCYBsXaKvYXWbKZkAyUPam6oHaZDpl5H-7JJc0A/viewform';
const HOST = new URL(URL_).hostname;
const BUDGET = parseInt(ARGS.budget || '60', 10);
const COUNT_FILE = ARGS['count-file'] || null;
const SNIPPET = fs.readFileSync(process.env.GFO_SNIPPET || path.resolve(__dirname, '..', '..', 'browser', 'console_snippet.js'), 'utf8');
const r1 = (x) => (typeof x === 'number' ? Math.round(x * 10) / 10 : x);

function readCount() { try { return parseInt(fs.readFileSync(COUNT_FILE, 'utf8'), 10) || 0; } catch (e) { return 0; } }

(async () => {
  if (/formResponse/i.test(URL_)) throw new Error('refusing: the URL must be the viewform page');
  const proxy = process.env.HTTPS_PROXY || process.env.https_proxy;
  const browser = await chromium.launch({ proxy: proxy ? { server: proxy, bypass: '127.0.0.1,localhost' } : undefined });
  const context = await browser.newContext({ serviceWorkers: 'block', locale: 'ko-KR', timezoneId: 'Asia/Seoul' });
  const guard = { passed: [], aborted: [], otherHosts: 0, used: 0, start: COUNT_FILE ? readCount() : 0 };
  await context.route('**/*', (route) => {
    const req = route.request(); const url = req.url(); const method = req.method();
    if (!/^https?:/i.test(url)) return route.continue();
    let host = ''; try { host = new URL(url).hostname; } catch (e) { /* ignore */ }
    if (method !== 'GET' && method !== 'HEAD') { guard.aborted.push(method + ' ' + url.slice(0, 120)); return route.abort('blockedbyclient'); }
    if (/formResponse/i.test(url)) { guard.aborted.push(method + ' ' + url.slice(0, 120)); return route.abort('blockedbyclient'); }
    if (host !== HOST) { guard.otherHosts++; return route.abort('blockedbyclient'); }
    if (guard.start + guard.used >= BUDGET) { guard.aborted.push('BUDGET ' + method + ' ' + url.slice(0, 120)); return route.abort('blockedbyclient'); }
    guard.used++;
    if (COUNT_FILE) fs.writeFileSync(COUNT_FILE, String(guard.start + guard.used));
    guard.passed.push({ t: Date.now(), method, path: new URL(url).pathname.replace(/\/forms\/d\/e\/[^/]+/, '') });
    return route.continue();
  });

  const page = await context.newPage();
  const io = { warnings: [], errors: [], dialogs: [] };
  page.on('console', (m) => { if (m.type() === 'warning') io.warnings.push(m.text().slice(0, 300)); if (m.type() === 'error') io.errors.push(m.text().slice(0, 300)); });
  page.on('pageerror', (e) => io.errors.push('pageerror: ' + String(e).slice(0, 300)));
  page.on('dialog', async (d) => { io.dialogs.push(d.message()); await d.accept(); });
  await page.goto(URL_, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(1000);          // keeps the request rate below 2 per second right after the navigation

  // The form's own script cannot load behind this guard: mark the answers in the widgets the way a user's clicks would.
  const filled = await page.evaluate(({ grade, name }) => {
    window.__viol = []; document.addEventListener('securitypolicyviolation', (e) => window.__viol.push(e.violatedDirective + ' ' + e.blockedURI));
    const radios = [...document.querySelectorAll('[role="radio"]')];
    for (const r of radios) r.setAttribute('aria-checked', String(r.getAttribute('data-value') === grade));
    const i = document.querySelector('form input[type="text"]');
    if (i) i.value = name;
    return { radios: radios.length, text: !!i, title: document.title, hasForm: !!document.querySelector('form[action*="formResponse"]') };
  }, { grade: String(ARGS.grade || '2'), name: String(ARGS.name || 'DRYRUN-NEVER-SENT') });
  console.log('page:', JSON.stringify(filled), 'final url:', page.url().replace(/\?.*/, ''));

  const out = { url: URL_, startedAt: new Date().toISOString() };
  // 1. what would be sent (no network)
  const answers = ARGS.answers ? JSON.parse(ARGS.answers) : null;
  await page.evaluate((c) => { window.GFO_CONFIG = c; }, { inspectOnly: true, answers });
  const before = guard.used;
  const insp = await page.evaluate(SNIPPET);
  out.inspect = { status: insp.status, problems: insp.problems, table: insp.payload && insp.payload.table, body: insp.payload && insp.payload.body.replace(/fbzx=-?\d+/, 'fbzx=...').replace(/%22-?\d+%22/, '%22...%22'), requests: guard.used - before };
  console.log('inspectOnly:', JSON.stringify(out.inspect, null, 1));

  // 2. dry run
  const cfg = { at: String(ARGS.at || '+30'), dryRun: true, answers };
  if (ARGS.fine === 'false') cfg.fineClock = false;
  if (ARGS.guard !== undefined) cfg.guardMs = Number(ARGS.guard);
  await page.evaluate((c) => { window.GFO_CONFIG = c; }, cfg);
  const used0 = guard.used; const t0 = Date.now();
  const res = await page.evaluate(SNIPPET);
  const extra = await page.evaluate(() => {
    const L = window.gfo._; const t = performance.now();
    const b = (src) => { const x = L.clock.bounds(t, src); return { lo: x.lo, hi: x.hi, width: x.width, n: x.n }; };
    const all = b(), pg = b('page'), fn = b('fine');
    const st = (a) => { if (!a.length) return null; const s = [...a].sort((x, y) => x - y); return { n: s.length, min: s[0], median: s[s.length >> 1], max: s[s.length - 1] }; };
    const S = L.clock.samples;
    return {
      width: { all: all.width, page: pg.width, fine: fn.n ? fn.width : null, pageN: pg.n, fineN: fn.n },
      fineInsidePage: fn.n ? { fineLoMinusPageLo: fn.lo - pg.lo, pageHiMinusFineHi: pg.hi - fn.hi } : null,
      rttPage: st(S.filter((x) => x.src === 'page').map((x) => x.rtt)), rttFine: st(S.filter((x) => x.src === 'fine').map((x) => x.rtt)),
      callToSend: st(S.map((x) => x.pre)), usedResourceTiming: S.filter((x) => x.usedRT).length + '/' + S.length,
      protocols: [...new Set(S.map((x) => x.proto))], newConnections: S.filter((x) => x.newConn).length + '/' + S.length,
      dateVsMs: S.filter((x) => x.S !== null && x.D !== null).map((x) => x.D - Math.floor(x.S / 1000) * 1000),
      state: { resets: L.state.resets, fineOn: L.state.fineOn, fineDropped: L.state.fineDropped, dateMismatch: L.state.dateMismatch, sampleErrors: L.state.sampleErrors, hasMs: L.state.hasMs, rateLimited: L.state.rateLimited },
      overlay: (document.querySelector('[data-gfo-overlay]') || {}).textContent || null, cspViolations: window.__viol,
    };
  });
  out.dryRun = { cfg, elapsedMs: Date.now() - t0, requests: guard.used - used0, result: res, extra };
  console.log('\n--- dry run result');
  console.log((res.messages || [res.detail]).join('\n'));
  console.log('\nstatus', res.status, '| landing (page clock of the GET released at the release instant) T+' + res.landingMs + ' ms');
  if (res.clock) console.log('clock at release: width ' + r1(res.clock.widthMs) + ' ms, samples ' + res.clock.samples + ', source ' + res.clock.clockSource + ', fine clock ' + res.clock.fineClock + ', resets ' + res.clock.resets + ', min round trip page/fine ' + r1(res.clock.minRttPageMs) + ' / ' + r1(res.clock.minRttFineMs) + ' ms');
  if (res.release) console.log('release: late by ' + r1(res.release.lateMs) + ' ms, left at T+' + r1(res.release.serverLoRelT) + ' .. T+' + r1(res.release.serverHiRelT) + ' ms; request ' + JSON.stringify(res.request));
  console.log('extra:', JSON.stringify(extra, (k, v) => (typeof v === 'number' ? r1(v) : v)));
  console.log('snippet warnings:', JSON.stringify(io.warnings.filter((w) => /\[gfo\]/.test(w)), null, 1));
  console.log('console errors:', io.errors.length, JSON.stringify(io.errors.slice(0, 5)));
  const span = guard.passed.length > 1 ? (guard.passed[guard.passed.length - 1].t - guard.passed[0].t) / 1000 : 0;
  const kinds = {}; for (const p of guard.passed) { const k = p.method + ' ' + p.path; kinds[k] = (kinds[k] || 0) + 1; }
  let minGap = Infinity; for (let i = 1; i < guard.passed.length; i++) minGap = Math.min(minGap, guard.passed[i].t - guard.passed[i - 1].t);
  out.guard = { requestsThisRun: guard.used, totalCount: guard.start + guard.used, kinds, seconds: span, perSecond: span ? guard.used / span : null, minGapMs: minGap, aborted: guard.aborted, otherHostsBlocked: guard.otherHosts };
  console.log('\nguard:', JSON.stringify(out.guard, (k, v) => (typeof v === 'number' ? r1(v) : v)));
  if (ARGS.json) fs.writeFileSync(ARGS.json, JSON.stringify(out, null, 1));
  await browser.close();
  const ok = res.status === 'DRYRUN' && guard.aborted.length === 0 && insp.status === 'INSPECT' && !(insp.problems || []).length;
  console.log(ok ? '\nOK: dry run completed, nothing was (or could have been) submitted.' : '\nFAILED: see above.');
  process.exit(ok ? 0 : 1);
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
