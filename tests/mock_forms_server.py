"""A local stand-in for Google Forms, used only for testing the timing logic.

It imitates the parts of docs.google.com that the tool relies on (shapes copied from real, read-only
observations of Google Forms in November 2026):

* ``HEAD /generate_204``                 answered by the "front end": 204 + ``Date`` (whole seconds, floor)
* ``GET  /forms/d/e/<id>/viewform``      form page with FB_PUBLIC_LOAD_DATA_, hidden inputs, cookies and the
                                          millisecond backend clock (``"server_time_ms":...``);
                                          302 -> ``closedform`` while the form is closed
* ``GET  /forms/d/e/<id>/closedform``    "no longer accepting responses" page (also carries server_time_ms)
* ``POST /forms/d/e/<id>/formResponse``  records a response stamped with the *server* clock
* ``GET  /spreadsheets/d/<sid>/gviz/tq`` response sheet readback (hour/minute/second/millisecond functions)
* ``GET  /__mock/responses``             JSON dump of everything recorded (for assertions)

The server clock deliberately differs from the local clock (``skew_ms``). Every request is delayed like a
real path: ``up_ms`` before the server sees it and ``down_ms`` before the answer arrives (plus random
``jitter_ms`` per leg). Requests that reach the Forms "backend" (everything except /generate_204) wait an
extra ``backend_ms`` (+ ``backend_jitter_ms``) before the handler runs, and a POST is stamped ``proc_ms``
after its handler starts. A client can therefore only hit a target instant by calibrating against the server.

Any GET/HEAD to /formResponse is counted in ``state.violations`` - the tool must never do that.

Run standalone:  python tests/mock_forms_server.py --port 8765
"""
from __future__ import annotations

import argparse
import datetime as _dt
import email.utils
import json
import random
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FORM_ID = "1FAIpQLSmockmockmockmockmockmockmockmockmockmockmockmockmock"
SHEET_ID = "1MockSheetIdMockSheetIdMockSheetIdMockSheetId"
ENTRY_GRADE = 405927314
ENTRY_NAME = 536164697
RECORDED_MARKER = "응답이 기록되었습니다."
CLOSED_MARKER = "양식에서 더 이상 응답을 받지 않습니다."
BASE = "/forms/d/e/%s/" % FORM_ID


class MockState:
    def __init__(self, *, skew_ms=0.0, up_ms=0.0, down_ms=0.0, jitter_ms=0.0, backend_ms=0.0,
                 backend_jitter_ms=0.0, proc_ms=0.0, date_lag_ms=0.0, open_at_server_ms=None,
                 tz_offset_min=540, seed=1, expose_server_time=True, backend_modes=(0.0,),
                 name_required=False, rate_limited=False, drop_post_response=False):
        self.skew_ms = float(skew_ms)
        self.up_ms = float(up_ms)
        self.down_ms = float(down_ms)
        self.jitter_ms = float(jitter_ms)
        self.backend_ms = float(backend_ms)
        self.backend_jitter_ms = float(backend_jitter_ms)
        self.proc_ms = float(proc_ms)                 # POST: handler start -> recorded timestamp
        self.date_lag_ms = float(date_lag_ms)         # pages: Date header is stamped this long after the page clock
        self.open_at_server_ms = open_at_server_ms    # None = always open
        self.tz_offset_min = tz_offset_min            # time zone of the "response sheet" (KST = +540)
        self.expose_server_time = expose_server_time  # False: pages carry no server_time_ms (Date only)
        self.backend_modes = tuple(backend_modes)     # extra backend delay per issued COMPASS cookie (cycled)
        self.name_required = name_required
        self.rate_limited = rate_limited              # True: every backend request answers 429
        self.drop_post_response = drop_post_response  # True: record the POST, then hang up without answering
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.responses = []                           # dicts, in commit order
        self.requests = []                            # (method, path, arrival_server_ms)
        self.violations = []                          # GET/HEAD to /formResponse
        self.fbzx_counter = 1000
        self.jar_counter = 0

    def now_ms(self) -> float:
        return time.time() * 1000.0 + self.skew_ms

    def is_open(self) -> bool:
        return self.open_at_server_ms is None or self.now_ms() >= self.open_at_server_ms

    def _sleep(self, base_ms: float, jitter_ms: float) -> None:
        with self.lock:
            j = self.rng.random() * jitter_ms
        delay = (base_ms + j) / 1000.0
        if delay > 0:
            time.sleep(delay)

    def uplink(self):
        self._sleep(self.up_ms, self.jitter_ms)

    def downlink(self):
        self._sleep(self.down_ms, self.jitter_ms)

    def backend(self, cookie_header: str):
        extra = 0.0
        m = re.search(r"COMPASS=b(\d+)", cookie_header or "")
        if m:
            extra = self.backend_modes[int(m.group(1)) % len(self.backend_modes)]
        else:
            extra = max(self.backend_modes)           # no affinity cookie: you may hit the slow backend
        self._sleep(self.backend_ms + extra, self.backend_jitter_ms)


def _http_date(ms: float) -> str:
    return email.utils.formatdate(int(ms // 1000), usegmt=True)     # whole seconds, floor


def _page_scripts(state: MockState, ms: float) -> str:
    if not state.expose_server_time:
        return ""
    return ('<script>_docs_flag_initialData={"docs-ails":"docs_cold","info_params":{},'
            '"sstu":%d,"docs-ewbs":false,"server_time_ms":%d,"gaia_session_id":""};</script>'
            % (int(ms * 1000) - 2000, int(ms)))


def _viewform_html(state: MockState, ms: float) -> str:
    with state.lock:
        state.fbzx_counter += 1
        fbzx = "-39127436208489%04d" % state.fbzx_counter
    req = 1 if state.name_required else 0
    load_data = [None, [None, [
        [474880873, "학년", None, 2, [[ENTRY_GRADE, [["1", None, None, None, 0], ["2", None, None, None, 0],
                                                    ["3", None, None, None, 0]], 0, None, None, None, None, None, 0]],
         None, None, None, None, None, None, [None, "학년"]],
        [925209746, "이름", None, 0, [[ENTRY_NAME, None, req]], None, None, None, None, None, None, [None, "이름"]],
    ], None, None, None, None, None, [None, "mock"], "모의 설문", 76, [None, None, None, 2, None, None, 1]],
        "/forms", "모의 설문", None, None, None, "", None, 0, 0, None, "", 0, "e/" + FORM_ID, 0, "[]", 0, 0, 1]
    return (
        "<!DOCTYPE html><html><head><title>모의 설문</title>%s</head><body>"
        '<form action="%sformResponse" target="_self" method="POST" id="mG61Hd" '
        'data-should-execute-invisible-captcha-on-form-submit="false">'
        '<input type="hidden" name="entry.%d_sentinel">'
        '<input type="hidden" name="fvv" value="1">'
        '<input type="hidden" name="partialResponse" value="[null,null,&quot;%s&quot;]">'
        '<input type="hidden" name="pageHistory" value="0">'
        '<input type="hidden" name="fbzx" value="%s">'
        '<input type="hidden" name="submissionTimestamp" value="-1">'
        "</form>%s"
        '<script type="text/javascript">var FB_PUBLIC_LOAD_DATA_ = %s;</script>'
        "</body></html>"
    ) % (_page_scripts(state, ms), BASE, ENTRY_GRADE, fbzx, fbzx, "<!-- padding -->" * 400,
         json.dumps(load_data, ensure_ascii=False))


def _simple_html(state: MockState, ms: float, text: str) -> str:
    return "<!DOCTYPE html><html><head><title>모의 설문</title>%s</head><body><div>%s</div></body></html>" % (
        _page_scripts(state, ms), text)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "GSE"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    @property
    def state(self) -> MockState:
        return self.server.state  # type: ignore[attr-defined]

    def _send(self, status: int, body: str, *, date_ms: float, content_type="text/html; charset=utf-8",
              extra=(), head_only=False, chunked=False):
        data = body.encode("utf-8")
        self.state.downlink()
        self.send_response_only(status)
        if status != 204:
            self.send_header("Content-Type", content_type)
        self.send_header("Date", _http_date(date_ms))
        if chunked and not head_only:
            self.send_header("Transfer-Encoding", "chunked")
        elif status != 204:
            self.send_header("Content-Length", str(len(data)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if not head_only and status != 204:
            if chunked:
                for i in range(0, len(data), 3000):
                    piece = data[i:i + 3000]
                    self.wfile.write(b"%x\r\n" % len(piece) + piece + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            else:
                self.wfile.write(data)
        self.wfile.flush()

    def _page(self, status, html_fn, *, head_only=False, extra=()):
        """Render a backend page: clock read first, Date header stamped ``date_lag_ms`` later."""
        ms = self.state.now_ms()
        body = html_fn(ms)
        if self.state.date_lag_ms > 0:
            time.sleep(self.state.date_lag_ms / 1000.0)
        self._send(status, body, date_ms=self.state.now_ms(), head_only=head_only, extra=extra, chunked=True)

    def _route(self, method: str):
        st = self.state
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        body = b""
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""      # blocks until the LAST byte arrives
        st.uplink()
        arrival_ms = st.now_ms()
        with st.lock:
            st.requests.append((method, self.path, arrival_ms))
        head_only = method == "HEAD"
        cookie = self.headers.get("Cookie") or ""

        if path == "/generate_204" and method in ("GET", "HEAD"):
            return self._send(204, "", date_ms=arrival_ms, head_only=head_only)

        if path == "/__mock/responses":
            with st.lock:
                payload = json.dumps({"responses": st.responses, "requests": st.requests,
                                      "violations": st.violations, "now_ms": arrival_ms}, ensure_ascii=False)
            return self._send(200, payload, date_ms=arrival_ms, content_type="application/json")

        # ---- everything below is handled by the "Forms backend" ----
        st.backend(cookie)
        if st.rate_limited:
            return self._send(429, "<html><body>unusual traffic</body></html>", date_ms=st.now_ms(),
                              head_only=head_only)

        if path == "/spreadsheets/d/%s/gviz/tq" % SHEET_ID and method == "GET":
            return self._send(200, self._gviz(urllib.parse.parse_qs(parsed.query)), date_ms=st.now_ms(),
                              content_type="application/javascript; charset=utf-8")

        if path == BASE + "viewform" and method in ("GET", "HEAD"):
            if not st.is_open():
                return self._send(302, "", date_ms=st.now_ms(), head_only=head_only,
                                  extra=(("Location", BASE + "closedform"),))
            cookies = []
            if "COMPASS=" not in cookie:
                with st.lock:
                    n = st.jar_counter
                    st.jar_counter += 1
                cookies = [("Set-Cookie", "S=spreadsheet_forms=s%d; Domain=.docs.google.com; Path=%s; Secure; HttpOnly"
                            % (n, BASE.rstrip("/"))),
                           ("Set-Cookie", "COMPASS=b%d; Domain=.docs.google.com; Path=%s; Secure; HttpOnly"
                            % (n, BASE.rstrip("/"))),
                           ("Set-Cookie", "NID=%s; path=/; domain=.google.com; Secure; HttpOnly" % ("n" * 180))]
            return self._page(200, lambda ms: _viewform_html(st, ms), head_only=head_only, extra=cookies)

        if path == BASE + "closedform" and method in ("GET", "HEAD"):
            if st.is_open():
                return self._send(302, "", date_ms=st.now_ms(), head_only=head_only,
                                  extra=(("Location", BASE + "viewform"),))
            return self._page(200, lambda ms: _simple_html(st, ms, CLOSED_MARKER), head_only=head_only)

        if path == BASE + "formResponse":
            if method != "POST":
                with st.lock:
                    st.violations.append((method, self.path, arrival_ms))
                return self._send(400, "<html><body>bad</body></html>", date_ms=st.now_ms(), head_only=head_only)
            if not st.is_open():
                return self._send(302, "", date_ms=st.now_ms(), extra=(("Location", BASE + "closedform"),))
            fields = urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
            grade = fields.get("entry.%d" % ENTRY_GRADE, [""])[0]
            name = fields.get("entry.%d" % ENTRY_NAME, [""])[0]
            if grade not in ("", "1", "2", "3") or (st.name_required and not name):
                return self._page(400, lambda ms: _viewform_html(st, ms))
            if st.proc_ms > 0:
                time.sleep(st.proc_ms / 1000.0)
            with st.lock:
                stamp_ms = st.now_ms()                      # the recorded "Timestamp"
                st.responses.append({
                    "stamp_ms": stamp_ms,
                    "arrival_ms": arrival_ms,
                    "fields": {k: v for k, v in fields.items()},
                    "cookie": cookie,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                })
            if st.drop_post_response:
                self.close_connection = True
                try:
                    self.connection.shutdown(2)
                except OSError:
                    pass
                return
            return self._page(200, lambda ms: _simple_html(
                st, ms, RECORDED_MARKER + ' <a href="%sviewform?usp=form_confirm">다른 응답 제출</a>' % BASE))

        return self._send(404, "<html><body>Not Found</body></html>", date_ms=st.now_ms(), head_only=head_only)

    # -- response sheet readback, shaped like the real gviz endpoint --------------------------------
    def _gviz(self, query) -> str:
        st = self.state
        tq = (query.get("tq") or ["select *"])[0]
        labels = {"A": "타임스탬프", "B": "학년", "C": "이름"}
        m = re.match(r"\s*select\s+(.*?)(?:\s+where\s+.*?)?(?:\s+order\s+by\s+.*?)?(?:\s+limit\s+(\d+))?\s*$",
                     tq, re.I | re.S)
        if not m:
            return self._gviz_wrap({"version": "0.6", "status": "error",
                                    "errors": [{"reason": "invalid_query", "message": "INVALID_QUERY"}]})
        items = [x.strip() for x in m.group(1).split(",")]
        if items == ["*"]:
            items = ["A", "B", "C"]
        limit = int(m.group(2)) if m.group(2) else 1000
        tz = _dt.timezone(_dt.timedelta(minutes=st.tz_offset_min))
        with st.lock:
            rows = sorted(st.responses, key=lambda r: -r["stamp_ms"])[:limit]
        cols, out_rows = [], []
        for it in items:
            f = re.match(r"(hour|minute|second|millisecond)\((\w)\)$", it, re.I)
            if f:
                cols.append({"id": "%s_%s" % (f.group(1).lower(), f.group(2)),
                             "label": "%s(%s)" % (f.group(1).lower(), labels.get(f.group(2), "")), "type": "number"})
            elif it in labels:
                cols.append({"id": it, "label": labels[it], "type": "datetime" if it == "A" else "string"})
            else:
                return self._gviz_wrap({"version": "0.6", "status": "error",
                                        "errors": [{"reason": "invalid_query", "message": "NO_COLUMN: " + it}]})
        for r in rows:
            ms = int(r["stamp_ms"])                      # the sheet keeps real milliseconds
            t = _dt.datetime.fromtimestamp(ms / 1000.0, tz)
            shown = _dt.datetime.fromtimestamp(round(ms / 1000.0), tz)   # default display ROUNDS to the second
            cells = []
            for it in items:
                f = re.match(r"(hour|minute|second|millisecond)\((\w)\)$", it, re.I)
                if f:
                    part = f.group(1).lower()
                    val = {"hour": t.hour, "minute": t.minute, "second": t.second, "millisecond": ms % 1000}[part]
                    cells.append({"v": float(val)})
                elif it == "A":
                    cells.append({"v": "Date(%d,%d,%d,%d,%d,%d)" % (t.year, t.month - 1, t.day, t.hour, t.minute,
                                                                     t.second),
                                  "f": shown.strftime("%Y. %m. %d %H:%M:%S")})
                elif it == "B":
                    cells.append({"v": r["fields"].get("entry.%d" % ENTRY_GRADE, [""])[0]})
                else:
                    name = r["fields"].get("entry.%d" % ENTRY_NAME, [""])[0]
                    cells.append({"v": name} if name else None)
            out_rows.append({"c": cells})
        return self._gviz_wrap({"version": "0.6", "reqId": "0", "status": "ok", "sig": "1",
                                "table": {"cols": cols, "rows": out_rows, "parsedNumHeaders": 1}})

    @staticmethod
    def _gviz_wrap(obj) -> str:
        return "/*O_o*/\ngoogle.visualization.Query.setResponse(%s);" % json.dumps(obj, ensure_ascii=False)

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("HEAD")

    def do_POST(self):
        self._route("POST")


class MockFormsServer:
    """Context manager: ``with MockFormsServer(skew_ms=1234, up_ms=5, down_ms=5) as m: m.form_url``"""

    def __init__(self, port=0, ssl_context=None, **state_kwargs):
        self.state = MockState(**state_kwargs)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.httpd.state = self.state  # type: ignore[attr-defined]
        self.scheme = "http"
        if ssl_context is not None:
            self.httpd.socket = ssl_context.wrap_socket(self.httpd.socket, server_side=True)
            self.scheme = "https"
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def origin(self) -> str:
        return "%s://127.0.0.1:%d" % (self.scheme, self.port)

    @property
    def form_url(self) -> str:
        return "%s%sviewform" % (self.origin, BASE)

    @property
    def sheet_url(self) -> str:
        return "%s/spreadsheets/d/%s/edit?gid=0#gid=0" % (self.origin, SHEET_ID)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--skew-ms", type=float, default=1234.0, help="server clock minus local clock")
    ap.add_argument("--up-ms", type=float, default=8.0)
    ap.add_argument("--down-ms", type=float, default=8.0)
    ap.add_argument("--jitter-ms", type=float, default=2.0)
    ap.add_argument("--backend-ms", type=float, default=40.0, help="extra delay before the Forms backend acts")
    ap.add_argument("--backend-jitter-ms", type=float, default=20.0)
    ap.add_argument("--proc-ms", type=float, default=15.0, help="POST handler start -> recorded timestamp")
    ap.add_argument("--open-in", type=float, default=None, help="form stays closed for this many seconds")
    args = ap.parse_args()
    srv = MockFormsServer(port=args.port, skew_ms=args.skew_ms, up_ms=args.up_ms, down_ms=args.down_ms,
                          jitter_ms=args.jitter_ms, backend_ms=args.backend_ms,
                          backend_jitter_ms=args.backend_jitter_ms, proc_ms=args.proc_ms, date_lag_ms=3.0)
    if args.open_in is not None:
        srv.state.open_at_server_ms = srv.state.now_ms() + args.open_in * 1000.0
    srv.start()
    print("mock form:   ", srv.form_url)
    print("mock sheet:  ", srv.sheet_url)
    print("recorded:    ", srv.origin + "/__mock/responses")
    print("server clock is %+.0f ms from this PC's clock; Ctrl+C to stop" % args.skew_ms)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    main()
