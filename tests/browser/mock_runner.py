"""Starts tests/mock_forms_server.py for the browser tests and keeps it under remote control.

usage: python3 mock_runner.py '<json kwargs for MockFormsServer>'

* The kwargs go to ``MockFormsServer(port=0, **kwargs)`` (``open_in_ms`` is extra: the form stays closed
  for that long, counted on the mock's server clock).
* First stdout line: ``{"port": ..., "origin": ..., "form_url": ..., "form_id": ...}`` as JSON.
* Every further stdin line is one JSON command, answered by one JSON line on stdout:
    {"id": 7, "set": {"rate_limited": true}}      set attributes of the MockState
    {"id": 8, "add": {"skew_ms": -700}}           add to numeric attributes (e.g. step the server clock)
    {"id": 9, "close": true}                      the form stops accepting responses now
* The process ends when stdin is closed, i.e. when the test runner exits.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from mock_forms_server import FORM_ID, MockFormsServer  # noqa: E402

SETTABLE = {"skew_ms", "up_ms", "down_ms", "jitter_ms", "backend_ms", "backend_jitter_ms", "proc_ms",
            "date_lag_ms", "open_at_server_ms", "expose_server_time", "name_required", "rate_limited"}


def main():
    kw = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    open_in_ms = kw.pop("open_in_ms", None)
    if "backend_modes" in kw:
        kw["backend_modes"] = tuple(kw["backend_modes"])
    srv = MockFormsServer(port=0, **kw)
    st = srv.state
    if open_in_ms is not None:
        st.open_at_server_ms = st.now_ms() + float(open_in_ms)
    srv.start()
    print(json.dumps({"port": srv.port, "origin": srv.origin, "form_url": srv.form_url, "form_id": FORM_ID}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        reply = {"ok": True}
        try:
            cmd = json.loads(line)
            reply["id"] = cmd.get("id")
            with st.lock:
                for k, v in (cmd.get("set") or {}).items():
                    if k not in SETTABLE:
                        raise KeyError(k)
                    setattr(st, k, v)
                for k, v in (cmd.get("add") or {}).items():
                    if k not in SETTABLE:
                        raise KeyError(k)
                    setattr(st, k, getattr(st, k) + v)
                if cmd.get("close"):
                    st.open_at_server_ms = st.now_ms() + 1e12
            reply["now_ms"] = st.now_ms()
        except Exception as exc:  # report, keep serving
            reply = {"ok": False, "error": repr(exc)}
        print(json.dumps(reply), flush=True)
    srv.stop()


if __name__ == "__main__":
    main()
