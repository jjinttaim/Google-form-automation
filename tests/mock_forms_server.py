"""A local stand-in for Google Forms, used only for testing the timing logic.

It imitates the parts of docs.google.com that the tool relies on:

* ``GET  /forms/d/e/<id>/viewform``      form page with FB_PUBLIC_LOAD_DATA_, hidden inputs and the
                                          millisecond server clock (``"server_time_ms":...``)
* ``POST /forms/d/e/<id>/formResponse``  records a response and stamps it with the *server* clock
* ``GET  /spreadsheets/d/<sid>/export?format=csv``  response sheet with millisecond timestamps
* ``GET  /__mock/responses``             JSON dump of everything recorded (for assertions)

The server clock deliberately differs from the local clock (``skew_ms``), and every request is
delayed like a real network path (``up_ms`` before the server acts, ``down_ms`` before the answer
leaves, plus random ``jitter_ms`` on each leg), so a client can only hit a target instant by
actually calibrating against the server.

Run standalone:  python tests/mock_forms_server.py --port 8765 --skew-ms 1234 --up-ms 15 --down-ms 15
"""
from __future__ import annotations

import argparse
import datetime as _dt
import email.utils
import json
import random
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FORM_ID = "1FAIpQLSmockmockmockmockmockmockmockmockmockmockmockmockmock"
SHEET_ID = "1MockSheetIdMockSheetIdMockSheetIdMockSheetId"
ENTRY_GRADE = 405927314
ENTRY_NAME = 536164697
RECORDED_MARKER = "응답이 기록되었습니다."
CLOSED_MARKER = "설문지에서 더 이상 응답을 받지 않습니다."


class MockState:
    def __init__(self, *, skew_ms=0.0, up_ms=0.0, down_ms=0.0, jitter_ms=0.0, proc_ms=0.0,
                 open_at_server_ms=None, tz_offset_min=540, seed=1, expose_server_time=True):
        self.skew_ms = float(skew_ms)
        self.up_ms = float(up_ms)
        self.down_ms = float(down_ms)
        self.jitter_ms = float(jitter_ms)
        self.proc_ms = float(proc_ms)            # backend work between arrival and the timestamp
        self.open_at_server_ms = open_at_server_ms   # None = always open
        self.tz_offset_min = tz_offset_min       # time zone of the "response sheet" (KST = +540)
        self.expose_server_time = expose_server_time  # False: page has no server_time_ms (Date-only)
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.responses = []                      # dicts, in commit order
        self.requests = []                       # (method, path, arrival_server_ms)
        self.fbzx_counter = 1000

    # -- clock -------------------------------------------------------------------------------
    def now_ms(self) -> float:
        return time.time() * 1000.0 + self.skew_ms

    def is_open(self) -> bool:
        return self.open_at_server_ms is None or self.now_ms() >= self.open_at_server_ms

    # -- network emulation ---------------------------------------------------------------------
    def _leg(self, base_ms: float) -> None:
        with self.lock:
            j = self.rng.random() * self.jitter_ms
        delay = (base_ms + j) / 1000.0
        if delay > 0:
            time.sleep(delay)

    def uplink(self):
        self._leg(self.up_ms)

    def downlink(self):
        self._leg(self.down_ms)


def _http_date(ms: float) -> str:
    # HTTP Date has one-second resolution and is the *floor* of the clock (as measured on Google).
    return email.utils.formatdate(int(ms // 1000), usegmt=True)


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
    load_data = [None, [None, [
        [474880873, "학년", None, 2, [[ENTRY_GRADE, [["1", None, None, None, 0], ["2", None, None, None, 0],
                                                    ["3", None, None, None, 0]], 1, None, None, None, None, None, 0]],
         None, None, None, None, None, None, [None, "학년"]],
        [925209746, "이름", None, 0, [[ENTRY_NAME, None, 1]], None, None, None, None, None, None, [None, "이름"]],
    ], None, None, None, None, None, [None, "mock"], "모의 설문", 76, [None, None, None, 2, None, None, 1]],
        "/forms", "모의 설문", None, None, None, "", None, 0, 0, None, "", 0, "e/" + FORM_ID, 0, "[]", 0, 0, 1]
    return (
        "<!DOCTYPE html><html><head><title>모의 설문</title>%s</head><body>"
        '<form action="/forms/d/e/%s/formResponse" method="POST" id="mG61Hd">'
        '<input type="hidden" name="entry.%d_sentinel">'
        '<input type="hidden" name="fvv" value="1">'
        '<input type="hidden" name="partialResponse" value="[null,null,&quot;%s&quot;]">'
        '<input type="hidden" name="pageHistory" value="0">'
        '<input type="hidden" name="fbzx" value="%s">'
        '<input type="hidden" name="submissionTimestamp" value="-1">'
        "</form>"
        '<script type="text/javascript">var FB_PUBLIC_LOAD_DATA_ = %s;</script>'
        "</body></html>"
    ) % (_page_scripts(state, ms), FORM_ID, ENTRY_GRADE, fbzx, fbzx, json.dumps(load_data, ensure_ascii=False))


def _simple_html(state: MockState, ms: float, title: str, text: str) -> str:
    return "<!DOCTYPE html><html><head><title>%s</title>%s</head><body><div>%s</div></body></html>" % (
        title, _page_scripts(state, ms), text)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "GSE"
    sys_version = ""

    def log_message(self, fmt, *args):  # silence
        pass

    @property
    def state(self) -> MockState:
        return self.server.state  # type: ignore[attr-defined]

    def _send(self, status: int, body: str, *, stamp_ms: float, content_type="text/html; charset=utf-8",
              extra=None, head_only=False):
        data = body.encode("utf-8")
        self.state.downlink()
        self.send_response_only(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Date", _http_date(stamp_ms))
        self.send_header("Server", "GSE")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head_only:
            self.wfile.write(data)
        self.wfile.flush()

    def _route(self, method: str):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        body = b""
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""   # blocks until the LAST byte arrives
        self.state.uplink()
        arrival_ms = self.state.now_ms()
        with self.state.lock:
            self.state.requests.append((method, self.path, arrival_ms))
        head_only = method == "HEAD"
        base = "/forms/d/e/%s/" % FORM_ID

        if path == "/__mock/responses":
            with self.state.lock:
                payload = json.dumps({"responses": self.state.responses, "requests": self.state.requests,
                                      "now_ms": arrival_ms}, ensure_ascii=False)
            return self._send(200, payload, stamp_ms=arrival_ms, content_type="application/json")

        if path == "/spreadsheets/d/%s/export" % SHEET_ID:
            return self._send(200, self._csv(), stamp_ms=arrival_ms, content_type="text/csv; charset=utf-8")

        if path == base + "viewform" and method in ("GET", "HEAD"):
            if not self.state.is_open():
                return self._send(302, "", stamp_ms=arrival_ms, extra={"Location": base + "closedform"},
                                  head_only=head_only)
            return self._send(200, _viewform_html(self.state, arrival_ms), stamp_ms=arrival_ms, head_only=head_only)

        if path == base + "closedform" and method in ("GET", "HEAD"):
            return self._send(200, _simple_html(self.state, arrival_ms, "모의 설문", CLOSED_MARKER),
                              stamp_ms=arrival_ms, head_only=head_only)

        if path == base + "formResponse" and method == "POST":
            if not self.state.is_open():
                return self._send(200, _simple_html(self.state, arrival_ms, "모의 설문", CLOSED_MARKER),
                                  stamp_ms=arrival_ms)
            fields = urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
            grade = fields.get("entry.%d" % ENTRY_GRADE, [""])[0]
            name = fields.get("entry.%d" % ENTRY_NAME, [""])[0]
            if grade not in ("1", "2", "3") or not name:
                return self._send(400, _viewform_html(self.state, arrival_ms), stamp_ms=arrival_ms)
            if self.state.proc_ms > 0:
                time.sleep(self.state.proc_ms / 1000.0)
            with self.state.lock:
                stamp_ms = self.state.now_ms()          # the recorded "Timestamp"
                self.state.responses.append({
                    "stamp_ms": stamp_ms,
                    "arrival_ms": arrival_ms,
                    "fields": {k: v for k, v in fields.items()},
                })
            return self._send(200, _simple_html(self.state, self.state.now_ms(), "모의 설문", RECORDED_MARKER),
                              stamp_ms=stamp_ms)

        return self._send(404, "<html><body>Not Found</body></html>", stamp_ms=arrival_ms, head_only=head_only)

    def _csv(self) -> str:
        tz = _dt.timezone(_dt.timedelta(minutes=self.state.tz_offset_min))
        lines = ["타임스탬프,학년,이름"]
        with self.state.lock:
            rows = list(self.state.responses)
        for r in rows:
            ms = int(r["stamp_ms"])                    # the sheet keeps milliseconds
            t = _dt.datetime.fromtimestamp(ms / 1000.0, tz)
            ts = t.strftime("%Y-%m-%d %H:%M:%S") + ".%03d" % (ms % 1000)
            grade = r["fields"].get("entry.%d" % ENTRY_GRADE, [""])[0]
            name = r["fields"].get("entry.%d" % ENTRY_NAME, [""])[0].replace('"', '""')
            lines.append('%s,%s,"%s"' % (ts, grade, name))
        return "\r\n".join(lines) + "\r\n"

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("HEAD")

    def do_POST(self):
        self._route("POST")


class MockFormsServer:
    """Context manager: ``with MockFormsServer(skew_ms=1234, up_ms=10, down_ms=10) as m: m.form_url``"""

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
        return "%s/forms/d/e/%s/viewform" % (self.origin, FORM_ID)

    @property
    def csv_url(self) -> str:
        return "%s/spreadsheets/d/%s/export?format=csv" % (self.origin, SHEET_ID)

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
    ap.add_argument("--up-ms", type=float, default=15.0)
    ap.add_argument("--down-ms", type=float, default=15.0)
    ap.add_argument("--jitter-ms", type=float, default=3.0)
    ap.add_argument("--proc-ms", type=float, default=20.0, help="backend time between arrival and the timestamp")
    ap.add_argument("--open-in", type=float, default=None, help="form stays closed for this many seconds")
    args = ap.parse_args()
    srv = MockFormsServer(port=args.port, skew_ms=args.skew_ms, up_ms=args.up_ms, down_ms=args.down_ms,
                          jitter_ms=args.jitter_ms, proc_ms=args.proc_ms)
    if args.open_in is not None:
        srv.state.open_at_server_ms = srv.state.now_ms() + args.open_in * 1000.0
    srv.start()
    print("mock form:   ", srv.form_url)
    print("mock sheet:  ", srv.csv_url)
    print("recorded:    ", srv.origin + "/__mock/responses")
    print("server clock is %+.0f ms from this PC's clock; Ctrl+C to stop" % args.skew_ms)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    main()
