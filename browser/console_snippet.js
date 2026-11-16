/*
 * gform_ontime 브라우저 콘솔 스니펫 (설치 없이 Chrome 개발자 도구에서 쓰는 버전)
 * ==========================================================================
 * 하는 일
 *   열려 있는 Google 설문 페이지 안에서 Google 서버 시계를 직접 재고, 서버 시각으로
 *   목표 시각 T "이후에만" 딱 한 번 제출합니다. (일찍 찍히면 무효, 늦으면 순위만 밀리는 선착순용)
 *
 * 사용법 (PC용 Chrome / Edge)
 *   1. 설문 페이지를 열고 답을 모두 입력합니다. 설문의 [제출] 버튼은 누르지 않습니다.
 *   2. F12 -> Console 탭. (처음 붙여넣을 때 Chrome 이 요구하면 allow pasting 을 직접 입력)
 *      또는 Sources -> Snippets 에 이 파일을 저장해 두고 Ctrl+Enter 로 실행해도 됩니다.
 *   3. 아래 SETTINGS 만 고친 뒤 파일 전체를 붙여넣고 Enter.
 *        - 아무것도 고치지 않고 실행하면: 30초 뒤를 목표로 하는 "연습" (아무것도 제출하지 않음)
 *        - 실제 제출: at 에 날짜+시각을 적고 dryRun: false. 확인 창에서 [확인]을 눌러야 예약됩니다.
 *        - 보낼 내용만 미리 보고 싶으면 아래 전문가용 옵션의 inspectOnly 를 true 로.
 *   4. 결과가 나올 때까지 이 탭을 화면에 보이게 두세요. (최소화하거나 다른 창으로 완전히 가리면
 *      브라우저가 타이머를 늦춥니다. 노트북은 전원을 연결하세요.)
 *      취소: 콘솔에 gfo.cancel() 입력 (발사 약 0.4초 전까지 가능)
 *   5. 결과는 화면 오른쪽 위 상자와 콘솔에 나옵니다. 자세한 값은 콘솔에 gfo.result 입력.
 *      실제 제출 뒤에는 설문의 [제출] 버튼을 누르지 마세요. 이 도구는 절대 다시 보내지 않습니다.
 *
 * 꼭 알아 둘 점
 *   - 기본 설정(leadMs 0)에서는 요청이 서버 시각 T + guardMs 이전에 이 PC를 떠나지 않습니다.
 *     그래서 T 보다 이르게 기록될 수 없고, 대신 네트워크/서버 처리 시간만큼 늦게 찍힙니다.
 *     실제로 얼마나 늦는지는 연습(dryRun)을 돌려 보면 "T+xx ms" 로 알려 줍니다.
 *   - 목표 시각은 날짜까지 적어야 합니다. 밤 10시는 22:00:00 입니다 (10:00:00 은 오전 10시).
 *   - 닫혀 있는 설문이 열리기를 기다리는 기능은 없습니다. 열려 있는 설문 페이지에서만 동작합니다.
 *   - 제출은 딱 한 번입니다. 결과가 "알 수 없음"이어도 다시 보내지 않습니다 (중복 응답 방지).
 *   - 응답 시트의 기본 표시는 초 단위 반올림입니다 (xx.500 이후에 찍히면 다음 초로 보입니다).
 *
 * --------------------------------------------------------------------------
 * How it works (for maintainers)
 *   All local times are performance.now() milliseconds; the PC wall clock is never used for timing.
 *   theta := (server clock) - performance.now().  Every clock sample gives hard bounds on theta:
 *     page GET (same-origin fetch of the viewform URL, body contains "server_time_ms":S)
 *        theta >= S - t_token          t_token = when the bytes carrying S had arrived
 *        theta <  S + 1 - t_send       t_send  = Resource Timing requestStart (request left the PC)
 *     Date header of the same response, treated as an independent clock reading (it is stamped a few
 *     ms after S, so Date == floor(S/1000) is NOT assumed):
 *        theta >= D - t_first          t_first = first byte of the final response headers
 *        theta <  D + 1000 - t_send
 *     fine clock: HEAD /generate_204 (same origin), released so that the request straddles a flip of
 *     the Date second (bisection); same two Date bounds, but the round trip is a few ms.
 *   Bounds are widened by 0.5 ms per second of age (500 ppm) and intersected.  If they contradict each
 *   other: a fine sample that disagrees with the page clock switches the fine clock off; otherwise only
 *   the newest sample is kept ("reset") - its loose bounds can only make the release later.
 *   Release instant (local) = T - theta_lo + guardMs - leadMs.  With leadMs = 0 the request cannot
 *   leave this PC before server time T + guardMs, therefore it cannot be stamped before T.
 *   A dry run sends a harmless GET of the viewform URL at the release instant; nothing in this file
 *   ever sends a GET/HEAD to /formResponse, and the single POST exists only in the real-submission path.
 */
(async function gfoMain() {
  'use strict';

  // =============================== SETTINGS (여기만 고치세요) ===============================
  const SETTINGS = {
    // 목표 시각 (Google 서버 시계 기준). 날짜까지 반드시 적습니다: 'YYYY-MM-DD HH:MM:SS' (24시간제, 이 PC의 시간대)
    //   예) 11월 23일 밤 10시 정각:  '2026-11-23 22:00:00'
    //       시간대를 직접 밝히려면:   '2026-11-23T22:00:00+09:00'
    //   '+30' = 지금부터 30초 뒤 (연습 전용: dryRun 이 true 일 때만 쓸 수 있습니다)
    at: '+30',

    // true  = 연습: 발사 순간에 설문 페이지를 한 번 읽기만 하고, 아무것도 제출하지 않습니다.
    // false = 실제 제출 (딱 1회). 확인 창에서 [확인]을 눌러야 진행됩니다.
    dryRun: true,

    // 화면에 입력한 답 대신 직접 지정하고 싶을 때만 씁니다. null 이면 화면에 입력한 답을 그대로 보냅니다.
    //   예) { '학년': '2', '이름': '홍길동' }     (질문 제목 또는 'entry.405927314' 같은 번호)
    answers: null,

    // 안전 여유(ms): 서버 시각이 "확실히" T + guardMs 이상일 때 요청을 내보냅니다. 보통 10 그대로 둡니다.
    guardMs: 10,
  };

  // =============================== 전문가용 옵션 (보통은 그대로 둡니다) ===============================
  const EXPERT = {
    // leadMs > 0: 그만큼 "일찍" 내보냅니다 (발사 시각 = T - theta_lo + guardMs - leadMs).
    //   본인 소유 설문으로 보정해서 "출발 후 최소 이만큼 지나야 기록된다"는 것을 확인한 경우에만 쓰세요.
    //   0 이 아니면 "절대 이르지 않음" 보장이 사라집니다. 허용 범위 0 ~ 2000.
    leadMs: 0,
    // 'fetch' = 미리 만들어 둔 POST 를 fetch() 로 1회 전송 (기본, 결과를 읽어서 알려 줌)
    // 'click' = 발사 순간에 설문의 [제출] 버튼을 대신 누름. ** 검증되지 않은 예비 수단입니다 **
    via: 'fetch',
    // true = HEAD /generate_204 의 Date 초 바뀜으로 서버 시계를 더 정밀하게 잽니다 (설문 페이지 시계와 모순되면 자동으로 꺼짐).
    //        Google 프런트엔드 시계와 설문 서버 시계가 같다는 가정이 들어갑니다. false 로 하면 그 가정 없이
    //        설문 페이지 시계만 쓰고, 대신 수십 ms 더 늦게 발사됩니다.
    fineClock: true,
    // true = 보낼 내용을 표로 보여 주기만 하고 멈춥니다 (네트워크 요청 없음)
    inspectOnly: false,
    // ---- tuning ----
    initialSamples: 4,     // page GETs before the target time is shown
    fineSamples: 12,       // max. fine-clock requests in the first bisection round
    idleEveryMs: 30000,    // far from T: one sample this often (fine / page alternating)
    denseFromMs: 15000,    // closer than this to the release: one fine sample per second ...
    pageEveryMs: 5000,     // ... and one page GET this often
    lastSampleMs: 2000,    // keep-warm page GET this long before the release (the last request before the shot)
    spinMs: 400,           // busy-wait on performance.now() for the last stretch
    minGapMs: 600,         // politeness: never more than about 1.6 requests per second
    libOnly: false,        // test hook: define window.gfo._ and return without doing anything
  };
  // ======================================================================================================

  const VERSION = '1.0.0';
  const AGE_PPM = 500;          // bounds are widened by 0.5 ms per second of sample age
  const PAD_MS = 0.3;           // covers the 0.1 ms coarsening of browser timestamps
  const SAMPLE_TIMEOUT_MS = 8000;
  const FORBIDDEN = /formresponse/i;   // no probe of any kind may ever address this path
  const RE_ST = /"server_time_ms":(\d{12,14})[,}]/;
  const RE_RECORDED = /응답이 기록되었습니다|Your response has been recorded|usp=form_confirm/;
  const RE_CLOSED = /더 이상 응답을 받지 않습니다|no longer accepting responses/i;

  const now = () => performance.now();
  const rawSleep = (ms) => new Promise((r) => setTimeout(r, Math.max(0, ms)));
  function spinUntil(t) { let n; do { n = now(); } while (n < t); return n; }
  class Stop extends Error {
    constructor(status, code, message) { super(message); this.status = status; this.code = code; }
  }

  // ---------------------------------------------------------------- one run at a time, state only in window.gfo
  const prev = window.gfo;
  if (prev && prev.running) {
    console.warn('[gfo] 이미 실행 중입니다. 아무것도 바꾸지 않았습니다. 취소하려면 gfo.cancel() 을 입력한 뒤 다시 실행하세요.');
    return 'already-running';
  }
  const EXT = (window.GFO_CONFIG && typeof window.GFO_CONFIG === 'object') ? window.GFO_CONFIG : null;   // automation hook
  const CONFIG = Object.assign({}, EXPERT, SETTINGS, EXT || {});
  const state = {
    phase: 'init', cancelled: false, fired: false, frozen: false, noMoreSamples: false, quiet: false,
    T: null, lastSampleAt: -1e9, hasMs: false, fineOn: false, fineDropped: false, fineFails: 0, resets: 0, lastResetAt: null,
    dateMismatch: 0, requests: 0, sampleErrors: 0, rateLimited: false, closedSeen: false, hiddenSeen: false, k: 0, status: '',
  };
  let resolveDone;
  const gfo = {
    version: VERSION, running: !CONFIG.libOnly, submitted: !!(prev && prev.submitted), config: CONFIG, state, result: null,
    done: new Promise((r) => { resolveDone = r; }),
    cancel() {
      if (!gfo.running) { console.log('[gfo] 실행 중이 아닙니다.'); return false; }
      if (state.fired) { console.warn('[gfo] 이미 발사되어 취소할 수 없습니다.'); return false; }
      state.cancelled = true; gfo.running = false;
      for (const ac of live) { try { ac.abort(); } catch (e) { /* ignore */ } }
      return true;
    },
  };
  if (prev && prev.box && prev.box.remove) { try { prev.box.remove(); } catch (e) { /* ignore */ } }
  window.gfo = gfo;
  const live = new Set();       // AbortControllers of clock samples that are still in flight

  // ---------------------------------------------------------------- console output (buffered near the release)
  const pending = [];
  function out(level, args) {
    if (state.quiet) { pending.push([level, args]); return; }
    if (level === 'log') console.log('%c[gfo]', 'color:#1a73e8;font-weight:bold', ...args);
    else console[level]('[gfo]', ...args);
  }
  const log = (...a) => out('log', a);
  const warn = (...a) => out('warn', a);
  const error = (...a) => out('error', a);
  function flushLogs() { state.quiet = false; for (const [l, a] of pending.splice(0)) out(l, a); }

  // ---------------------------------------------------------------- formatting
  const pad = (n, w) => String(Math.trunc(Math.abs(n))).padStart(w || 2, '0');
  const DOW = ['일', '월', '화', '수', '목', '금', '토'];
  function fmtClock(ms) {
    const d = new Date(Math.floor(ms));
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()) + '.' + pad(d.getMilliseconds(), 3);
  }
  function fmtDate(ms) {
    const d = new Date(Math.floor(ms));
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' (' + DOW[d.getDay()] + ') ' + fmtClock(ms);
  }
  function fmtUtc(ms) { return new Date(Math.floor(ms)).toISOString().replace('T', ' ').replace('Z', ''); }
  function tzLabel(ms) {
    const o = -new Date(ms).getTimezoneOffset();
    return 'UTC' + (o >= 0 ? '+' : '-') + pad(Math.abs(o) / 60) + ':' + pad(Math.abs(o) % 60);
  }
  function fmtRel(ms) { return 'T' + (ms >= 0 ? '+' : '-') + Math.abs(ms).toFixed(1) + ' ms'; }
  function fmtLeft(ms) {
    let s = Math.max(0, ms) / 1000;
    const d = Math.floor(s / 86400); s -= d * 86400;
    const h = Math.floor(s / 3600); s -= h * 3600;
    const m = Math.floor(s / 60); s -= m * 60;
    return (d ? d + '일 ' : '') + (d || h ? h + '시간 ' : '') + (d || h || m ? m + '분 ' : '') + s.toFixed(1) + '초';
  }
  function plain(o) { return JSON.parse(JSON.stringify(o, (k, v) => (typeof v === 'number' && !isFinite(v) ? null : v))); }

  // ---------------------------------------------------------------- waiting
  function checkCancel() { if (state.cancelled) throw new Stop('CANCELLED', 'CANCELLED', '취소했습니다. 아무것도 제출되지 않았습니다.'); }
  async function pause(ms) {          // cancellable sleep
    const end = now() + ms;
    for (;;) {
      checkCancel();
      const rest = end - now();
      if (rest <= 0) return;
      await rawSleep(Math.min(rest, 100));
    }
  }
  // 'ok' when p settles, 'late' when the local time `deadline` comes first (p keeps running in the background).
  function withDeadline(p, deadline) {
    return new Promise((resolve, reject) => {
      let settled = false;
      const check = () => {
        if (settled) return;
        if (state.cancelled) { settled = true; reject(new Stop('CANCELLED', 'CANCELLED', '취소했습니다. 아무것도 제출되지 않았습니다.')); return; }
        const rest = deadline - now();
        if (rest <= 0) { settled = true; resolve('late'); return; }
        setTimeout(check, Math.min(rest, 100));
      };
      p.then(() => { if (!settled) { settled = true; resolve('ok'); } }, (e) => { if (!settled) { settled = true; reject(e); } });
      check();
    });
  }

  // ---------------------------------------------------------------- overlay (createElement/textContent/style only: Trusted Types safe)
  let box = null; const lines = {}; let ticker = null; let onVis = null;
  function ui() {
    if (box) return;
    box = document.createElement('div');
    box.setAttribute('data-gfo-overlay', '1');
    box.style.cssText = 'position:fixed;z-index:2147483647;right:12px;top:12px;padding:10px 14px;background:#202124;color:#fff;' +
      'font:13px/1.5 monospace;border-radius:8px;box-shadow:0 2px 12px rgba(0,0,0,.4);max-width:600px;white-space:pre-wrap';
    for (const k of ['title', 'clock', 'plan', 'status']) { lines[k] = document.createElement('div'); box.appendChild(lines[k]); }
    lines.title.textContent = 'gform_ontime ' + (CONFIG.dryRun ? '[연습: 아무것도 제출하지 않음]' : '[실제 제출 1회]') + '   취소: 콘솔에 gfo.cancel()';
    lines.title.style.color = CONFIG.dryRun ? '#8ab4f8' : '#f28b82';
    (document.body || document.documentElement).appendChild(box);
    gfo.box = box;
  }
  function setStatus(s) { state.status = s; if (lines.status) lines.status.textContent = s; }
  function cleanup() {
    if (ticker) { clearInterval(ticker); ticker = null; }
    if (onVis) { document.removeEventListener('visibilitychange', onVis); onVis = null; }
    if (rt.obs) { try { rt.obs.disconnect(); } catch (e) { /* ignore */ } rt.obs = null; }
    if (box) {
      const b = box;
      b.style.cursor = 'pointer'; b.title = '클릭하면 닫힙니다';
      b.addEventListener('click', () => b.remove(), { once: true });
    }
  }
  function finish(res) {
    state.phase = 'done';
    flushLogs();
    cleanup();
    gfo.result = plain(res);
    gfo.running = false;
    resolveDone(gfo.result);
    return gfo.result;
  }

  // ---------------------------------------------------------------- Resource Timing (observer: not limited by the page's buffer size)
  const rt = { list: [], obs: null };
  try {
    rt.obs = new PerformanceObserver((l) => { for (const e of l.getEntries()) rt.list.push(e); });
    rt.obs.observe({ type: 'resource', buffered: false });
  } catch (e) { rt.obs = null; }
  function rtFind(url, t0) {
    if (rt.obs) { for (const e of rt.obs.takeRecords()) rt.list.push(e); }
    const src = rt.obs ? rt.list : performance.getEntriesByName(url);
    let best = null;
    for (const e of src) {
      if (e.name !== url) continue;
      const d = e.startTime - t0;                    // startTime = the fetch() call
      if (d >= -1 && d <= 50 && (!best || Math.abs(d) < Math.abs(best.startTime - t0))) best = e;
    }
    if (rt.list.length > 600) rt.list.splice(0, rt.list.length - 300);
    return best;
  }
  async function rtWait(url, t0) {
    // the entry is queued when the response ends, which can be a moment after fetch() resolved
    let e = rtFind(url, t0);
    for (let i = 0; !e && i < 4; i++) { await rawSleep(i ? 4 : 0); e = rtFind(url, t0); }
    return e;
  }
  // first byte of the FINAL response headers (an interim 103 must not count: Date is stamped later than that)
  function headersAt(e, tSend, tHdr) {
    if (!e) return tHdr;
    let f = 0;
    if (e.finalResponseHeadersStart > 0) f = e.finalResponseHeadersStart;
    else if (!(e.firstInterimResponseStart > 0)) f = e.responseStart;
    return (f >= tSend && f <= tHdr) ? f : tHdr;
  }

  // ---------------------------------------------------------------- server clock
  const clock = {
    samples: [],
    bounds(at, src) {
      let lo = -Infinity, hi = Infinity, n = 0;
      for (const s of this.samples) {
        if (src && s.src !== src) continue;
        n++;
        const widen = Math.abs(at - s.t) * AGE_PPM * 1e-6;
        if (s.lo - widen > lo) lo = s.lo - widen;
        if (s.hi + widen < hi) hi = s.hi + widen;
      }
      return { lo, hi, width: hi - lo, n };
    },
    minRtt(src) { let m = Infinity; for (const s of this.samples) if (s.src === src && s.rtt < m) m = s.rtt; return m; },
    add(s) {
      if (state.frozen) return;                       // the release plan is fixed: late samples are ignored
      this.samples.push(s);
      if (this.samples.length > 400) this.samples = this.samples.filter((x) => s.t - x.t < 600000).slice(-400);
      const t = now();
      let b = this.bounds(t);
      if (b.lo <= b.hi) return;
      const page = this.bounds(t, 'page');
      if (this.samples.some((x) => x.src === 'fine') && page.n && page.lo <= page.hi) {
        // the front-end (Date) clock disagrees with the page clock: trust the page clock
        this.samples = this.samples.filter((x) => x.src !== 'fine');
        state.fineOn = false; state.fineDropped = true;
        warn('정밀 시계(/generate_204 의 Date)가 설문 페이지 시계와 맞지 않습니다. 정밀 시계 측정값을 모두 버리고 끕니다. (발사는 조금 늦어질 뿐 일러지지 않습니다)');
        b = this.bounds(t);
        if (b.lo <= b.hi) return;
      }
      state.resets++; state.lastResetAt = t;
      this.samples = [s];
      warn('시계 측정값이 서로 모순됩니다 (PC 절전, 시계 변경 등). 가장 새 측정값만 남기고 다시 맞춥니다. [재설정 ' + state.resets + '회째]');
    },
  };

  // One read-only request = one clock sample. kind 'page' = GET viewform, kind 'fine' = HEAD /generate_204.
  async function takeSample(kind, opt) {
    const url = kind === 'fine' ? FINE_URL : VIEW_URL;
    if (FORBIDDEN.test(url)) throw new Error('internal: refusing to probe ' + url);
    if (!(opt && opt.aimed)) {
      const gap = state.lastSampleAt + CONFIG.minGapMs - now();
      if (gap > 0) await pause(gap);
    }
    if (state.frozen || state.noMoreSamples || state.cancelled) return null;
    const ac = new AbortController();
    const killer = setTimeout(() => ac.abort(), SAMPLE_TIMEOUT_MS);
    live.add(ac);
    const t0 = state.lastSampleAt = now();
    state.requests++;
    try {
      const r = await fetch(url, { method: kind === 'fine' ? 'HEAD' : 'GET', cache: 'no-store', credentials: 'same-origin', signal: ac.signal });
      const tHdr = now();
      let S = null, tTok = null;
      if (r.body) {
        try {
          const reader = r.body.getReader(); const dec = new TextDecoder(); let tail = '';
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            if (S === null && kind === 'page') {
              tail = tail.slice(-48) + dec.decode(value, { stream: true });
              const m = RE_ST.exec(tail);
              if (m) { S = Number(m[1]); tTok = now(); tail = ''; }
            }
          }
        } catch (e) { /* body cut short: the Date header is still usable */ }
      }
      const tEnd = now();
      const e = await rtWait(url, t0);
      const tSend = (e && e.requestStart >= t0 - 0.2 && e.requestStart <= tHdr) ? e.requestStart : t0;
      const tFirst = headersAt(e, tSend, tHdr);
      if (S !== null && e && e.responseEnd >= tFirst && e.responseEnd < tTok) tTok = e.responseEnd;
      const dRaw = r.headers.get('date');
      const D = dRaw ? Date.parse(dRaw) : NaN;
      const s = {
        src: kind, t: tSend, t0, tSend, tFirst, tTok, tEnd, S, D: isNaN(D) ? null : D, status: r.status,
        rtt: tFirst - tSend, pre: tSend - t0, usedRT: !!e, proto: e ? e.nextHopProtocol : '', newConn: e ? e.connectEnd > e.connectStart : null,
        lo: null, hi: null,
      };
      let path = ''; try { path = new URL(r.url).pathname; } catch (x) { /* ignore */ }
      if (r.status === 429 || /\/sorry\//.test(path)) {
        if (!state.rateLimited) warn('서버가 "요청이 너무 많다"(HTTP ' + r.status + ')고 응답했습니다. 시계 측정을 멈추고, 지금까지의 측정값으로 진행합니다.');
        state.rateLimited = true;
        return s;
      }
      if (/\/closedform$/.test(path) && !state.closedSeen) {
        state.closedSeen = true;
        warn('설문이 지금은 닫혀 있는 것으로 보입니다 (응답을 받지 않는 상태). 그대로 진행하면 닫힌 설문에 제출하게 될 수 있습니다.');
      }
      const ok = kind === 'fine' ? (r.status === 204 || r.status === 200) : (r.status >= 200 && r.status < 400);
      let lo = -Infinity, hi = Infinity;
      if (ok && !isNaN(D)) { lo = D - tFirst; hi = D + 1000 - tSend; }
      if (ok && S !== null) {
        const loS = S - tTok, hiS = S + 1 - tSend;
        if (Math.max(lo, loS) > Math.min(hi, hiS)) { state.dateMismatch++; lo = loS; hi = hiS; }   // keep the millisecond clock
        else { lo = Math.max(lo, loS); hi = Math.min(hi, hiS); }
        state.hasMs = true;
      }
      if (isFinite(lo) && isFinite(hi)) {
        s.lo = lo - PAD_MS; s.hi = hi + PAD_MS;
        if (kind === 'fine') state.fineFails = 0;
        clock.add(s);
      } else if (kind === 'fine' && state.fineOn && ++state.fineFails >= 2) {
        state.fineOn = false;
        warn('정밀 시계(/generate_204)를 쓸 수 없어 끕니다 (HTTP ' + r.status + '). 설문 페이지 시계만으로 진행합니다.');
      }
      return s;
    } catch (e) {
      state.sampleErrors++;
      if (!state.cancelled && !state.frozen) {
        if (state.sampleErrors <= 2) warn('시계 측정 요청 1건 실패: ' + (e && e.message ? e.message : e));
        else if (state.sampleErrors === 3) warn('시계 측정 요청이 계속 실패합니다 (네트워크가 끊겼거나 로그인이 풀렸을 수 있습니다). 마지막으로 성공한 측정값으로 진행하며, 그만큼 늦게 발사될 수 있습니다.');
      }
      return null;
    } finally {
      clearTimeout(killer); live.delete(ac);
    }
  }

  // Local time at which fetch() should be called so that the request straddles the next flip of the
  // server's Date second: arrival is aimed at (lo+hi)/2 + rmin/2 before the flip (bisection), with a
  // small dither once the bracket is about as tight as the round trip allows.
  function aimTime(kind) {
    const n0 = now(); const b = clock.bounds(n0);
    if (!b.n || !isFinite(b.lo) || !isFinite(b.hi)) return null;
    const mine = clock.samples.filter((x) => x.src === kind);
    const rmin = mine.length ? Math.min(clock.minRtt(kind), kind === 'fine' ? 100 : 500) : (kind === 'fine' ? 20 : 80);
    const pres = mine.slice(-5).map((x) => x.pre).sort((x, y) => x - y);
    const pre = pres.length ? Math.min(pres[pres.length >> 1], 300) : 1;     // fetch() call -> request on the wire
    const dither = b.width < 2 * rmin + 2 ? (Math.random() - 0.5) * 0.5 * b.width : 0;
    const off = (b.lo + b.hi) / 2 + rmin * (kind === 'fine' ? 0.5 : 0.9) + pre + dither;
    const earliest = Math.max(n0 + 15, state.lastSampleAt + CONFIG.minGapMs);
    return Math.ceil((earliest + off) / 1000) * 1000 - off;
  }
  async function takeAimedAt(kind, tCall) {
    await pause(tCall - now() - 6);
    if (state.frozen || state.noMoreSamples) return null;
    spinUntil(tCall);
    return takeSample(kind, { aimed: true });
  }

  // ---------------------------------------------------------------- what will be submitted
  function loadQuestions() {
    const data = window.FB_PUBLIC_LOAD_DATA_;
    if (!Array.isArray(data) || !Array.isArray(data[1]) || !Array.isArray(data[1][1])) return null;
    const qs = []; let pageBreaks = 0;
    for (const q of data[1][1]) {
      if (!Array.isArray(q)) continue;
      if (q[3] === 8) pageBreaks++;
      if (!Array.isArray(q[4])) continue;
      for (const sub of q[4]) {
        if (!Array.isArray(sub) || typeof sub[0] !== 'number') continue;
        const opts = Array.isArray(sub[1]) ? sub[1].filter(Array.isArray) : null;
        qs.push({
          title: String(q[1] == null ? '' : q[1]).trim(), type: q[3], id: sub[0], name: 'entry.' + sub[0], required: !!sub[2],
          options: opts ? opts.map((o) => String(o[0] == null ? '' : o[0])).filter((v) => v !== '') : null,
          hasOther: opts ? opts.some((o) => !!o[4]) : false,
        });
      }
    }
    qs.pageBreaks = pageBreaks;
    return qs;
  }
  // The answer as the visible widget shows it. null = this snippet cannot read that widget; [] = nothing chosen.
  function readDom(q) {
    const marker = '[[' + q.id + ',';
    let host = null;
    for (const el of document.querySelectorAll('[data-params]')) {
      if ((el.getAttribute('data-params') || '').includes(marker)) { host = el; break; }
    }
    if (!host) return null;
    const pick = (sel, flag, attr) => {
      const all = [...host.querySelectorAll(sel)];
      if (!all.length) return null;
      return all.filter((x) => x.getAttribute(flag) === 'true').map((x) => x.getAttribute(attr)).filter((v) => v !== null && v !== '');
    };
    switch (q.type) {
      case 0: { const i = host.querySelector('input:not([type="hidden"])'); return i ? [i.value] : null; }
      case 1: { const t = host.querySelector('textarea'); return t ? [t.value] : null; }
      case 2: case 5: return pick('[role="radio"]', 'aria-checked', 'data-value');
      case 4: return pick('[role="checkbox"]', 'aria-checked', 'data-answer-value');
      case 3: return pick('[role="option"]', 'aria-selected', 'data-value');
      default: return null;      // grid, date, time, file upload ...: use SETTINGS.answers
    }
  }
  function collect(form) {
    const problems = [], notes = [], rows = [], entries = [], extra = [];
    const fd = [...new FormData(form)].filter((p) => typeof p[1] === 'string');
    const qs = loadQuestions();
    const cap = document.querySelector('[data-should-execute-invisible-captcha-on-form-submit]');
    const facts = {
      captcha: !!cap && cap.getAttribute('data-should-execute-invisible-captcha-on-form-submit') === 'true',
      multiPage: (!!qs && qs.pageBreaks > 0) || !!document.querySelector('[jsname="OCpkoe"]'),
      submitButton: document.querySelector('[jsname="M2UYVd"]'),
    };
    if (!qs) {
      problems.push('이 페이지에서 설문 구조(FB_PUBLIC_LOAD_DATA_)를 읽지 못해 보낼 내용을 검증할 수 없습니다.');
      return Object.assign({ problems, notes, rows, pairs: fd, body: '', questions: [] }, facts);
    }
    const byName = new Map(qs.map((q) => [q.name, q]));
    const ov = new Map();
    const answers = CONFIG.answers;
    const toList = (v) => (Array.isArray(v) ? v : [v]).filter((x) => x !== null && x !== undefined).map(String);
    const scalar = (v) => v === null || v === undefined || ['string', 'number', 'boolean'].includes(typeof v);
    if (answers !== null && answers !== undefined) {
      if (typeof answers !== 'object' || Array.isArray(answers)) problems.push("answers 는 { '질문 제목': '값' } 형태여야 합니다.");
      else {
        for (const key of Object.keys(answers)) {
          const k = String(key).trim(); let q = null; let m;
          if ((m = /^(?:entry\.)?(\d+)$/.exec(k))) {
            q = byName.get('entry.' + m[1]) || null;
            if (!q) { problems.push('answers 의 "' + k + '" 에 해당하는 질문이 이 설문에 없습니다.'); continue; }
          } else if ((m = /^entry\.(\d+)(\.other_option_response|_year|_month|_day|_hour|_minute)$/.exec(k))) {
            if (!byName.has('entry.' + m[1])) { problems.push('answers 의 "' + k + '" 에 해당하는 질문이 이 설문에 없습니다.'); continue; }
            extra.push([k, String(answers[key])]);
            continue;
          } else {
            const hits = qs.filter((x) => x.title === k);
            if (hits.length === 1) q = hits[0];
            else {
              problems.push(hits.length ? 'answers 의 "' + k + '": 제목이 같은 질문이 ' + hits.length + '개입니다. entry 번호로 지정하세요.'
                : 'answers 의 "' + k + '" 라는 제목의 질문이 없습니다. 이 설문의 질문: ' + qs.map((x) => '"' + x.title + '"').join(', '));
              continue;
            }
          }
          if (ov.has(q.name)) { problems.push('answers 에 "' + q.title + '" 이(가) 두 번 지정되었습니다.'); continue; }
          const v = answers[key];
          if (!(Array.isArray(v) ? v.every(scalar) : scalar(v))) { problems.push('answers 의 "' + k + '" 값은 문자열(또는 문자열 배열)이어야 합니다.'); continue; }
          ov.set(q.name, toList(v));
        }
      }
    }
    const extraNames = new Set(extra.map((p) => p[0]));
    const have = (name) => extraNames.has(name) || fd.some((p) => p[0] === name && p[1] !== '');
    for (const q of qs) {
      const hidden = fd.filter((p) => p[0] === q.name).map((p) => p[1]);
      const dom = readDom(q);
      let vals, src;
      if (ov.has(q.name)) { vals = ov.get(q.name); src = 'answers 설정'; }
      else if (dom !== null) {
        vals = dom; src = '화면에 입력된 값';
        const a = hidden.filter((v) => v !== '').join('\u0001'), b = dom.filter((v) => v !== '').join('\u0001');
        if (hidden.length && a !== b) problems.push('"' + q.title + '": 화면에 보이는 값(' + (b.split('\u0001').join(' | ') || '비어 있음') + ')과 페이지 내부 값(' + (a.split('\u0001').join(' | ') || '비어 있음') + ')이 다릅니다. answers 로 직접 지정하세요.');
      } else if (hidden.length) { vals = hidden; src = '페이지의 hidden input'; }
      else {
        vals = []; src = '(읽지 못함)';
        problems.push('"' + q.title + '" (' + q.name + ', 유형 ' + q.type + ') 의 답을 화면에서 읽지 못했습니다. answers 로 직접 지정하세요 (비워 둘 거면 빈 문자열 \'\').');
      }
      vals = vals.map(String).filter((v) => v !== '');
      if (q.type === 1) vals = vals.map((v) => v.replace(/\r?\n/g, '\r\n'));
      if (!vals.length && q.required) problems.push('필수 질문 "' + q.title + '" 이(가) 비어 있습니다.');
      if (vals.length > 1 && q.type !== 4 && q.type !== 7) problems.push('"' + q.title + '" 에는 값을 하나만 넣을 수 있습니다.');
      if (q.options && q.options.length && [2, 3, 4, 5, 7].includes(q.type)) {
        for (const v of vals) {
          if (v === '__other_option__' && q.hasOther) {
            if (!have(q.name + '.other_option_response')) problems.push('"' + q.title + '" 에서 "기타"를 골랐습니다. 기타 내용은 answers 에 \'' + q.name + ".other_option_response' 로 지정하세요.");
          } else if (!q.options.includes(v)) {
            problems.push('"' + q.title + '" 의 값 "' + v + '" 은(는) 선택지에 없습니다. 가능한 값: ' + q.options.join(', '));
          }
        }
      }
      for (const v of vals) entries.push([q.name, v]);
      rows.push({ 질문: q.title, 필드: q.name, 값: vals.join(' | ') || '(비어 있음)', 출처: src, 필수: q.required ? '예' : '' });
    }
    if (!entries.length && !problems.length) problems.push('답변이 하나도 없습니다 (빈 응답을 제출하게 됩니다). 설문을 채우거나 answers 를 지정한 뒤 다시 실행하세요.');
    // everything else the page itself would post, exactly as given (fvv, partialResponse, pageHistory, fbzx, submissionTimestamp, sentinels ...)
    const rest = fd.filter((p) => !byName.has(p[0]) && !extraNames.has(p[0]));
    const pairs = entries.concat(extra, rest);
    const usp = new URLSearchParams();
    for (const [k, v] of pairs) usp.append(k, v);
    const email = fd.find((p) => p[0] === 'emailAddress');
    if (email && !email[1]) notes.push('이 설문은 이메일 주소를 받는데 비어 있습니다.');
    return Object.assign({ problems, notes, rows, pairs, body: usp.toString(), questions: qs }, facts);
  }

  // ---------------------------------------------------------------- target time
  const AT_EXAMPLE = "예) at: '2026-11-23 22:00:00'  (밤 10시 = 22:00:00, 오전 10시 = 10:00:00)";
  // Syntax only (no clock needed). Returns { rel: seconds } or { abs: epoch ms }.
  function parseAt(at, dryRun) {
    if (typeof at !== 'string' || !at.trim()) throw new Stop('ABORTED', 'AT_FORMAT', 'SETTINGS 의 at 에 목표 시각을 따옴표로 감싼 문자열로 적어 주세요. ' + AT_EXAMPLE);
    const s = at.trim(); let m;
    if ((m = /^\+\s*(\d+(?:\.\d+)?)\s*s?$/.exec(s))) {
      if (!dryRun) throw new Stop('ABORTED', 'AT_RELATIVE_REAL', "'" + s + "' (N초 뒤) 형식은 연습(dryRun: true) 전용입니다. 실제 제출에는 날짜와 시각을 정확히 적어야 합니다. " + AT_EXAMPLE);
      const sec = parseFloat(m[1]);
      if (!(sec >= 1 && sec <= 86400)) throw new Stop('ABORTED', 'AT_FORMAT', "'+N' 의 N 은 1 ~ 86400 (초) 사이여야 합니다.");
      return { rel: sec };
    }
    if (/^(오전|오후|[ap]m)?\s*\d{1,2}(:\d{1,2}){1,2}(\.\d+)?\s*([ap]m)?$/i.test(s) || /^\d{1,2}\s*시/.test(s)) {
      throw new Stop('ABORTED', 'AT_BARE_TIME', "at: '" + s + "' 처럼 시각만 적으면 받아들이지 않습니다. 오전/오후나 날짜를 잘못 짚으면 되돌릴 수 없기 때문입니다. 날짜까지 'YYYY-MM-DD HH:MM:SS' (24시간제) 로 적어 주세요. " + AT_EXAMPLE);
    }
    m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?\s*(Z|[+-]\d{2}:?\d{2})?$/i.exec(s);
    if (!m) throw new Stop('ABORTED', 'AT_FORMAT', "at: '" + s + "' 을(를) 이해하지 못했습니다. 'YYYY-MM-DD HH:MM:SS' (24시간제, 초까지) 또는 시간대를 붙인 ISO 형식만 됩니다. " + AT_EXAMPLE);
    const y = +m[1], mo = +m[2], d = +m[3], hh = +m[4], mi = +m[5], ss = +m[6], ms = m[7] ? +(m[7] + '00').slice(0, 3) : 0;
    const bad = () => new Stop('ABORTED', 'AT_FORMAT', "at: '" + s + "' 은(는) 존재하지 않는 날짜/시각입니다 (시는 00~23). " + AT_EXAMPLE);
    if (mo < 1 || mo > 12 || d < 1 || d > 31 || hh > 23 || mi > 59 || ss > 59) throw bad();
    if (m[8]) {
      const u = new Date(Date.UTC(y, mo - 1, d, hh, mi, ss, ms));
      if (u.getUTCMonth() !== mo - 1 || u.getUTCDate() !== d) throw bad();
      let offMin = 0;
      if (!/^z$/i.test(m[8])) { const o = /^([+-])(\d{2}):?(\d{2})$/.exec(m[8]); offMin = (o[1] === '-' ? -1 : 1) * (+o[2] * 60 + +o[3]); }
      return { abs: u.getTime() - offMin * 60000, zone: m[8].toUpperCase() };
    }
    const l = new Date(y, mo - 1, d, hh, mi, ss, ms);
    if (l.getFullYear() !== y || l.getMonth() !== mo - 1 || l.getDate() !== d || l.getHours() !== hh || l.getMinutes() !== mi) throw bad();
    return { abs: l.getTime(), zone: null };
  }
  function describeTarget(T, serverNow) {
    return [
      '목표 시각 T (Google 서버 시계 기준)',
      '   ' + fmtDate(T) + '   [이 PC 시간대 ' + tzLabel(T) + ']',
      '   = UTC ' + fmtUtc(T),
      '   남은 시간: ' + fmtLeft(T - serverNow),
    ];
  }
  // Local release instant for target T from the current clock state (evaluated at the release itself,
  // approached from the late side so rounding can only delay it).
  function releaseTime(T) {
    const g = CONFIG.guardMs - CONFIG.leadMs;
    let t = T - clock.bounds(now()).lo + g + 1000;
    for (let i = 0; i < 3; i++) t = T - clock.bounds(t).lo + g;
    return t;
  }

  // ---------------------------------------------------------------- outcome of the one POST
  function classify(status, finalUrl, body) {
    let host = '', path = '';
    try { const u = new URL(finalUrl); host = u.hostname; path = u.pathname; } catch (e) { /* ignore */ }
    body = body || '';
    if (host === 'accounts.google.com' || /\/ServiceLogin|\/signin\//.test(path)) return ['LOGIN', '로그인 화면으로 넘어갔습니다. 기록되지 않았습니다 (로그인이 필요한 설문).'];
    if (status === 429 || /\/sorry\//.test(path)) return ['RATE_LIMITED', '서버가 요청이 너무 많다고 거절했습니다 (HTTP ' + status + '). 기록되지 않은 것으로 보입니다.'];
    const closedMsg = '설문이 닫혀 있어 기록되지 않았습니다. (아직 열리지 않은 것이라면, 열린 뒤 페이지를 새로고침해서 직접 제출하세요.)';
    if (/\/closedform$/.test(path)) return ['CLOSED', closedMsg];
    const formPage = /FB_PUBLIC_LOAD_DATA_/.test(body) && /name="fbzx"/.test(body);
    if (formPage) return ['REJECTED', '설문 페이지가 다시 돌아왔습니다 = 기록되지 않았습니다 (필수 항목 누락이나 값 오류).'];
    if (status === 400) return ['REJECTED', '서버가 요청을 거절했습니다 (HTTP 400). 기록되지 않았습니다.'];
    const recorded = RE_RECORDED.test(body);
    if (status === 200 && /\/formResponse$/.test(path)) {
      if (recorded) return ['RECORDED', '응답이 기록되었습니다.'];
      if (RE_CLOSED.test(body)) return ['CLOSED', closedMsg];
      if (!body) return ['UNKNOWN', 'HTTP 200 을 받았지만 본문을 읽지 못했습니다. 기록 여부를 직접 확인하세요.'];
      return ['RECORDED_PROBABLE', '기록된 것으로 보입니다 (HTTP 200, 설문 페이지가 아님). 다만 알려진 확인 문구는 없었습니다 (설문 작성자가 확인 메시지를 바꾼 경우).'];
    }
    if (RE_CLOSED.test(body)) return ['CLOSED', closedMsg];
    if (status >= 400 && status < 500) return ['REJECTED', '서버가 요청을 거절했습니다 (HTTP ' + status + '). 기록되지 않은 것으로 보입니다.'];
    return ['UNKNOWN', 'HTTP ' + status + ' 응답으로는 기록 여부를 판단할 수 없습니다.'];
  }
  // After a POST that ended in a network error: read-only look at what the origin says now (never /formResponse).
  async function diagnose() {
    const probe = async (url, method) => {
      if (FORBIDDEN.test(url)) return { error: 'forbidden' };
      const ac = new AbortController(); const k = setTimeout(() => ac.abort(), 5000);
      try { const r = await fetch(url, { method, cache: 'no-store', credentials: 'same-origin', signal: ac.signal }); return { status: r.status, url: r.url }; }
      catch (e) { return { error: String(e && e.message ? e.message : e) }; }
      finally { clearTimeout(k); }
    };
    const origin = await probe(FINE_URL, 'HEAD');
    const view = await probe(VIEW_URL, 'GET');
    return { origin, view };
  }

  // ---------------------------------------------------------------- page
  let form = null, VIEW_URL = '', ACTION_URL = '', FINE_URL = '';
  function locateForm() {
    form = document.querySelector('form[action*="formResponse"]');
    if (!form) {
      const closed = /\/closedform$/.test(location.pathname) || RE_CLOSED.test(document.body ? document.body.textContent : '');
      if (closed) throw new Stop('CLOSED', 'PAGE_CLOSED', '이 설문은 지금 닫혀 있습니다 (응답을 받지 않는 상태). 이 스니펫에는 설문이 열리기를 기다리는 기능이 없습니다. 설문이 열린 뒤 페이지를 새로고침하고 다시 실행하세요. 아무것도 보내지 않았습니다.');
      throw new Stop('NO_FORM', 'NO_FORM', '이 페이지에서 Google 설문 양식을 찾지 못했습니다. 설문 작성 화면(…/viewform)에서 실행하세요. 아무것도 보내지 않았습니다.');
    }
    const here = new URL(location.href);
    const action = new URL(form.getAttribute('action') || '', location.href);
    if (action.origin !== here.origin || !/\/formResponse$/.test(action.pathname)) {
      throw new Stop('NO_FORM', 'NO_FORM', '이 페이지의 양식은 Google 설문 형식이 아닙니다 (제출 주소: ' + action.href + '). 아무것도 보내지 않았습니다.');
    }
    ACTION_URL = action.origin + action.pathname;
    VIEW_URL = here.origin + (/\/viewform$/.test(here.pathname) ? here.pathname : action.pathname.replace(/\/formResponse$/, '/viewform'));
    FINE_URL = here.origin + '/generate_204';
    if (FORBIDDEN.test(VIEW_URL) || FORBIDDEN.test(FINE_URL)) throw new Stop('NO_FORM', 'NO_FORM', '설문 주소를 해석하지 못했습니다. 아무것도 보내지 않았습니다.');
  }
  function checkConfig() {
    const known = new Set(Object.keys(SETTINGS).concat(Object.keys(EXPERT)));
    const unknown = Object.keys(CONFIG).filter((k) => !known.has(k));
    const fail = (msg) => { throw new Stop('ABORTED', 'CONFIG', msg); };
    if (unknown.length) fail('알 수 없는 설정: ' + unknown.join(', ') + ' (mode / marginMs / force / confirm 옵션은 없어졌습니다).');
    if (typeof CONFIG.dryRun !== 'boolean') fail('dryRun 은 true 또는 false 여야 합니다 (따옴표 없이).');
    if (typeof CONFIG.inspectOnly !== 'boolean' || typeof CONFIG.fineClock !== 'boolean') fail('inspectOnly / fineClock 은 true 또는 false 여야 합니다.');
    if (typeof CONFIG.guardMs !== 'number' || !(CONFIG.guardMs >= 0 && CONFIG.guardMs <= 2000)) fail('guardMs 는 0 ~ 2000 사이의 숫자여야 합니다.');
    if (typeof CONFIG.leadMs !== 'number' || !isFinite(CONFIG.leadMs)) fail('leadMs 는 숫자여야 합니다.');
    if (CONFIG.leadMs < 0) fail('leadMs 는 음수일 수 없습니다 (늦추려면 guardMs 를 키우세요).');
    if (CONFIG.leadMs > 2000) fail('leadMs 는 2000 을 넘을 수 없습니다.');
    if (CONFIG.via !== 'fetch' && CONFIG.via !== 'click') fail("via 는 'fetch' 또는 'click' 이어야 합니다.");
    for (const k of ['initialSamples', 'fineSamples', 'idleEveryMs', 'denseFromMs', 'pageEveryMs', 'lastSampleMs', 'spinMs', 'minGapMs']) {
      if (typeof CONFIG[k] !== 'number' || !(CONFIG[k] >= 0)) fail(k + ' 는 0 이상의 숫자여야 합니다.');
    }
    CONFIG.minGapMs = Math.max(500, CONFIG.minGapMs);          // never more than 2 requests per second
    CONFIG.initialSamples = Math.max(1, Math.round(CONFIG.initialSamples));
    CONFIG.spinMs = Math.min(Math.max(CONFIG.spinMs, 20), 1500);
    CONFIG.lastSampleMs = Math.max(CONFIG.lastSampleMs, CONFIG.spinMs + 600);
    CONFIG.idleEveryMs = Math.max(CONFIG.idleEveryMs, 5000);
  }

  gfo._ = { clock, parseAt, classify, collect: () => collect(form), loadQuestions, readDom, releaseTime, fmtDate, describeTarget, state };

  // ================================================================ main flow
  const base = () => ({ version: VERSION, dryRun: CONFIG.dryRun, via: CONFIG.via, guardMs: CONFIG.guardMs, leadMs: CONFIG.leadMs });
  let T = null, payload = null;
  try {
    checkConfig();
    if (CONFIG.libOnly) { try { locateForm(); } catch (e) { /* lib mode works without a form */ } cleanup(); return 'lib'; }
    if (EXT) log('window.GFO_CONFIG 의 설정을 사용합니다:', EXT);
    locateForm();
    if (location.hostname !== 'docs.google.com') warn('이 페이지는 docs.google.com 이 아닙니다 (' + location.host + ').');

    // ---- 1. what would be sent
    payload = collect(form);
    console.table(payload.rows);
    console.table(payload.pairs.map((p, i) => ({ 순서: i + 1, 필드: p[0], 값: p[1] })));
    log('전송될 본문 (application/x-www-form-urlencoded):\n' + payload.body);
    for (const n of payload.notes) warn(n);
    if (CONFIG.inspectOnly) {
      for (const p of payload.problems) warn('문제: ' + p);
      log('inspectOnly: 위 내용을 보여 주기만 하고 멈춥니다. 아무것도 보내지 않았습니다.');
      return finish(Object.assign(base(), { status: 'INSPECT', detail: '보낼 내용만 확인했습니다.', payload: { fields: payload.pairs, body: payload.body, table: payload.rows }, problems: payload.problems }));
    }
    if (payload.problems.length) {
      if (CONFIG.via === 'click') for (const p of payload.problems) warn("문제 (via:'click' 이라 계속 진행): " + p);
      else throw new Stop('ABORTED', 'PAYLOAD', '보낼 내용에 문제가 있어 중단했습니다. 아무것도 보내지 않았습니다.\n - ' + payload.problems.join('\n - '));
    }
    if (CONFIG.via === 'fetch' && (payload.captcha || payload.multiPage)) {
      throw new Stop('ABORTED', 'NEEDS_CLICK', '이 설문은 ' + (payload.captcha ? '제출할 때 캡차를 실행' : '여러 페이지로 구성') + '되어 있어 fetch 방식으로는 보낼 수 없습니다. (검증되지 않은 예비 수단: 전문가용 옵션 via: \'click\') 아무것도 보내지 않았습니다.');
    }
    if (CONFIG.via === 'click' && !payload.submitButton) throw new Stop('ABORTED', 'NO_SUBMIT_BUTTON', "via:'click' 인데 [제출] 버튼을 찾지 못했습니다. 아무것도 보내지 않았습니다.");
    if (!CONFIG.dryRun && gfo.submitted) {
      throw new Stop('ALREADY_SUBMITTED', 'ALREADY_SUBMITTED', '이 페이지에서는 이미 한 번 제출(발사)했습니다. 이 도구는 다시 제출하지 않습니다. 이전 결과: ' + (prev && prev.result ? prev.result.status : '?') + '. (정말 다시 내야 한다면 페이지를 새로고침한 뒤 실행하세요.)');
    }

    // ---- 2. target time: syntax first (no network needed to refuse a bad one)
    const at = parseAt(CONFIG.at, CONFIG.dryRun);

    // ---- 3. first look at the server clock
    ui();
    if (document.hidden) { state.hiddenSeen = true; warn('이 탭이 지금 화면에 보이지 않습니다. 가려진 탭은 타이머가 느려져 늦게 발사될 수 있습니다. 탭을 화면에 띄워 두세요.'); }
    setStatus('서버 시계 측정 중...');
    state.fineOn = CONFIG.fineClock;
    for (let i = 0; i < CONFIG.initialSamples && !state.rateLimited; i++) { checkCancel(); await takeSample('page'); }
    checkCancel();
    let b = clock.bounds(now());
    if (!b.n && state.fineOn) { await takeSample('fine'); await takeSample('fine'); b = clock.bounds(now()); }
    if (!b.n) throw new Stop('ERROR', 'NO_CLOCK', '서버 시계를 측정하지 못했습니다 (네트워크 연결, 로그인 상태를 확인하세요). 아무것도 제출하지 않았습니다.');
    const serverNow = now() + (b.lo + b.hi) / 2;
    T = state.T = at.rel !== undefined ? Math.ceil((serverNow + at.rel * 1000) / 1000) * 1000 : at.abs;
    const pcAhead = Date.now() - serverNow;
    const targetLines = describeTarget(T, serverNow);
    if (T <= serverNow) {
      throw new Stop('ABORTED', 'AT_PAST', targetLines.slice(0, 3).join('\n') + '\n이 시각은 서버 시계로 이미 ' + fmtLeft(serverNow - T) + ' 전에 지났습니다. 날짜와 오전/오후(24시간제)를 확인하세요. 아무것도 보내지 않았습니다.');
    }
    log('서버 시각은 지금 ' + fmtDate(serverNow) + ' 입니다 (오차 폭 ' + b.width.toFixed(1) + ' ms, ' + (state.hasMs ? '밀리초 시계' : 'Date 헤더(초 단위)') + ' 사용). 이 PC 시계는 서버보다 ' + (pcAhead >= 0 ? '+' : '') + (pcAhead / 1000).toFixed(3) + '초.');

    // ---- 4. show the plan, ask
    const leadWarn = CONFIG.leadMs > 0
      ? '*** 경고: leadMs = ' + CONFIG.leadMs + ' ***\n요청이 서버 시각 ' + fmtRel(CONFIG.guardMs - CONFIG.leadMs) + ' 부터 출발할 수 있습니다.\n"절대 이르지 않음" 보장이 사라집니다. T 보다 일찍 기록되면 무효가 될 수 있습니다.\n본인 설문으로 보정한 값일 때만 진행하세요.'
      : '';
    const fieldLines = payload.pairs.map((p) => {
      const q = payload.questions.find((x) => x.name === p[0]);
      const v = p[1].length > 60 ? p[1].slice(0, 57) + '...' : p[1];
      return '   ' + (q ? q.title + ' [' + p[0] + ']' : p[0]) + ' = ' + (v === '' ? '(빈 값)' : v);
    });
    const summary = [
      CONFIG.dryRun ? '[연습] 실제로는 아무것도 제출하지 않습니다 (발사 순간에 설문 페이지를 한 번 읽기만 합니다).' : '[실제 제출] 아래 내용을 딱 1회 제출합니다. 재시도는 없습니다.',
      '',
    ].concat(targetLines, [
      '',
      '발사 규칙: 서버 시각이 확실히 ' + fmtRel(CONFIG.guardMs - CONFIG.leadMs) + ' 이후일 때 요청 출발' + (CONFIG.leadMs > 0 ? '' : ' (T 보다 이르게 기록될 수 없음)'),
      '방식: ' + (CONFIG.dryRun ? '(연습) 설문 페이지 읽기(GET) 1회. 실제 제출 때는 ' : '') + (CONFIG.via === 'click' ? "설문의 [제출] 버튼 누르기 (via:'click', 검증되지 않은 예비 수단)" : 'fetch POST 1회'),
    ], leadWarn ? ['', leadWarn] : [], [
      '',
      CONFIG.via === 'click' ? '참고용으로 읽은 내용 (실제로는 페이지가 직접 보냅니다):' : (CONFIG.dryRun ? '실제 제출이라면 보낼 내용 (' : '보낼 내용 (') + payload.pairs.length + '개 필드, 순서대로):',
    ], fieldLines,
    T - serverNow > 24 * 3600e3 ? ['', '주의: 목표 시각까지 24시간 넘게 남았습니다. 날짜가 맞는지 확인하세요. 그때까지 이 탭을 닫거나 새로고침하면 안 됩니다.'] : [],
    document.hidden ? ['', '주의: 이 탭이 화면에 보이지 않습니다. 탭을 띄워 두세요.'] : []).join('\n');
    log(summary);
    if (leadWarn) warn(leadWarn);
    if (T - serverNow < 8000) warn('목표 시각까지 ' + fmtLeft(T - serverNow) + ' 밖에 남지 않았습니다. 시계를 충분히 못 재면 그만큼 늦게(이르게는 아님) 발사됩니다.');
    checkCancel();
    if (!window.confirm(summary + '\n\n진행하려면 [확인], 그만두려면 [취소]를 누르세요.')) {
      throw new Stop('CANCELLED', 'DECLINED', '확인 창에서 취소했습니다. 아무것도 제출되지 않았습니다.');
    }
    checkCancel();

    // ---- 5. the one request, built now; nothing below changes it
    const shot = CONFIG.dryRun
      ? { url: VIEW_URL, init: { method: 'GET', cache: 'no-store', credentials: 'same-origin' } }
      : { url: ACTION_URL, init: { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: payload.body, credentials: 'same-origin' } };
    if (CONFIG.dryRun && (FORBIDDEN.test(shot.url) || shot.init.method !== 'GET')) throw new Error('internal: dry run must not touch formResponse');

    // ---- 6. countdown display
    const tick = () => {
      const n = now(); const bb = clock.bounds(n);
      if (!bb.n) return;
      const sv = n + (bb.lo + bb.hi) / 2;
      lines.clock.textContent = '서버 시각 ' + fmtClock(sv).slice(0, 10) + '   (오차 폭 ' + bb.width.toFixed(1) + ' ms, 측정 ' + bb.n + '회' + (state.resets ? ', 재설정 ' + state.resets + '회' : '') + ')';
      lines.plan.textContent = 'T = ' + fmtDate(T) + '  까지 ' + fmtLeft(T - sv) + (document.hidden ? '\n** 탭이 가려져 있습니다! 화면에 띄워 두세요 **' : '');
    };
    ticker = setInterval(tick, 250); tick();
    onVis = () => {
      if (document.hidden && gfo.running && !state.fired) { state.hiddenSeen = true; warn('탭이 가려졌습니다. 타이머가 느려져 늦게 발사될 수 있으니 이 탭을 화면에 보이게 두세요.'); }
    };
    document.addEventListener('visibilitychange', onVis);

    // ---- 7. calibration and waiting
    const rel = () => releaseTime(T);
    // Runs one sample but never waits for it longer than `deadline` (local time). 'skip' = it could not start in time.
    async function sample(kind, latestStart, deadline) {
      const aimed = kind === 'fine' || !state.hasMs;
      const tCall = aimed ? aimTime(kind) : null;
      const start = tCall !== null ? tCall : Math.max(now(), state.lastSampleAt + CONFIG.minGapMs);
      if (start > latestStart) return 'skip';
      return withDeadline(tCall !== null ? takeAimedAt(kind, tCall) : takeSample(kind), deadline);
    }
    const latestStart = () => rel() - CONFIG.lastSampleMs - 800;    // regular samples must have started by then
    const softDeadline = () => rel() - CONFIG.lastSampleMs;          // ... and are not waited for past this

    setStatus('서버 시계 정밀 측정 중...');
    for (let i = 0; state.fineOn && !state.rateLimited && i < CONFIG.fineSamples; i++) {
      const r = await sample('fine', latestStart(), softDeadline());
      if (r !== 'ok') break;
      const bb = clock.bounds(now());
      if (i >= 3 && bb.width <= 1.25 * clock.minRtt('fine') + 1) break;      // as tight as the round trip allows
    }
    b = clock.bounds(now());
    log('서버 시계 보정: 오차 폭 ' + b.width.toFixed(1) + ' ms (측정 ' + b.n + '회' + (state.fineOn ? ', 정밀 시계 사용, 최소 왕복 ' + clock.minRtt('fine').toFixed(1) + ' ms' : ', 정밀 시계 꺼짐') + '). 이제 목표 시각까지 기다립니다.');
    setStatus('대기 중 (주기적으로 서버 시계를 다시 잽니다)');
    let lastPageAt = now();
    for (;;) {
      if (state.rateLimited) break;
      const rest = rel() - now();
      if (now() >= latestStart()) break;
      let kind;
      if (rest > CONFIG.denseFromMs + 1000) {
        await pause(Math.min(CONFIG.idleEveryMs, rest - CONFIG.denseFromMs));
        kind = state.fineOn && (state.k++ % 2 === 0) ? 'fine' : 'page';
      } else if (state.fineOn) {
        kind = now() - lastPageAt >= CONFIG.pageEveryMs ? 'page' : 'fine';
      } else {
        kind = 'page';
        if (state.hasMs) await pause(Math.min(lastPageAt + 1500 - now(), latestStart() - now()));
      }
      if (now() >= latestStart()) break;
      const r = await sample(kind, latestStart(), softDeadline());
      if (kind === 'page') lastPageAt = now();
      if (r !== 'ok') break;
    }

    // ---- 8. keep-warm: one page GET about lastSampleMs before the release, the last request before the shot
    state.quiet = true;                                   // console output could cost milliseconds: buffer it from here
    if (!state.rateLimited) {
      await pause(rel() - CONFIG.lastSampleMs - now());
      // (a hidden tab only gets timer wake-ups about once per second: no keep-warm there, straight to the long busy-wait)
      if (!document.hidden && rel() - now() > CONFIG.spinMs + 500) {
        // never wait on a pending sample past the start of the busy-wait
        await withDeadline(takeSample('page'), rel() - CONFIG.spinMs - 150);
      }
    }
    state.noMoreSamples = true;

    // ---- 9. final approach: timers until spinMs before the release, then a busy-wait
    setStatus('발사 대기 중...');
    let tRel;
    for (;;) {
      checkCancel();
      tRel = rel();
      const rest = tRel - now();
      if (document.hidden) state.hiddenSeen = true;
      const spin = document.hidden ? 2200 : CONFIG.spinMs;      // hidden tabs only get timer wake-ups about once per second
      if (rest <= spin) break;
      await rawSleep(Math.min(rest - spin, 100));
    }
    state.frozen = true; state.phase = 'spin';
    clearInterval(ticker); ticker = null;
    tRel = rel();
    const bRel = clock.bounds(tRel);
    const plan = {
      tRel, thetaLo: bRel.lo, thetaHi: bRel.hi, widthMs: bRel.width, samples: bRel.n, resets: state.resets,
      resetInLast3s: state.lastResetAt !== null && tRel - state.lastResetAt < 3000,
      fineClock: state.fineOn, fineDropped: state.fineDropped, clockSource: state.hasMs ? 'server_time_ms' : 'date',
      minRttPageMs: clock.minRtt('page'), minRttFineMs: clock.minRtt('fine'), requests: state.requests, rateLimited: state.rateLimited,
    };
    lines.plan.textContent = 'T = ' + fmtDate(T) + '  발사: 서버 시각 ' + fmtRel(tRel + bRel.lo - T) + ' ~ ' + fmtRel(tRel + bRel.hi - T);
    const res = Object.assign(base(), {
      status: 'UNKNOWN', detail: '', T, target: fmtDate(T), targetUtc: fmtUtc(T), neverEarly: CONFIG.leadMs === 0,
      clock: plan, hiddenSeen: state.hiddenSeen, closedSeen: state.closedSeen,
      payload: { fields: payload.pairs, body: payload.body },
    });
    checkCancel();

    // ---- 10. fire (everything from here to fetch() is synchronous)
    let tCall, p = null;
    if (CONFIG.via === 'click' && !CONFIG.dryRun) {
      const btn = payload.submitButton;
      spinUntil(tRel);
      tCall = now();
      state.fired = true; gfo.submitted = true;
      btn.click();                                     // untested fallback: the page's own code validates and submits
    } else {
      spinUntil(tRel);
      tCall = now();
      state.fired = true; if (!CONFIG.dryRun) gfo.submitted = true;
      p = fetch(shot.url, shot.init);                  // <- the one and only request of the shot
    }

    // ---- 11. report
    const bCall = clock.bounds(tCall);
    res.release = { plannedLocal: tRel, actualLocal: tCall, lateMs: tCall - tRel, serverLoRelT: tCall + bCall.lo - T, serverHiRelT: tCall + bCall.hi - T };
    const releaseLine = '요청 출발 (서버 시계): ' + fmtRel(res.release.serverLoRelT) + ' ~ ' + fmtRel(res.release.serverHiRelT) + '   [' + fmtClock(tCall + bCall.lo) + ' ~ ' + fmtClock(tCall + bCall.hi) + ']';
    const shown = [];
    try {
      if (!p) {
        res.status = 'CLICKED';
        res.detail = "[제출] 버튼을 눌렀습니다 (via:'click', 검증되지 않은 방식). 결과는 화면에서 직접 확인하세요.";
        shown.push('[CLICKED] ' + res.detail, '목표 T = ' + fmtDate(T), releaseLine);
      } else {
        setStatus('요청을 보냈습니다. 응답을 기다리는 중...');
        let r = null, body = '', netErr = null, tHdr = null;
        // stop WAITING after a while (the request itself is never aborted or repeated)
        const within = (pr, ms, what) => Promise.race([pr, rawSleep(ms).then(() => { throw new Error(what); })]);
        try { r = await within(p, 30000, '30초 동안 응답 없음'); tHdr = now(); } catch (e) { netErr = e; }
        if (r) { try { body = await within(r.text(), 15000, '응답 본문을 15초 안에 다 받지 못함'); } catch (e) { res.bodyError = String(e && e.message ? e.message : e); } }
        const e = await rtWait(shot.url, tCall);
        let leftAt = tCall;
        if (e && e.requestStart >= tCall - 0.2 && (tHdr === null || e.requestStart <= tHdr)) {
          leftAt = Math.max(tCall, e.requestStart);
          res.request = { startDelayMs: e.requestStart - tCall, newConnection: e.connectEnd > e.connectStart, protocol: e.nextHopProtocol };
        }
        let S = null;
        if (r) {
          const m = RE_ST.exec(body); S = m ? Number(m[1]) : null;
          const dh = r.headers.get('date');
          res.response = { http: r.status, finalUrl: r.url, dateHeader: dh, serverTimeMs: S, serverTimeRelT: S === null ? null : S - T, headersLocal: tHdr };
        }
        // the stamp cannot precede the departure and cannot follow the confirmation page / the response headers
        const loAbs = leftAt + bCall.lo;
        const hiAbs = r ? Math.min(S === null ? Infinity : S + 1, tHdr + clock.bounds(tHdr).hi) : null;
        if (CONFIG.dryRun) {
          if (netErr) {
            res.status = 'DRYRUN_FAILED'; res.detail = '연습 요청이 실패했습니다: ' + (netErr.message || netErr); res.error = String(netErr.message || netErr);
            shown.push('[연습 실패] ' + res.detail, releaseLine);
          } else {
            res.status = 'DRYRUN';
            res.landingMs = S === null ? null : S - T;
            const D = r.headers.get('date') ? Date.parse(r.headers.get('date')) : NaN;
            res.dateHeaderAtOrAfterT = isNaN(D) ? null : D >= T;       // Date = floor(stamp): D >= T proves "handled at or after T"
            res.detail = S === null
              ? '이 순간 보낸 요청의 처리 시각을 밀리초로는 알 수 없습니다 (페이지에 server_time_ms 없음). 응답 Date 헤더: ' + r.headers.get('date') + (res.dateHeaderAtOrAfterT ? '  = T 이후에 처리된 것이 확인됨' : '')
              : '이 순간 보낸 요청을 Forms 서버가 처리한 시각: ' + fmtRel(res.landingMs).replace(/\.0 ms$/, ' ms') + '   [' + fmtClock(S) + ']';
            shown.push('[연습 완료] 아무것도 제출하지 않았습니다.', '목표 T = ' + fmtDate(T), releaseLine, res.detail);
            if (S !== null) {
              // how long after leaving this PC the Forms backend read its clock for this request
              res.serverDelayMs = { lo: S - (leftAt + bCall.hi), hi: S + 1 - (leftAt + bCall.lo) };
              shown.push('출발부터 서버가 처리하기까지: 약 ' + Math.max(0, res.serverDelayMs.lo).toFixed(0) + ' ~ ' + res.serverDelayMs.hi.toFixed(0) + ' ms' + (isFinite(plan.minRttPageMs) ? '  (보정 중 설문 페이지 왕복: 최소 ' + plan.minRttPageMs.toFixed(0) + ' ms)' : ''));
              if (res.serverDelayMs.lo > 150) shown.push('참고: 설문 서버가 이번 요청을 느리게 처리했습니다. 연습을 한두 번 더 해 보세요. 계속 느리면 시크릿 창에서 설문을 새로 열어 연습해 보면 더 빠른 서버에 배정될 수도 있습니다 (확실하지는 않습니다).');
            }
            if (res.request) shown.push('연결: ' + (res.request.protocol || '?') + (res.request.newConnection ? ', 새 연결을 맺음 (느려짐)' : ', 기존 연결 재사용') + ', fetch 호출 -> 전송 ' + res.request.startDelayMs.toFixed(1) + ' ms');
            shown.push('실제 제출(POST)도 이 정도, 또는 서버의 기록 처리 시간만큼 더 늦게 찍힐 것으로 예상됩니다.');
            if (S !== null && S < T && CONFIG.leadMs === 0) shown.push('*** 경고: 연습 요청이 T 이전에 처리되었습니다. 있어서는 안 되는 일입니다. 실제 제출에 쓰지 말고 문의하세요. ***');
          }
        } else if (netErr) {
          res.error = String(netErr.message || netErr);
          res.diagnosis = await diagnose();
          const dg = res.diagnosis;
          if (dg.origin.status && dg.view.error) {
            res.status = 'LOGIN';
            res.detail = '제출 요청이 다른 사이트(로그인 화면으로 추정)로 넘겨져 읽을 수 없었습니다. 기록되지 않았을 가능성이 높습니다. 로그인 상태를 확인하세요. (추정)';
          } else {
            res.status = 'UNKNOWN';
            res.detail = '응답을 받지 못했습니다 (' + res.error + '). 기록되었는지 알 수 없습니다. 다시 제출하지 마세요 (중복 응답 위험). 주최자에게 확인하거나 결과를 기다리세요.';
          }
          shown.push('[' + res.status + '] ' + res.detail, '목표 T = ' + fmtDate(T), releaseLine);
        } else {
          const c = classify(r.status, r.url, body);
          res.status = c[0]; res.detail = c[1];
          shown.push('[' + c[0] + '] ' + c[1], '목표 T = ' + fmtDate(T), releaseLine);
          if (S !== null) shown.push('응답 페이지 생성 시각 (서버): ' + fmtRel(S - T).replace(/\.0 ms$/, ' ms') + '   [' + fmtClock(S) + ']' + (c[0].indexOf('RECORDED') === 0 ? '  = 기록 시각의 상한' : ''));
          if (c[0] === 'RECORDED' || c[0] === 'RECORDED_PROBABLE') {
            res.stamp = { loMs: loAbs, hiMs: hiAbs, loRelT: loAbs - T, hiRelT: hiAbs - T };
            res.stamp.text = '기록 시각은 ' + fmtClock(loAbs) + ' ~ ' + fmtClock(hiAbs) + ' 사이입니다 (' + fmtRel(loAbs - T) + ' ~ ' + fmtRel(hiAbs - T) + ')';
            shown.push(res.stamp.text);
            if (loAbs < T) shown.push('*** 주의: leadMs 때문에 T 이전에 기록되었을 가능성이 있습니다. ***');
            shown.push('참고: 응답 시트의 기본 표시는 초 단위 반올림입니다. 설문의 [제출] 버튼은 누르지 마세요.');
          } else if (c[0] === 'UNKNOWN') {
            shown.push('다시 제출하지 마세요 (중복 응답 위험). 주최자에게 확인하거나 결과를 기다리세요.');
          }
          if (c[0] === 'RECORDED' || c[0] === 'RECORDED_PROBABLE' || c[0] === 'UNKNOWN') {
            const sb = payload.submitButton;
            if (sb) { sb.style.pointerEvents = 'none'; sb.style.opacity = '0.3'; }
          }
        }
        if (res.release.lateMs > 5) shown.push('참고: 발사가 계획보다 ' + res.release.lateMs.toFixed(1) + ' ms 늦었습니다 (탭이 가려졌거나 PC가 바빴음).');
      }
    } catch (e2) {
      res.status = CONFIG.dryRun ? 'DRYRUN_FAILED' : 'UNKNOWN';
      res.error = String(e2 && e2.message ? e2.message : e2);
      res.detail = (CONFIG.dryRun ? '연습 요청은 나갔지만' : '요청은 이미 나갔지만') + ' 결과를 정리하다가 오류가 났습니다: ' + res.error + (CONFIG.dryRun ? '' : ' 다시 제출하지 말고 기록 여부를 직접 확인하세요.');
      shown.push('[' + res.status + '] ' + res.detail, releaseLine);
    }
    if (plan.resets) shown.push('참고: 시계 재설정 ' + plan.resets + '회' + (plan.resetInLast3s ? ' (마지막 3초 안에 발생: 느슨한 측정값으로 늦게 발사했습니다)' : ''));
    if (plan.fineDropped) shown.push('참고: 정밀 시계가 페이지 시계와 맞지 않아 꺼진 상태로 발사했습니다.');
    if (state.hiddenSeen) shown.push('참고: 실행 중 탭이 가려진 적이 있습니다.');
    res.messages = shown;
    setStatus(shown.join('\n'));
    flushLogs();
    log(shown.join('\n'));
    log('자세한 값: gfo.result');
    return finish(res);
  } catch (err) {
    flushLogs();
    if (err instanceof Stop) {
      const msg = err.message;
      if (err.status === 'CANCELLED') log(msg); else error(msg);
      setStatus((err.status === 'CANCELLED' ? '취소됨: ' : '중단: ') + msg);
      return finish(Object.assign(base(), { status: err.status, code: err.code, detail: msg, T, fired: state.fired }));
    }
    const msg = String(err && err.message ? err.message : err);
    error('예상하지 못한 오류로 중단했습니다:', err);
    const tail = state.fired ? (CONFIG.dryRun ? ' (연습 요청은 이미 나갔습니다.)' : ' 요청은 이미 나갔습니다. 다시 제출하지 말고 기록 여부를 직접 확인하세요.') : ' 아무것도 제출하지 않았습니다.';
    setStatus('오류: ' + msg + tail);
    return finish(Object.assign(base(), { status: state.fired && !CONFIG.dryRun ? 'UNKNOWN' : 'ERROR', code: 'EXCEPTION', detail: msg + tail, error: msg, T, fired: state.fired }));
  }
})();
