#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gform_ontime.py - 구글 폼 응답 1건을 '구글 서버 시계 기준 정각'에 맞춰 제출합니다.

표준 라이브러리만 사용합니다 (Python 3.8 이상). 사용법은 README.md 를 보세요.

    python gform_ontime.py inspect   <폼 주소>
    python gform_ontime.py clock     <폼 주소>
    python gform_ontime.py rehearse  <폼 주소>                      (읽기 전용 리허설, 제출 안 함)
    python gform_ontime.py fire      <폼 주소> --at "2026-11-23 22:00:00" --answer "학년=2" --answer "이름=홍길동"
    python gform_ontime.py calibrate <내 연습용 폼 주소> --sheet <응답 시트 주소>
    python gform_ontime.py selftest

How it works (short version - the long one is in README.md):

* Local time base is a monotonic high-resolution counter, never the PC wall clock.
      server_ms = local_ms + theta          (theta unknown, bracketed by measurements)
* Google tells us its clock in two ways, each giving HARD bounds on theta:
    - ``HEAD /generate_204`` -> ``Date`` header (whole seconds, floor). Requests are aimed so that
      they straddle a second flip (bisection); the bracket shrinks to about one round trip.
    - every Forms page embeds ``"server_time_ms":<ms>`` - the Forms backend clock in milliseconds.
          theta >= S - t_received            theta <  S + 1 - t_sent
* ``theta_lo`` (the largest lower bound) is PROVEN: a request released at local time t cannot leave
  this PC before server time t + theta_lo. Mode "safe" releases the whole POST, in one write, at
      t = T - theta_lo + guard
  so the response cannot be stamped before the target T. It lands one network trip plus Google's
  own processing time after T (typically well inside the same second).
* Exactly one submission. Nothing but that one POST ever touches /formResponse.
"""
from __future__ import annotations

import argparse
import atexit
import base64
import datetime as _dt
import email.utils
import gc
import hashlib
import html as _html
import json
import math
import os
import random
import re
import socket
import ssl
import statistics
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

VERSION = "1.0"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0.0.0 Safari/537.36")
FE_PROBE_PATH = "/generate_204"
RE_SERVER_TIME = re.compile(rb'"server_time_ms":(\d{12,14})[,}]')
CONFIRM_TEXT = ("응답이 기록되었습니다", "Your response has been recorded")
CLOSED_TEXT = ("더 이상 응답을 받지 않습니다", "is no longer accepting responses")
CALIBRATION_FILE = "gform_ontime_calibration.json"
SPIN_MS = 50.0 if os.name == "nt" else 30.0        # busy-wait window before the release
AGE_MS_PER_S = 0.5                                 # 500 ppm: how fast an old clock sample loses trust
WEEKDAY = "월화수목금토일"

# ------------------------------------------------------------------------------------------------
# local clock: monotonic, high resolution, not slewed by NTP where the OS offers that
# ------------------------------------------------------------------------------------------------
if sys.platform.startswith("linux") and hasattr(time, "CLOCK_MONOTONIC_RAW"):
    def now_ns() -> int:
        return time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
    CLOCK_NAME = "CLOCK_MONOTONIC_RAW"
else:
    now_ns = time.perf_counter_ns
    CLOCK_NAME = "perf_counter"


def ms(ns: int) -> float:
    return ns / 1e6


def wait_until(t_ns: int, spin_ms: float = SPIN_MS) -> None:
    """Sleep until shortly before t_ns, then busy-wait. Never returns early."""
    spin_ns = int(spin_ms * 1e6)
    while True:
        rem = t_ns - now_ns()
        if rem <= spin_ns + 500_000:
            break
        time.sleep((rem - spin_ns) / 1e9 * 0.8)
    while now_ns() < t_ns:
        pass


def sleep_until(t_ns: int) -> None:
    """Coarse wait without spinning (may overshoot by a scheduler tick)."""
    while True:
        rem = t_ns - now_ns()
        if rem <= 0:
            return
        time.sleep(min(rem / 1e9, 0.25))


# ------------------------------------------------------------------------------------------------
# console / operating-system helpers
# ------------------------------------------------------------------------------------------------
_quiet = False


def say(*args, end: str = "\n") -> None:
    if _quiet:
        return
    print(*args, end=end)
    sys.stdout.flush()


def warn(msg: str) -> None:
    say("[주의] " + msg)


_status_len = 0


def status(line: str) -> None:
    """One self-overwriting status line."""
    global _status_len
    if _quiet:
        return
    pad = max(0, _status_len - len(line))
    sys.stdout.write("\r" + line + " " * pad)
    sys.stdout.flush()
    _status_len = len(line)


def status_end() -> None:
    global _status_len
    if _status_len and not _quiet:
        sys.stdout.write("\n")
        sys.stdout.flush()
    _status_len = 0


def setup_process() -> None:
    """Best effort: readable console, no QuickEdit freeze, higher priority, no sleep."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            handle = k32.GetStdHandle(-10)
            mode = ctypes.c_uint32()
            if k32.GetConsoleMode(handle, ctypes.byref(mode)):
                saved = mode.value
                # a stray click in a QuickEdit console freezes the program on its next print
                k32.SetConsoleMode(handle, (saved | 0x0080) & ~0x0040)
                atexit.register(lambda: k32.SetConsoleMode(handle, saved))
            k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00000080)      # HIGH_PRIORITY_CLASS
            k32.SetThreadExecutionState(0x80000001)                        # keep the system awake
            atexit.register(lambda: k32.SetThreadExecutionState(0x80000000))
        except Exception:
            pass
    else:
        try:
            os.nice(-10)
        except Exception:
            pass
        if sys.platform == "darwin":
            try:
                import subprocess
                subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass


class Abort(Exception):
    """Stop with a message; nothing was submitted unless the message says otherwise."""

    def __init__(self, msg: str, code: int = 2):
        super().__init__(msg)
        self.code = code


# ------------------------------------------------------------------------------------------------
# URL handling
# ------------------------------------------------------------------------------------------------
@dataclass
class Target:
    scheme: str
    host: str
    port: int
    base: str                      # "/forms/d/e/<id>"

    @property
    def origin(self) -> str:
        default = 443 if self.scheme == "https" else 80
        return "%s://%s%s" % (self.scheme, self.host, "" if self.port == default else ":%d" % self.port)

    @property
    def view_path(self) -> str:
        return self.base + "/viewform"

    @property
    def post_path(self) -> str:
        return self.base + "/formResponse"

    @property
    def view_url(self) -> str:
        return self.origin + self.view_path


_held_locks: list = []


def single_instance(target: "Target") -> None:
    """Refuse to run a second `fire` for the same form on this computer (started twice by mistake, or by two
    schedulers). The operating system releases the lock when the process ends, however it ends."""
    key = hashlib.sha1((target.host + target.base).encode("utf-8")).hexdigest()[:16]
    path = os.path.join(tempfile.gettempdir(), "gform_ontime_%s.lock" % key)
    try:
        f = open(path, "a+")
    except OSError:
        return                                   # no writable temp dir: do not block the user over it
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise Abort("이 PC에서 같은 폼에 대한 제출 프로그램이 이미 실행 중입니다. 중복 제출을 막기 위해 "
                    "이 실행은 아무것도 보내지 않고 종료합니다.", 2)
    _held_locks.append(f)


def parse_target(url: str) -> Target:
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise Abort("폼 주소를 이해하지 못했습니다: %s" % url)
    if u.hostname == "forms.gle":
        raise Abort("forms.gle 단축 주소는 브라우저로 한 번 연 뒤, 주소창의 docs.google.com/forms/... 주소를 넣어 주세요.")
    m = re.match(r"^(/forms/(?:u/\d+/)?d/(?:e/)?[A-Za-z0-9_-]+)", u.path)
    if not m:
        raise Abort("구글 폼 주소가 아닙니다 (…/forms/d/e/<ID>/viewform 형태여야 합니다): %s" % url)
    base = re.sub(r"^/forms/u/\d+/", "/forms/", m.group(1))
    return Target(u.scheme, u.hostname, u.port or (443 if u.scheme == "https" else 80), base)


# ------------------------------------------------------------------------------------------------
# raw HTTP/1.1 connection with timing marks
# ------------------------------------------------------------------------------------------------
class NetError(Exception):
    def __init__(self, msg: str, eof: bool = False):
        super().__init__(msg)
        self.eof = eof


@dataclass
class Resp:
    status: int
    headers: Dict[str, str]
    set_cookies: List[str]
    body: bytes
    t_send_ns: int
    t_first_ns: int                 # first byte of the response arrived
    t_done_ns: int
    t_token_ns: Optional[int]       # the bytes containing "server_time_ms" had arrived
    server_time_ms: Optional[int]
    date_s: Optional[int]
    keepalive: bool

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    @property
    def location(self) -> str:
        return self.headers.get("location", "")


def _proxy_for(scheme: str, host: str) -> Optional[urllib.parse.SplitResult]:
    if scheme != "https" or host in ("127.0.0.1", "localhost", "::1"):
        return None
    raw = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    return urllib.parse.urlsplit(raw)


class RawConn:
    """One keep-alive HTTP/1.1 connection. Requests are strictly sequential."""

    def __init__(self, scheme: str, host: str, port: int, *, addr=None, cafile: Optional[str] = None,
                 timeout: float = 10.0):
        self.scheme, self.host, self.port = scheme, host, port
        self.addr = addr                     # (family, sockaddr) to pin, or None
        self.cafile = cafile
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self.via_proxy = False
        self.requests = 0
        self._left = b""

    # -- connect ---------------------------------------------------------------------------------
    def connect(self) -> "RawConn":
        proxy = _proxy_for(self.scheme, self.host)
        dest_host, dest_port = (proxy.hostname, proxy.port or 8080) if proxy else (self.host, self.port)
        sock = None
        last: Optional[Exception] = None
        candidates = [self.addr] if self.addr else []
        if not candidates:
            try:
                infos = socket.getaddrinfo(dest_host, dest_port, type=socket.SOCK_STREAM)
            except OSError as e:
                raise NetError("주소를 찾지 못했습니다 (%s): %s" % (dest_host, e))
            candidates = [(fam, sa) for fam, _t, _p, _c, sa in infos]
        for fam, sa in candidates:
            try:
                s = socket.socket(fam, socket.SOCK_STREAM)
                s.settimeout(min(self.timeout, 5.0))
                s.connect(sa)
                sock, self.addr = s, (fam, sa)
                break
            except OSError as e:
                last = e
                try:
                    s.close()
                except Exception:
                    pass
        if sock is None:
            raise NetError("연결하지 못했습니다 (%s): %s" % (dest_host, last))
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(self.timeout)
            if proxy:
                self.via_proxy = True
                req = "CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n" % (self.host, self.port, self.host, self.port)
                if proxy.username:
                    cred = "%s:%s" % (urllib.parse.unquote(proxy.username),
                                      urllib.parse.unquote(proxy.password or ""))
                    req += "Proxy-Authorization: Basic %s\r\n" % base64.b64encode(cred.encode()).decode()
                sock.sendall((req + "\r\n").encode("ascii"))
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = sock.recv(4096)
                    if not chunk:
                        raise NetError("프록시가 연결을 닫았습니다")
                    buf += chunk
                code = buf.split(b" ", 2)[1] if b" " in buf else b"?"
                if code != b"200":
                    raise NetError("프록시가 %s 접속을 거부했습니다 (HTTP %s)" % (self.host, code.decode("ascii", "replace")))
            if self.scheme == "https":
                ctx = ssl.create_default_context(cafile=self.cafile or os.environ.get("SSL_CERT_FILE") or None)
                try:
                    ctx.set_alpn_protocols(["http/1.1"])
                except Exception:
                    pass
                sock = ctx.wrap_socket(sock, server_hostname=self.host)
        except (OSError, ssl.SSLError) as e:
            try:
                sock.close()
            except Exception:
                pass
            raise NetError("연결 설정 실패: %s" % e)
        self.sock = sock
        return self

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def alive(self) -> bool:
        """True if the peer has not closed the connection. Sends nothing.
        (A non-blocking recv, not select(): right after a TLS 1.3 handshake the raw socket is readable
        although there is no application data.)"""
        s = self.sock
        if s is None:
            return False
        try:
            try:
                s.setblocking(False)
                s.recv(1)
            except (ssl.SSLWantReadError, BlockingIOError, InterruptedError):
                return True
            except (ssl.SSLError, OSError, ValueError):
                return False
            return False                 # EOF or unexpected data: do not trust this connection
        finally:
            try:
                s.settimeout(self.timeout)
            except Exception:
                pass

    # -- request / response ----------------------------------------------------------------------
    def build(self, method: str, path: str, headers=(), body: bytes = b"") -> bytes:
        default = 443 if self.scheme == "https" else 80
        host = self.host if self.port == default else "%s:%d" % (self.host, self.port)
        lines = ["%s %s HTTP/1.1" % (method, path), "Host: " + host]
        lines += ["%s: %s" % kv for kv in headers]
        if body or method == "POST":
            lines.append("Content-Length: %d" % len(body))
        return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body

    def send(self, data: bytes) -> Tuple[int, int]:
        """One write of the whole request. Returns (time before, time after)."""
        if self.sock is None:
            raise NetError("연결이 없습니다")
        t0 = now_ns()
        try:
            self.sock.sendall(data)
        except (OSError, ssl.SSLError) as e:
            raise NetError("전송 실패: %s" % e)
        t1 = now_ns()
        self.requests += 1
        return t0, t1

    def _recv(self) -> Tuple[bytes, int]:
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            raise NetError("응답 대기 시간 초과")
        except (OSError, ssl.SSLError) as e:
            raise NetError("수신 실패: %s" % e)
        t = now_ns()
        if not data:
            raise NetError("서버가 연결을 닫았습니다", eof=True)
        return data, t

    def read_response(self, method: str, t_send_ns: int, timeout: float = 20.0) -> Resp:
        self.sock.settimeout(timeout)
        buf = bytearray(self._left)
        self._left = b""
        t_first: Optional[int] = None
        while True:                                        # status line + headers (skipping 1xx)
            while True:
                i = buf.find(b"\r\n\r\n")
                if i >= 0:
                    break
                chunk, t = self._recv()
                if t_first is None:
                    t_first = t
                buf += chunk
                if len(buf) > 512 * 1024:
                    raise NetError("응답 헤더가 비정상적으로 큽니다")
            head = bytes(buf[:i]).decode("iso-8859-1")
            del buf[:i + 4]
            lines = head.split("\r\n")
            parts = lines[0].split(" ", 2)
            try:
                status_code = int(parts[1])
            except (IndexError, ValueError):
                raise NetError("HTTP 응답이 아닙니다: %r" % lines[0][:60])
            if status_code >= 200:
                break
        if t_first is None:
            t_first = now_ns()
        headers: Dict[str, str] = {}
        cookies: List[str] = []
        for ln in lines[1:]:
            k, _sep, v = ln.partition(":")
            k, v = k.strip().lower(), v.strip()
            if k == "set-cookie":
                cookies.append(v)
            else:
                headers[k] = v

        body = bytearray()
        found: Dict[str, Optional[int]] = {"S": None, "t": None, "scan": 0}

        def feed(data, t: int) -> None:
            body.extend(data)
            if found["S"] is None:
                m = RE_SERVER_TIME.search(body, max(0, found["scan"] - 40))
                if m:
                    found["S"], found["t"] = int(m.group(1)), t
                found["scan"] = len(body)

        keepalive = parts[0].upper() == "HTTP/1.1" and headers.get("connection", "").lower() != "close"
        t_last = t_first
        if method == "HEAD" or status_code in (204, 304):
            pass
        elif "chunked" in headers.get("transfer-encoding", "").lower():
            done = False
            while not done:
                while True:
                    j = buf.find(b"\r\n")
                    if j < 0:
                        break
                    try:
                        size = int(bytes(buf[:j]).split(b";")[0].strip() or b"0", 16)
                    except ValueError:
                        raise NetError("청크 인코딩이 손상되었습니다")
                    if size == 0:
                        k = buf.find(b"\r\n\r\n", j)
                        if k < 0:
                            break
                        del buf[:k + 4]
                        done = True
                        break
                    if len(buf) < j + 2 + size + 2:
                        break
                    feed(buf[j + 2:j + 2 + size], t_last)
                    del buf[:j + 2 + size + 2]
                if not done:
                    chunk, t_last = self._recv()
                    buf += chunk
        elif "content-length" in headers:
            need = int(headers["content-length"])
            feed(buf[:need], t_last)
            del buf[:need]
            while len(body) < need:
                chunk, t_last = self._recv()
                feed(chunk[:need - len(body)], t_last)
        else:                                              # body runs until the connection closes
            keepalive = False
            feed(buf, t_last)
            del buf[:]
            while True:
                try:
                    chunk, t_last = self._recv()
                except NetError as e:
                    if e.eof:
                        break
                    raise
                feed(chunk, t_last)
        self._left = bytes(buf)
        date_s = None
        if "date" in headers:
            try:
                date_s = int(email.utils.parsedate_to_datetime(headers["date"]).timestamp())
            except Exception:
                date_s = None
        self.sock.settimeout(self.timeout)
        return Resp(status_code, headers, cookies, bytes(body), t_send_ns, t_first, now_ns(), found["t"],
                    found["S"], date_s, keepalive)

    def request(self, method: str, path: str, headers=(), body: bytes = b"", timeout: float = 20.0) -> Resp:
        t0, _t1 = self.send(self.build(method, path, headers, body))
        return self.read_response(method, t0, timeout)


class CookieJar:
    """Minimal jar: Google pins a client to a Forms backend with the COMPASS cookie."""
    SKIP = {"NID"}                     # large, not needed, keeps the POST in one packet

    def __init__(self):
        self.cookies: Dict[str, Tuple[str, str]] = {}

    def update(self, set_cookie_headers: List[str]) -> None:
        for h in set_cookie_headers:
            first, *attrs = h.split(";")
            name, _sep, value = first.strip().partition("=")
            path = "/"
            for a in attrs:
                k, _s, v = a.strip().partition("=")
                if k.lower() == "path" and v:
                    path = v
            if name:
                self.cookies[name] = (value, path)

    def header(self, path: str) -> str:
        return "; ".join("%s=%s" % (n, v) for n, (v, p) in self.cookies.items()
                         if n not in self.SKIP and path.startswith(p))


# ------------------------------------------------------------------------------------------------
# server clock
# ------------------------------------------------------------------------------------------------
class ServerClock:
    """Brackets theta = server_ms - local_ms. Every sample is a pair of hard bounds."""
    WINDOW_S = 300.0

    def __init__(self):
        self.samples: List[Tuple[float, float, int, str]] = []     # (lo, hi, t_ns, source)
        self.resets = 0
        self.cross_conflicts = 0
        self.trust_frontend = True

    def add(self, lo: float, hi: float, t_ns: int, src: str) -> bool:
        """Returns False if the sample contradicted what we had (then older samples were dropped).

        A contradiction means the local counter jumped (PC slept) or one sample is anomalous. Keeping only
        the newest sample is self-healing: the bracket is rebuilt from the following samples, and a single
        fresh sample has a low (late-biased, i.e. safe) lower bound."""
        if src == "fe" and not self.trust_frontend:
            return True
        cutoff = t_ns - int(self.WINDOW_S * 1e9)
        self.samples = [s for s in self.samples if s[2] >= cutoff]
        self.samples.append((lo, hi, t_ns, src))
        if self._consistent(self.samples, t_ns):
            return True
        self.resets += 1
        same = [s for s in self.samples if s[3] == src]
        if len(same) < len(self.samples) and self._consistent(same, t_ns):
            # the two clock sources (Google front end vs Forms backend) disagree with each other
            self.cross_conflicts += 1
            if self.cross_conflicts >= 2 and self.trust_frontend:
                self.trust_frontend = False          # the Forms backend stamps the response: trust it
                self.samples = [s for s in self.samples if s[3] != "fe"]
                if self.samples and self._consistent(self.samples, t_ns):
                    return False
        self.samples = [s for s in self.samples[-1:] if s[3] != "fe" or self.trust_frontend]
        return False

    @classmethod
    def _consistent(cls, samples, at_ns: int) -> bool:
        lo, hi = cls._bounds(samples, at_ns)
        return lo <= hi

    @staticmethod
    def _bounds(samples, at_ns: int) -> Tuple[float, float]:
        lo = max(l - max(0, at_ns - t) / 1e9 * AGE_MS_PER_S for l, _h, t, _s in samples)
        hi = min(h + max(0, at_ns - t) / 1e9 * AGE_MS_PER_S for _l, h, t, _s in samples)
        return lo, hi

    def bounds(self, at_ns: Optional[int] = None) -> Optional[Tuple[float, float]]:
        if not self.samples:
            return None
        return self._bounds(self.samples, now_ns() if at_ns is None else at_ns)

    def server_now(self) -> Optional[Tuple[float, float]]:
        """(estimate, half-width) of the server clock right now, in epoch ms."""
        t = now_ns()
        b = self.bounds(t)
        if not b:
            return None
        return ms(t) + (b[0] + b[1]) / 2, (b[1] - b[0]) / 2


def fmt_clock(server_ms: float, digits: int = 3) -> str:
    """Server epoch ms -> HH:MM:SS.mmm in this PC's time zone."""
    whole = math.floor(server_ms)
    d = _dt.datetime.fromtimestamp(whole // 1000, _dt.timezone.utc).astimezone()
    out = d.strftime("%H:%M:%S")
    if digits:
        out += ".%03d" % (whole % 1000)
    return out


def fmt_rel(delta_ms: float) -> str:
    return "%+.1f ms" % delta_ms


# ------------------------------------------------------------------------------------------------
# form definition, answers, payload
# ------------------------------------------------------------------------------------------------
TYPE_NAMES = {0: "단답형", 1: "장문형", 2: "객관식", 3: "드롭다운", 4: "체크박스", 5: "선형 배율",
              7: "그리드", 9: "날짜", 10: "시간", 13: "파일 업로드"}
NON_QUESTIONS = (6, 8, 11, 12)          # title, page break, image, video
CHOICE_TYPES = (2, 3, 4, 5)


@dataclass
class Entry:
    id: int
    required: bool
    options: Optional[List[str]]
    has_other: bool


@dataclass
class Question:
    title: str
    type: int
    entries: List[Entry]

    @property
    def entry(self) -> Entry:
        return self.entries[0]


@dataclass
class Form:
    title: str
    questions: List[Question]
    hidden: List[Tuple[str, str]]
    action_path: str
    pages: int
    signin_required: bool
    collect_email: int                  # 1/None no, 2 verified (sign-in), 3 typed by responder
    captcha: bool
    sha1: str


def _get(obj, *idx, default=None):
    for i in idx:
        if not isinstance(obj, list) or i >= len(obj) or obj[i] is None:
            return default
        obj = obj[i]
    return obj


def parse_form(page: str) -> Form:
    m = re.search(r"FB_PUBLIC_LOAD_DATA_\s*=\s*(\[.+?\])\s*;\s*</script>", page, re.S)
    if not m:
        raise ValueError("폼 정의(FB_PUBLIC_LOAD_DATA_)를 찾지 못했습니다")
    raw = m.group(1)
    data = json.loads(raw)
    form = data[1]
    questions: List[Question] = []
    pages = 1
    for item in form[1] or []:
        qtype = _get(item, 3)
        if qtype == 8:
            pages += 1
        if qtype in NON_QUESTIONS or qtype is None:
            continue
        entries = []
        for sub in _get(item, 4, default=[]) or []:
            opts = _get(sub, 1)
            options = [str(o[0]) for o in opts if o and o[0] is not None] if opts else None
            has_other = bool(opts) and any(_get(o, 4) for o in opts)
            entries.append(Entry(int(sub[0]), bool(_get(sub, 2, default=0)), options, has_other))
        if entries:
            questions.append(Question(str(_get(item, 1, default="") or "").strip(), int(qtype), entries))
    hidden: List[Tuple[str, str]] = []
    for tag in re.findall(r"<input[^>]*type=\"hidden\"[^>]*>", page):
        name = re.search(r'name="([^"]*)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        if name:
            hidden.append((_html.unescape(name.group(1)), _html.unescape(value.group(1)) if value else ""))
    action = re.search(r'<form[^>]*action="([^"]+)"', page)
    action_path = urllib.parse.urlsplit(_html.unescape(action.group(1))).path if action else ""
    cap = re.search(r'data-should-execute-invisible-captcha-on-form-submit="([^"]*)"', page)
    return Form(
        title=str(_get(form, 8) or _get(data, 3) or ""),
        questions=questions,
        hidden=hidden,
        action_path=action_path,
        pages=pages,
        signin_required=bool(_get(data, 18, default=0)),
        collect_email=int(_get(form, 10, 6, default=1) or 1),
        captcha=bool(cap and cap.group(1) == "true"),
        sha1=hashlib.sha1(raw.encode("utf-8")).hexdigest(),
    )


def find_question(form: Form, key: str) -> Question:
    key = key.strip()
    m = re.match(r"^(?:entry\.)?(\d+)$", key)
    if m:
        for q in form.questions:
            if any(e.id == int(m.group(1)) for e in q.entries):
                return q
    hits = [q for q in form.questions if q.title == key]
    if not hits:
        hits = [q for q in form.questions if q.title.replace(" ", "") == key.replace(" ", "")]
    if len(hits) == 1:
        return hits[0]
    titles = ", ".join('"%s"' % q.title for q in form.questions)
    if not hits:
        raise Abort('질문 "%s" 을(를) 폼에서 찾지 못했습니다. 폼의 질문: %s' % (key, titles))
    raise Abort('제목이 "%s" 인 질문이 여러 개입니다. entry 번호로 지정하세요 (inspect 명령으로 확인).' % key)


def check_supported(form: Form) -> None:
    if form.signin_required or form.collect_email == 2:
        raise Abort("이 폼은 구글 로그인이 필요합니다. 이 도구(파이썬)는 로그인 세션이 없으므로 "
                    "browser/console_snippet.js (브라우저 콘솔 방식)를 사용하세요.")
    for q in form.questions:
        if q.type not in (0, 1, 2, 3, 4, 5, 9, 10):
            raise Abort('질문 "%s" 의 유형(%s)은 지원하지 않습니다. browser/console_snippet.js 를 사용하세요.'
                        % (q.title, TYPE_NAMES.get(q.type, "유형 %d" % q.type)))


def normalize_answers(form: Form, pairs: List[Tuple[str, str]]) -> Dict[int, List[str]]:
    """--answer pairs -> {entry id: [values]} after validating against the form definition."""
    out: Dict[int, List[str]] = {}
    for key, value in pairs:
        q = find_question(form, key)
        e = q.entry
        value = value.strip() if q.type != 1 else value
        if value == "":
            continue
        if q.type in CHOICE_TYPES and e.options is not None and value not in e.options and not e.has_other:
            raise Abort('"%s" 의 값 "%s" 은(는) 보기에 없습니다. 보기: %s'
                        % (q.title, value, " / ".join(e.options)))
        if q.type == 9 and not re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", value):
            raise Abort('"%s" 은(는) 날짜 질문입니다. YYYY-MM-DD 형식으로 적어 주세요.' % q.title)
        if q.type == 10 and not re.match(r"^\d{1,2}:\d{2}$", value):
            raise Abort('"%s" 은(는) 시간 질문입니다. HH:MM 형식으로 적어 주세요.' % q.title)
        if q.type == 4:
            out.setdefault(e.id, []).append(value)
        else:
            if e.id in out:
                raise Abort('"%s" 에 답이 두 번 지정되었습니다.' % q.title)
            out[e.id] = [value]
    for q in form.questions:
        if q.entry.required and not out.get(q.entry.id):
            raise Abort('필수 질문 "%s" 에 답이 없습니다.' % q.title)
    return out


def build_fields(form: Form, answers: Dict[int, List[str]], email: Optional[str] = None) -> List[Tuple[str, str]]:
    """POST fields in the order a browser sends them."""
    fields: List[Tuple[str, str]] = []
    if form.collect_email == 3:
        if not email:
            raise Abort("이 폼은 이메일 주소를 입력받습니다. --email 로 지정하세요.")
        fields.append(("emailAddress", email))
    for q in form.questions:
        e = q.entry
        key = "entry.%d" % e.id
        for v in answers.get(e.id, []):
            if q.type == 9:
                y, mo, d = v.split("-")
                fields += [(key + "_year", str(int(y))), (key + "_month", str(int(mo))), (key + "_day", str(int(d)))]
            elif q.type == 10:
                h, mi = v.split(":")
                fields += [(key + "_hour", str(int(h))), (key + "_minute", str(int(mi)))]
            elif q.type in CHOICE_TYPES and e.options is not None and v not in e.options:
                fields += [(key, "__other_option__"), (key + ".other_option_response", v)]
            else:
                fields.append((key, v))
        if q.type in CHOICE_TYPES:
            fields.append((key + "_sentinel", ""))
    for name, value in form.hidden:
        if name.endswith("_sentinel") or name.startswith("entry."):
            continue
        if name == "pageHistory" and form.pages > 1:
            value = ",".join(str(i) for i in range(form.pages))
        fields.append((name, value))
    return fields


def describe_answers(form: Form, answers: Dict[int, List[str]]) -> List[str]:
    lines = []
    for q in form.questions:
        vals = answers.get(q.entry.id)
        lines.append("    %-12s : %s" % (q.title or "(제목 없음)", ", ".join(vals) if vals else "(빈칸)"))
    return lines


# ------------------------------------------------------------------------------------------------
# what did the server answer?
# ------------------------------------------------------------------------------------------------
def classify(resp: Resp, was_post: bool) -> Tuple[str, str]:
    """-> (code, detail). Codes: RECORDED, RECORDED_PROBABLE, OPEN, CLOSED, REJECTED, LOGIN, RATE_LIMITED,
    NOT_FOUND, UNKNOWN. Redirects are never followed, and a bare HTTP 200 is never taken as success."""
    st = resp.status
    loc = resp.location
    if st in (301, 302, 303, 307, 308):
        if "accounts.google.com" in loc or "ServiceLogin" in loc:
            return "LOGIN", "구글 로그인 페이지로 보내졌습니다"
        if "/sorry" in loc:
            return "RATE_LIMITED", "구글이 비정상 트래픽으로 판단했습니다 (sorry 페이지)"
        if "closedform" in loc:
            return "CLOSED", "폼이 응답을 받지 않는 상태입니다 (closedform)"
        return "UNKNOWN", "예상하지 못한 리디렉션: %s" % loc[:120]
    if st == 429:
        return "RATE_LIMITED", "HTTP 429 (요청이 너무 많음)"
    if st in (401, 403):
        return "LOGIN", "HTTP %d (권한 필요)" % st
    if st in (404, 410):
        return "NOT_FOUND", "HTTP %d (폼을 찾을 수 없음)" % st
    text = resp.text
    is_form_page = "FB_PUBLIC_LOAD_DATA_" in text or re.search(r'<input[^>]*name="fbzx"', text) is not None
    if st == 400:
        return "REJECTED", "HTTP 400 (입력값이 폼과 맞지 않음)"
    if st >= 500:
        return "UNKNOWN", "HTTP %d (서버 오류 - 기록 여부 불명)" % st
    if st != 200:
        return "UNKNOWN", "HTTP %d" % st
    if is_form_page:
        return ("REJECTED", "폼 페이지가 다시 표시되었습니다 (기록되지 않음)") if was_post else ("OPEN", "응답 받는 중")
    if any(t in text for t in CLOSED_TEXT):
        return "CLOSED", "폼이 응답을 받지 않는 상태입니다"
    if was_post:
        if any(t in text for t in CONFIRM_TEXT) or "usp=form_confirm" in text:
            return "RECORDED", "제출 확인 페이지를 받았습니다"
        return "RECORDED_PROBABLE", "HTTP 200 확인 페이지(문구는 설문 작성자가 바꾼 것으로 보임)"
    return "UNKNOWN", "알 수 없는 페이지"


# ------------------------------------------------------------------------------------------------
# session: connection + cookies + clock for one form
# ------------------------------------------------------------------------------------------------
class RateLimited(Exception):
    pass


_sessions: List["Session"] = []


class Session:
    def __init__(self, url: str, cafile: Optional[str] = None):
        self.t = parse_target(url)
        _sessions.append(self)
        self.cafile = cafile
        self.jar = CookieJar()
        self.clock = ServerClock()
        self.conn: Optional[RawConn] = None
        self.addr = None
        self.rtts: List[float] = []            # front-end round trips on the current connection (ms)
        self.fe_ok = True                      # /generate_204 answers 204 + Date
        self.page_gaps: List[float] = []       # per page GET: server clock read minus send, raw (ms + theta)
        self.n_requests = 0
        self.reconnects = 0
        self.shot_started = False      # True once a submission may be on the wire

    # -- connection ------------------------------------------------------------------------------
    def reconnect(self) -> RawConn:
        if self.conn is not None:
            self.conn.close()
            self.reconnects += 1
        c = RawConn(self.t.scheme, self.t.host, self.t.port, addr=self.addr, cafile=self.cafile)
        try:
            c.connect()
        except NetError:
            if self.addr is None:
                raise
            c = RawConn(self.t.scheme, self.t.host, self.t.port, cafile=self.cafile).connect()
        self.addr = c.addr
        self.conn = c
        self.rtts = []
        return c

    def ensure(self) -> RawConn:
        if self.conn is None or self.conn.sock is None:
            return self.reconnect()
        return self.conn

    def headers(self, path: str, extra=()) -> List[Tuple[str, str]]:
        h = [("User-Agent", UA), ("Accept", "text/html,*/*;q=0.8"), ("Accept-Language", "ko-KR,ko;q=0.9,en;q=0.5"),
             ("Accept-Encoding", "identity")]
        h += list(extra)
        cookie = self.jar.header(path)
        if cookie:
            h.append(("Cookie", cookie))
        return h

    # -- measurements ----------------------------------------------------------------------------
    def _absorb(self, r: Resp, frontend: bool) -> None:
        self.n_requests += 1
        self.jar.update(r.set_cookies)
        if r.status == 429 or "/sorry" in r.location:
            raise RateLimited()
        t_send = ms(r.t_send_ns)
        if r.date_s is not None:
            self.clock.add(r.date_s * 1000.0 - ms(r.t_first_ns), (r.date_s + 1) * 1000.0 - t_send, r.t_first_ns,
                           "fe" if frontend else "page")
        if r.server_time_ms is not None and r.t_token_ns is not None:
            self.clock.add(r.server_time_ms - ms(r.t_token_ns), r.server_time_ms + 1.0 - t_send, r.t_token_ns, "page")
            self.page_gaps.append(r.server_time_ms - t_send)
        if frontend:
            self.rtts.append(ms(r.t_first_ns - r.t_send_ns))
            self.rtts = self.rtts[-60:]
        if not r.keepalive and self.conn is not None:
            self.conn.close()

    def _do(self, method: str, path: str, t_send_ns: Optional[int], timeout: float) -> Resp:
        last: Optional[Exception] = None
        for attempt in (0, 1):
            c = self.ensure()
            try:
                data = c.build(method, path, self.headers(path))
                if t_send_ns is not None and attempt == 0:
                    wait_until(t_send_ns, spin_ms=3.0)
                t0, _t1 = c.send(data)
                return c.read_response(method, t0, timeout)
            except NetError as e:
                last = e
                c.close()
        raise NetError("서버와 통신하지 못했습니다: %s" % last)

    def probe(self, t_send_ns: Optional[int] = None) -> Resp:
        """Tiny front-end request whose Date header brackets the clock."""
        if self.fe_ok:
            r = self._do("HEAD", FE_PROBE_PATH, t_send_ns, 6.0)
            if r.status == 204 and r.date_s is not None:
                self._absorb(r, frontend=True)
                return r
            self.fe_ok = False
        r = self._do("HEAD", self.t.view_path, t_send_ns, 8.0)
        self._absorb(r, frontend=False)
        self.rtts.append(ms(r.t_first_ns - r.t_send_ns))
        return r

    def page(self, timeout: float = 15.0) -> Resp:
        """GET the form page (never follows redirects)."""
        r = self._do("GET", self.t.view_path, None, timeout)
        self._absorb(r, frontend=False)
        return r

    def next_aim_ns(self, not_before_ns: int) -> Optional[int]:
        """When to send the next probe so that it straddles a second flip of the server clock."""
        b = self.clock.bounds()
        if not b or not self.rtts:
            return None
        lo, hi = b
        rmin = min(self.rtts)
        width = hi - lo
        dither = random.uniform(-0.35, 0.35) * max(width, 1.0) if width < 1.5 * rmin + 1.0 else 0.0
        off = (lo + hi) / 2 + rmin / 2 + dither          # server_ms at arrival = local_ms at send + off
        k = math.ceil((ms(not_before_ns) + off) / 1000.0) * 1000.0
        return int((k - off) * 1e6)

    def rtt_stats(self) -> Optional[Tuple[float, float]]:
        if not self.rtts:
            return None
        return min(self.rtts), statistics.median(self.rtts)

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()


def choose_backend(sess: Session, tries: int = 3, per: int = 3) -> None:
    """Google pins a client to one Forms backend through the COMPASS cookie, and backends differ in
    speed. Try a few cookie jars with page loads and keep the one that reaches the backend clock fastest.
    Read-only; best effort."""
    best: Optional[Tuple[float, CookieJar]] = None
    for _i in range(tries):
        jar = CookieJar()
        sess.jar = jar
        gaps = []
        try:
            for _j in range(per):
                before = len(sess.page_gaps)
                r = sess.page()
                if classify(r, False)[0] != "OPEN":
                    break
                if len(sess.page_gaps) > before:
                    gaps.append(sess.page_gaps[-1])
                time.sleep(0.35)
        except (NetError, RateLimited):
            break
        if gaps:
            med = statistics.median(gaps)
            if best is None or med < best[0] - 1.0:
                best = (med, jar)
    if best is not None:
        sess.jar = best[1]
    sess.page_gaps = []


# ------------------------------------------------------------------------------------------------
# the timed shot
# ------------------------------------------------------------------------------------------------
@dataclass
class Shot:
    outcome: str                      # classify() code, or NOT_SENT / UNKNOWN
    detail: str
    target_ms: float
    t_plan_ns: int = 0
    t_before_ns: int = 0              # just before the single write
    t_after_ns: int = 0
    theta_lo: float = 0.0
    theta_hi: float = 0.0
    resp: Optional[Resp] = None
    retried: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def left_pc_lo(self) -> float:    # earliest possible server time at which the request left this PC
        return ms(self.t_before_ns) + self.theta_lo

    @property
    def left_pc_hi(self) -> float:
        return ms(self.t_after_ns) + self.theta_hi


def timed_send(sess: Session, data: bytes, method: str, target_ms: float, shift_ms: float,
               on_unknown: str = "stop") -> Shot:
    """Release ``data`` in ONE write at local time (target - theta_lo + shift). The final second is
    single-threaded, silent and garbage-collector free."""
    b = sess.clock.bounds()
    if not b:
        raise Abort("서버 시계를 측정하지 못했습니다.")
    shot = Shot("NOT_SENT", "", target_ms)

    def plan() -> int:
        lo, hi = sess.clock.bounds()
        t = int((target_ms - lo + shift_ms) * 1e6)
        lo, hi = sess.clock.bounds(max(t, now_ns()))     # bounds as they will be trusted AT the release
        shot.theta_lo, shot.theta_hi = lo, hi
        return int((target_ms - lo + shift_ms) * 1e6)

    try:
        t_rel = plan()
        sleep_until(t_rel - 300_000_000)
        conn = sess.ensure()
        if not conn.alive():
            shot.notes.append("발사 0.3초 전 연결이 끊겨 있어 다시 연결했습니다")
            conn = sess.reconnect()
        gc.collect()
        gc.disable()
        t_rel = plan()                                   # bounds aged a little: can only move later
        shot.t_plan_ns = t_rel
        sleep_until(t_rel - int(SPIN_MS * 1e6))
        if not conn.alive():
            shot.notes.append("발사 직전 연결이 끊겨 다시 연결했습니다 (그만큼 늦어짐)")
            conn = sess.reconnect()
        while now_ns() < t_rel:
            pass
    except KeyboardInterrupt:
        gc.enable()
        raise Abort("취소했습니다. 아무것도 제출하지 않았습니다.", 2)
    except NetError as e:
        gc.enable()
        shot.outcome, shot.detail = "NOT_SENT", "발사 직전 연결에 실패했습니다: %s" % e
        return shot
    sess.shot_started = True
    try:
        try:
            shot.t_before_ns, shot.t_after_ns = conn.send(data)
        except NetError as e:
            # The single write failed, so no complete request exists on the wire: safe to send once more.
            shot.notes.append("첫 전송 실패(%s) - 새 연결로 즉시 재전송" % e)
            shot.retried = True
            conn = sess.reconnect()
            shot.t_before_ns, shot.t_after_ns = conn.send(data)
    except NetError as e:
        shot.outcome, shot.detail = "NOT_SENT", "전송하지 못했습니다: %s" % e
        return shot
    finally:
        gc.enable()
    try:
        shot.resp = conn.read_response(method, shot.t_before_ns, timeout=25.0)
    except NetError as e:
        shot.outcome = "UNKNOWN"
        shot.detail = "요청은 보냈지만 응답을 받지 못했습니다 (%s)" % e
        if on_unknown == "retry" and method == "POST":
            shot.notes.append("--on-unknown retry: 중복 기록 위험을 감수하고 한 번 더 보냅니다")
            try:
                conn = sess.reconnect()
                t0, t1 = conn.send(data)
                shot.resp = conn.read_response(method, t0, timeout=25.0)
                shot.retried = True
            except NetError as e2:
                shot.detail += " / 재전송도 실패: %s" % e2
                return shot
        else:
            return shot
    try:
        sess._absorb(shot.resp, frontend=False)
    except RateLimited:
        pass
    shot.outcome, shot.detail = classify(shot.resp, was_post=(method == "POST"))
    return shot


def sync_line(sess: Session, target_ms: Optional[float]) -> str:
    sn = sess.clock.server_now()
    if not sn:
        return "서버 시계 측정 중..."
    est, half = sn
    parts = ["서버시각 %s" % fmt_clock(est, 3), "시계오차 ±%.1fms" % half]
    rs = sess.rtt_stats()
    if rs:
        parts.append("왕복 %.1fms" % rs[0])
    if target_ms is not None:
        rem = (target_ms - est) / 1000.0
        parts.insert(1, "남은 시간 %s" % fmt_remaining(rem))
    return " | ".join(parts)


def fmt_remaining(sec: float) -> str:
    if sec < 0:
        return "지남"
    if sec < 120:
        return "%.1f초" % sec
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    out = ""
    if d:
        out += "%d일 " % d
    if h or d:
        out += "%d시간 " % h
    return out + "%d분 %d초" % (m, s)


def remaining_s(sess: Session, target_ms: float) -> float:
    """Seconds until the target on the server clock (conservative: uses the upper clock estimate)."""
    t = now_ns()
    b = sess.clock.bounds(t)
    return (target_ms - (ms(t) + b[1])) / 1000.0


def initial_sync(sess: Session, n: int = 3) -> None:
    for _ in range(n):
        try:
            sess.probe(sess.next_aim_ns(now_ns() + 30_000_000))
        except RateLimited:
            raise Abort("구글이 요청을 제한하고 있습니다 (429). 몇 분 뒤 다시 시도하세요.")


def countdown(sess: Session, target_ms: float, *, pages: int = 3, on_page: Optional[Callable[[Resp], None]] = None,
              rotate: bool = True, show: bool = True) -> None:
    """Keep the clock bracket fresh until about 2 s before the target.

    far (> 75 s)   one probe every 20 s
    dense          one probe per second, aimed at the second flips
    T-25 s         fresh connection for the shot, then ``pages`` page loads (fresh hidden fields, cookies,
                   backend clock samples) and aimed probes on that connection until T-2 s
    """
    if not sess.clock.bounds():
        initial_sync(sess)
    rotated = not rotate
    pages_done = 0
    last_page_ns = 0
    backoff = 0.0
    while True:
        rem = remaining_s(sess, target_ms)
        if rem <= 2.2:
            break
        if show:
            status(sync_line(sess, target_ms))
        try:
            if backoff:
                time.sleep(min(backoff, max(0.0, rem - 3.0)))
                backoff = 0.0
                continue
            if rem > 75:
                sess.probe()
                nap = min(20.0, rem - 70.0)
                end = now_ns() + int(nap * 1e9)
                while now_ns() < end:
                    time.sleep(min(1.0, max(0.0, (end - now_ns()) / 1e9)))
                    if show:
                        status(sync_line(sess, target_ms))
                continue
            if not rotated and rem <= 26:
                if rem > 9:                 # fresh connection for the shot (never idle-timed-out, warm)
                    sess.reconnect()
                    sess.probe()
                rotated = True
                continue
            if rotated and pages_done < pages and rem > 6.5 and now_ns() - last_page_ns > 3_500_000_000 \
                    and (rem <= 24 or not rotate):
                r = sess.page()
                pages_done += 1
                last_page_ns = now_ns()
                if on_page:
                    on_page(r)
                continue
            aim = sess.next_aim_ns(now_ns() + 120_000_000)
            if aim is None:
                sess.probe()
                continue
            if remaining_s(sess, target_ms) - (aim - now_ns()) / 1e9 < 2.0:
                break
            sess.probe(aim)
        except RateLimited:
            warn("구글이 요청을 제한했습니다(429). 측정 빈도를 낮춥니다.")
            backoff = 5.0
        except NetError as e:
            if remaining_s(sess, target_ms) < 4.0:
                break
            warn("측정 요청 실패(%s) - 다시 연결합니다." % e)
            sess.reconnect()
    if show:
        status(sync_line(sess, target_ms))
        status_end()


def backend_delay_estimate(sess: Session) -> Optional[float]:
    """Median time from 'request leaves this PC' to 'Forms backend reads its clock' over recent page loads
    (estimated with the midpoint clock; a GET-based figure, the POST may differ)."""
    b = sess.clock.bounds()
    if not b or not sess.page_gaps:
        return None
    mid = (b[0] + b[1]) / 2
    return statistics.median(g - mid for g in sess.page_gaps[-6:])


# ------------------------------------------------------------------------------------------------
# target time
# ------------------------------------------------------------------------------------------------
def parse_at(text: str, utc_offset: Optional[str]) -> float:
    """'YYYY-MM-DD HH:MM[:SS[.mmm]]' (+ optional offset) -> epoch ms. The date is mandatory on purpose."""
    s = text.strip().replace("T", " ")
    m = re.match(r"^(\d{4}-\d{2}-\d{2}) (\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?\s*(Z|[+-]\d{2}:?\d{2})?$", s)
    if not m:
        raise Abort('--at 은 날짜와 24시간제 시각을 모두 적어야 합니다. 예: --at "2026-11-23 22:00:00" '
                    "(오전 10시는 10:00:00, 밤 10시는 22:00:00)")
    date, hh, mm, ss, frac, off = m.groups()
    y, mo, d = (int(x) for x in date.split("-"))
    try:
        naive = _dt.datetime(y, mo, d, int(hh), int(mm), int(ss or 0), int(((frac or "0") + "000000")[:6]))
    except ValueError as e:
        raise Abort("--at 의 날짜/시각이 올바르지 않습니다: %s" % e)
    off = off or utc_offset
    if off:
        if off.upper() == "Z":
            tz = _dt.timezone.utc
        else:
            mo2 = re.match(r"^([+-])(\d{2}):?(\d{2})$", off.strip())
            if not mo2:
                raise Abort("--utc-offset 형식은 +09:00 과 같아야 합니다.")
            sign = 1 if mo2.group(1) == "+" else -1
            tz = _dt.timezone(sign * _dt.timedelta(hours=int(mo2.group(2)), minutes=int(mo2.group(3))))
        aware = naive.replace(tzinfo=tz)
    else:
        aware = naive.astimezone()          # this PC's time zone
    return aware.timestamp() * 1000.0


def describe_target(target_ms: float) -> str:
    local = _dt.datetime.fromtimestamp(target_ms / 1000.0, _dt.timezone.utc).astimezone()
    utc = _dt.datetime.fromtimestamp(target_ms / 1000.0, _dt.timezone.utc)
    off = local.utcoffset() or _dt.timedelta(0)
    sign = "+" if off >= _dt.timedelta(0) else "-"
    off = abs(off)
    return "%s (%s) %s.%03d  [UTC%s%02d:%02d, 세계표준시 %s]" % (
        local.strftime("%Y-%m-%d"), WEEKDAY[local.weekday()], local.strftime("%H:%M:%S"),
        int(target_ms) % 1000, sign, off.seconds // 3600, off.seconds % 3600 // 60, utc.strftime("%m-%d %H:%M:%S"))


# ------------------------------------------------------------------------------------------------
# reporting
# ------------------------------------------------------------------------------------------------
def load_calibration(path: Optional[str]) -> Optional[dict]:
    p = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), CALIBRATION_FILE)
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and "lag_min_ms" in data else None
    except (OSError, ValueError):
        return None


def report_shot(shot: Shot, what: str, cal: Optional[dict] = None) -> None:
    T = shot.target_ms
    say("")
    if shot.t_before_ns:
        over = ms(shot.t_before_ns - shot.t_plan_ns)
        say("  요청이 PC를 떠난 시각(서버 기준) : %s ~ %s   (목표 %s ~ %s)" % (
            fmt_clock(shot.left_pc_lo), fmt_clock(shot.left_pc_hi), fmt_rel(shot.left_pc_lo - T),
            fmt_rel(shot.left_pc_hi - T)))
        if over > 1.0:
            say("  (PC 사정으로 계획보다 %.1f ms 늦게 나갔습니다)" % over)
    r = shot.resp
    if r is not None:
        if r.server_time_ms is not None:
            say("  %s 서버시각            : %s   (목표 %s)" % (what, fmt_clock(r.server_time_ms),
                                                      fmt_rel(r.server_time_ms - T)))
        elif r.date_s is not None:
            say("  응답의 Date 헤더(초 단위)        : %s" % fmt_clock(r.date_s * 1000.0, 0))
    for n in shot.notes:
        say("  * " + n)
    if shot.outcome in ("RECORDED", "RECORDED_PROBABLE") and r is not None and r.server_time_ms is not None:
        lo, hi = shot.left_pc_lo, float(r.server_time_ms)
        say("")
        say("  => 기록 시각은 %s ~ %s 사이입니다 (목표 %s ~ %s)." % (
            fmt_clock(lo), fmt_clock(hi), fmt_rel(lo - T), fmt_rel(hi - T)))
        if cal and cal.get("conf_gap_median_ms") is not None:
            est = hi - float(cal["conf_gap_median_ms"])
            say("     보정 자료 기준 추정 기록 시각: 약 %s (목표 %s)" % (fmt_clock(est), fmt_rel(est - T)))
        if hi - T >= 500 and lo - T < 500:
            say("     (참고: 기록 시각의 밀리초가 500 이상이면 응답 시트의 기본 표시는 다음 초로 반올림되어 보입니다.)")
        elif lo - T >= 500:
            say("     (주의: 목표보다 0.5초 이상 늦어, 응답 시트의 기본 표시로는 다음 초로 보입니다.)")


def save_log(shot: Shot, kind: str, extra: Optional[dict] = None) -> Optional[str]:
    """Evidence for later: times, bounds and the raw server answer."""
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    name = "gform_ontime_%s_%s" % (kind, stamp)
    data = {
        "tool_version": VERSION, "kind": kind, "outcome": shot.outcome, "detail": shot.detail,
        "target_server_ms": shot.target_ms, "theta_lo_ms": shot.theta_lo, "theta_hi_ms": shot.theta_hi,
        "left_pc_server_ms": [shot.left_pc_lo, shot.left_pc_hi] if shot.t_before_ns else None,
        "notes": shot.notes, "retried": shot.retried, "pc_wall_clock": _dt.datetime.now().astimezone().isoformat(),
    }
    if shot.resp is not None:
        data["response"] = {"status": shot.resp.status, "date_header": shot.resp.headers.get("date"),
                            "server_time_ms": shot.resp.server_time_ms, "location": shot.resp.location}
    data.update(extra or {})
    try:
        with open(name + ".json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        if shot.resp is not None and shot.resp.body:
            with open(name + ".html", "wb") as f:
                f.write(shot.resp.body)
        return name + ".json"
    except OSError:
        return None


EXIT = {"RECORDED": 0, "RECORDED_PROBABLE": 0, "CLOSED": 3, "REJECTED": 4, "LOGIN": 4, "RATE_LIMITED": 4,
        "NOT_FOUND": 4, "UNKNOWN": 5, "NOT_SENT": 5, "OPEN": 0}


# ------------------------------------------------------------------------------------------------
# commands
# ------------------------------------------------------------------------------------------------
def open_form(sess: Session) -> Tuple[str, str, Optional[Form], Resp]:
    try:
        r = sess.page()
    except RateLimited:
        raise Abort("구글이 요청을 제한하고 있습니다 (429). 몇 분 뒤 다시 시도하세요.")
    code, detail = classify(r, was_post=False)
    form = None
    if code == "OPEN":
        try:
            form = parse_form(r.text)
        except ValueError as e:
            raise Abort("폼 페이지를 해석하지 못했습니다: %s" % e)
    return code, detail, form, r


def cmd_inspect(a) -> int:
    sess = Session(a.url, a.cafile)
    code, detail, form, r = open_form(sess)
    if form is None:
        say("폼 상태: %s - %s" % (code, detail))
        if code == "CLOSED":
            say("닫혀 있는 동안에는 질문 목록을 읽을 수 없습니다. 열리는 시각에 맞춰 fire 를 실행하면 "
                "열릴 때까지 기다렸다가 제출합니다 (--answer 로 답을 미리 지정).")
        return EXIT.get(code, 5)
    say("폼 제목 : %s" % form.title)
    say("상태    : 응답 받는 중%s%s" % (", 로그인 필요" if form.signin_required or form.collect_email == 2 else "",
                                  ", 캡차 검사 사용" if form.captcha else ""))
    say("페이지  : %d" % form.pages)
    say("")
    for i, q in enumerate(form.questions, 1):
        e = q.entry
        say("%d. %s  [%s%s]  entry.%d" % (i, q.title or "(제목 없음)", TYPE_NAMES.get(q.type, "유형 %d" % q.type),
                                         ", 필수" if e.required else ", 선택", e.id))
        if e.options:
            say("     보기: %s%s" % (" / ".join(e.options), " / (기타 직접 입력 가능)" if e.has_other else ""))
    if r.server_time_ms is not None:
        say("")
        say("이 페이지를 만든 순간의 구글 서버 시각: %s" % fmt_clock(r.server_time_ms))
    say("")
    answers = " ".join('--answer "%s=..."' % q.title for q in form.questions)
    say("제출 명령 예시:")
    say('  python gform_ontime.py fire "%s" --at "YYYY-MM-DD HH:MM:SS" %s' % (sess.t.view_url, answers))
    try:
        check_supported(form)
    except Abort as e:
        say("")
        warn(str(e))
    return 0


def cmd_clock(a) -> int:
    sess = Session(a.url, a.cafile)
    say("구글 서버 시계를 측정합니다 (%d초). Ctrl+C 로 중단." % a.seconds)
    initial_sync(sess, 3)
    end = now_ns() + int(a.seconds * 1e9)
    try:
        while now_ns() < end:
            sess.probe(sess.next_aim_ns(now_ns() + 120_000_000))
            status(sync_line(sess, None))
    except KeyboardInterrupt:
        pass
    status_end()
    est, half = sess.clock.server_now()
    pc = time.time() * 1000.0
    rs = sess.rtt_stats()
    say("")
    say("구글 서버 시각        : %s  (±%.1f ms)" % (fmt_clock(est), half))
    say("이 PC 시계와의 차이   : PC 시계가 %.0f ms %s" % (abs(pc - est), "빠릅니다" if pc > est else "느립니다"))
    if rs:
        say("왕복 지연(최소/중간) : %.1f / %.1f ms   (측정 %d회, 시계 재설정 %d회)" % (
            rs[0], rs[1], sess.n_requests, sess.clock.resets))
    say("시간 기준            : %s" % CLOCK_NAME)
    return 0


def next_whole_second(sess: Session, lead_s: float) -> float:
    est, half = sess.clock.server_now()
    return math.ceil((est + half + lead_s * 1000.0) / 1000.0) * 1000.0


def cmd_rehearse(a) -> int:
    """Everything except the submission: at the release instant a harmless GET of the form page is sent."""
    setup_process()
    sess = Session(a.url, a.cafile)
    code, detail, _form, _r = open_form(sess)
    say("폼 상태: %s" % detail)
    if code not in ("OPEN", "CLOSED"):
        return EXIT.get(code, 5)
    initial_sync(sess)
    landings = []
    for i in range(a.count):
        target = parse_at(a.at, a.utc_offset) if (a.at and i == 0) else next_whole_second(sess, a.lead if i == 0 else 6.0)
        if a.at and i == 0 and remaining_s(sess, target) < 5:
            raise Abort("목표 시각이 이미 지났거나 너무 가깝습니다.")
        say("리허설 %d/%d - 목표 서버시각 %s (제출하지 않습니다)" % (i + 1, a.count, fmt_clock(target)))
        countdown(sess, target, pages=1, rotate=(remaining_s(sess, target) > 30))
        data = sess.ensure().build("GET", sess.t.view_path, sess.headers(sess.t.view_path))
        shot = timed_send(sess, data, "GET", target, a.guard_ms)
        report_shot(shot, "폼 서버가 처리한")
        if shot.resp is not None and shot.resp.server_time_ms is not None:
            landings.append(shot.resp.server_time_ms - target)
    if landings:
        say("")
        say("요약: 목표 대비 폼 서버 처리 시각 = 최소 %s / 중간 %s / 최대 %s  (%d회)" % (
            fmt_rel(min(landings)), fmt_rel(statistics.median(landings)), fmt_rel(max(landings)), len(landings)))
        if min(landings) < 0:
            warn("목표보다 이른 결과가 있습니다. 이 도구의 가정이 이 환경에서 깨졌다는 뜻이니 실제 제출에 쓰지 마세요.")
        say("실제 제출(POST)은 구글 쪽 처리가 조금 더 걸릴 수 있습니다. 정확한 값은 본인 폼으로 calibrate 하면 알 수 있습니다.")
    else:
        say("폼 페이지에서 서버 시각을 읽지 못해(닫힌 폼 등) 도착 시각은 확인하지 못했습니다. 시계 동기화와 발사 절차는 정상 수행되었습니다.")
        return 0
    return 0 if min(landings) >= 0 else 5


def ask_answers(form: Form, given: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Ask on the terminal for every question that has no --answer."""
    have = set()
    for key, _v in given:
        have.add(find_question(form, key).entry.id)
    pairs = list(given)
    for q in form.questions:
        if q.entry.id in have:
            continue
        if not sys.stdin or not sys.stdin.isatty():
            if q.entry.required:
                raise Abort('필수 질문 "%s" 의 답을 --answer 로 지정하세요.' % q.title)
            continue
        hint = ""
        if q.entry.options:
            hint = " (%s)" % " / ".join(q.entry.options)
        elif q.type == 9:
            hint = " (YYYY-MM-DD)"
        elif q.type == 10:
            hint = " (HH:MM)"
        if q.type == 4:
            hint += " 여러 개는 쉼표로 구분"
        value = input("  %s%s%s: " % (q.title, hint, "" if q.entry.required else " [빈칸 가능]")).strip()
        if q.type == 4:
            pairs += [("entry.%d" % q.entry.id, v) for v in value.split(",") if v.strip()]
        elif value:
            pairs.append(("entry.%d" % q.entry.id, value))
    return pairs


def make_post(sess: Session, form: Form, fields: List[Tuple[str, str]]) -> bytes:
    path = form.action_path if form.action_path.endswith("/formResponse") else sess.t.post_path
    body = urllib.parse.urlencode(fields).encode("ascii")
    extra = [("Content-Type", "application/x-www-form-urlencoded"), ("Origin", sess.t.origin),
             ("Referer", sess.t.view_url)]
    return sess.ensure().build("POST", path, sess.headers(path, extra), body)


class Plan:
    """What will be submitted; rebuilt from every fresh page load so hidden fields stay current."""

    def __init__(self, sess: Session, pairs: List[Tuple[str, str]], email: Optional[str]):
        self.sess, self.pairs, self.email = sess, pairs, email
        self.form: Optional[Form] = None
        self.answers: Dict[int, List[str]] = {}
        self.data: Optional[bytes] = None
        self.closed = False
        self.error: Optional[str] = None

    def use(self, form: Form) -> None:
        check_supported(form)
        self.answers = normalize_answers(form, self.pairs)
        if not any(self.answers.values()):
            raise Abort("모든 답이 비어 있습니다. 빈 응답은 제출하지 않습니다.")
        self.form = form
        self.data = make_post(self.sess, form, build_fields(form, self.answers, self.email))
        self.closed = False

    def on_page(self, r: Resp) -> None:
        code, _detail = classify(r, was_post=False)
        if code == "CLOSED":
            self.closed = True
            return
        if code != "OPEN":
            return
        try:
            form = parse_form(r.text)
            changed = self.form is not None and form.sha1 != self.form.sha1
            self.use(form)
            if changed:
                status_end()
                warn("폼 구성이 방금 바뀌었습니다. 새 구성으로 답을 다시 확인했습니다.")
        except (Abort, ValueError) as e:
            self.error = str(e)


def wait_open_and_fire(sess: Session, plan: Plan, target_ms: float, shift_ms: float, poll_ms: float,
                       wait_max_s: float, on_unknown: str) -> Shot:
    """The form is closed: poll until it opens. If it opens before the target, do the normal timed shot;
    if it opens at/after the target, submit immediately."""
    say("폼이 아직 닫혀 있습니다. 열릴 때까지 확인하다가, 열리는 즉시(단, 목표 시각 이전에는 보내지 않고) 제출합니다.")
    deadline = now_ns() + int((max(0.0, remaining_s(sess, target_ms)) + wait_max_s) * 1e9)
    polls = 0
    while now_ns() < deadline:
        rem = remaining_s(sess, target_ms)
        gap = 2.0 if rem > 6 else (poll_ms / 1000.0 if rem > -90 else 1.0)
        t_next = now_ns() + int(gap * 1e9)
        try:
            c = sess.ensure()
            head = c.request("HEAD", sess.t.view_path, sess.headers(sess.t.view_path), timeout=8.0)
            polls += 1
            sess._absorb(head, frontend=False)
            if head.status == 200:
                r = sess.page()
                plan.on_page(r)
                if plan.error:
                    raise Abort("폼이 열렸지만 답을 맞출 수 없습니다: %s" % plan.error, 4)
                if plan.data is not None and not plan.closed:
                    status_end()
                    lo, _hi = sess.clock.bounds()
                    if ms(now_ns()) + lo >= target_ms + shift_ms:
                        say("폼이 열렸습니다 - 즉시 제출합니다.")
                        shot = Shot("NOT_SENT", "", target_ms)
                        shot.theta_lo, shot.theta_hi = sess.clock.bounds()
                        shot.t_plan_ns = now_ns()
                        conn = sess.ensure()
                        sess.shot_started = True
                        try:
                            shot.t_before_ns, shot.t_after_ns = conn.send(plan.data)
                        except NetError:
                            conn = sess.reconnect()
                            shot.retried = True
                            shot.t_before_ns, shot.t_after_ns = conn.send(plan.data)
                        try:
                            shot.resp = conn.read_response("POST", shot.t_before_ns, timeout=25.0)
                            shot.outcome, shot.detail = classify(shot.resp, was_post=True)
                        except NetError as e:
                            shot.outcome, shot.detail = "UNKNOWN", "요청은 보냈지만 응답을 받지 못했습니다 (%s)" % e
                        shot.notes.append("폼이 열린 것을 확인하고 바로 보냈습니다 (확인 %d회)" % polls)
                        return shot
                    say("폼이 목표 시각 전에 열렸습니다 - 목표 시각에 맞춰 제출합니다.")
                    countdown(sess, target_ms, pages=1, on_page=plan.on_page, rotate=False)
                    return timed_send(sess, plan.data, "POST", target_ms, shift_ms, on_unknown)
            status("폼 열림 대기 중 | " + sync_line(sess, target_ms) + " | 확인 %d회" % polls)
        except RateLimited:
            warn("구글이 요청을 제한했습니다(429). 5초 쉬었다가 계속합니다.")
            t_next = now_ns() + 5_000_000_000
        except NetError:
            sess.reconnect()
        sleep_until(t_next)
    status_end()
    return Shot("CLOSED", "기다렸지만 폼이 열리지 않았습니다 (아무것도 제출하지 않음)", target_ms)


def cmd_fire(a) -> int:
    setup_process()
    single_instance(parse_target(a.url))           # before any network activity
    target_ms = parse_at(a.at, a.utc_offset)
    pairs = [tuple(x.split("=", 1)) for x in a.answer]
    for p in pairs:
        if len(p) != 2:
            raise Abort('--answer 는 "질문=값" 형식입니다.')
    sess = Session(a.url, a.cafile)
    code, detail, form, _r = open_form(sess)
    plan = Plan(sess, pairs, a.email)
    cal = None if a.no_calibration else load_calibration(a.calibration)

    say("폼        : %s" % (form.title if form else "(닫혀 있어 제목을 읽을 수 없음)"))
    if code == "OPEN":
        check_supported(form)
        if form.captcha:
            warn("이 폼은 제출 시 캡차 검사를 사용합니다. 이 방식은 거절될 수 있으니 브라우저 스니펫을 권장합니다.")
        plan.pairs = ask_answers(form, pairs)
        plan.use(form)
        say("제출할 답 :")
        for line in describe_answers(form, plan.answers):
            say(line)
        blanks = [q.title for q in form.questions if not plan.answers.get(q.entry.id)]
        if blanks:
            warn("빈칸으로 제출되는 질문: %s" % ", ".join(blanks))
    elif code == "CLOSED":
        if a.no_wait_open:
            raise Abort("폼이 닫혀 있습니다. (--no-wait-open 이 지정되어 기다리지 않습니다)", 3)
        if not pairs:
            raise Abort('폼이 닫혀 있어 질문을 읽을 수 없습니다. --answer "질문제목=값" 으로 답을 미리 지정하세요.')
        say("상태      : 닫혀 있음 - 열릴 때까지 기다립니다. 답은 열리는 순간 폼과 대조합니다:")
        for k, v in pairs:
            say("    %s = %s" % (k, v))
    else:
        raise Abort("폼을 사용할 수 없습니다: %s" % detail, EXIT.get(code, 5))

    initial_sync(sess)
    est, half = sess.clock.server_now()
    say("목표 시각 : %s" % describe_target(target_ms))
    say("            지금부터 %s 후 (구글 서버 시계 기준, 현재 %s)" % (
        fmt_remaining((target_ms - est) / 1000.0), fmt_clock(est)))
    if target_ms - est < 5000:
        raise Abort("목표 시각이 이미 지났거나 5초도 남지 않았습니다. --at 을 확인하세요.")

    # release rule
    if a.mode == "fast":
        lead = a.lead_ms if a.lead_ms is not None else (cal or {}).get("lag_min_ms")
        if lead is None:
            raise Abort("fast 모드는 보정값이 필요합니다. 본인 폼으로 calibrate 를 먼저 실행하거나 --lead-ms 를 지정하세요.")
        if cal and cal.get("lag_min_ms", 0) < 0 and a.lead_ms is None:
            raise Abort("보정 자료에 음수 지연이 있습니다(가정 위반). fast 모드를 사용할 수 없습니다.")
        margin = 30.0 if a.margin_ms is None else a.margin_ms
        if margin < 0:
            raise Abort("--margin-ms 는 0 이상이어야 합니다 (목표 시각 이전을 겨냥하지 않습니다).")
        advance = max(0.0, float(lead) - margin)
        shift = -advance
        say("모드      : fast - 측정된 최소 지연 %.0f ms 중 %.0f ms 만큼 일찍 보냅니다 (여유 %.0f ms)." % (lead, advance, margin))
        warn("fast 모드는 '절대 이르지 않음'이 보장되지 않습니다. 구글 쪽 처리가 보정 때보다 빠르면 목표 이전으로 찍힐 수 있습니다.")
    else:
        shift = a.guard_ms
        if shift < 0:
            raise Abort("--guard-ms 는 0 이상이어야 합니다.")
        say("모드      : safe - 요청이 서버 시각 기준 목표+%.0f ms 이전에는 PC를 떠나지 않습니다 (이르게 찍힐 수 없음)." % shift)
        if cal:
            say("            보정 자료 기준 예상 기록 시각: 목표 %s ~ %s" % (
                fmt_rel(shift + cal["lag_min_ms"]), fmt_rel(shift + cal.get("lag_max_ms", cal["lag_min_ms"]))))
    if not a.yes:
        if not sys.stdin or not sys.stdin.isatty():
            raise Abort("확인 입력을 받을 수 없습니다. 내용을 확인했다면 --yes 를 붙여 실행하세요.")
        if input("이대로 진행할까요? 정확히 1건이 제출됩니다. [y/N] ").strip().lower() not in ("y", "yes", "ㅛ"):
            say("취소했습니다. 아무것도 제출하지 않았습니다.")
            return 2

    if code == "OPEN" and remaining_s(sess, target_ms) > 45 and not a.no_backend_pick:
        choose_backend(sess)
        plan.on_page(sess.page())
    say("창을 닫거나 PC를 재우지 말고 그대로 두세요. (중단: Ctrl+C)")

    try:
        countdown(sess, target_ms, pages=3, on_page=plan.on_page, rotate=True)
        if plan.error:
            raise Abort("폼이 바뀌어 준비한 답을 보낼 수 없습니다: %s (아무것도 제출하지 않음)" % plan.error, 4)
        if plan.closed or plan.data is None:
            if a.no_wait_open:
                raise Abort("폼이 닫혀 있습니다. 아무것도 제출하지 않았습니다.", 3)
            shot = wait_open_and_fire(sess, plan, target_ms, shift, a.poll_ms, a.wait_max, a.on_unknown)
        else:
            bd = backend_delay_estimate(sess)
            lo, hi = sess.clock.bounds()
            line = "발사 준비 완료: 시계오차 ±%.1f ms" % ((hi - lo) / 2)
            if a.mode != "fast" and bd is not None:
                line += ", 예상 기록 시각(추정) 목표 %s 부근" % fmt_rel(shift + (hi - lo) / 2 + max(bd, 0.0))
            say(line + " - 이후 결과가 나올 때까지 화면이 조용합니다.")
            shot = timed_send(sess, plan.data, "POST", target_ms, shift, a.on_unknown)
            if shot.outcome == "CLOSED" and not a.no_wait_open:
                say("폼이 닫혀 있어 거절되었습니다 (기록되지 않음). 열릴 때까지 기다립니다.")
                shot = wait_open_and_fire(sess, plan, target_ms, shift, a.poll_ms, a.wait_max, a.on_unknown)
    except KeyboardInterrupt:
        status_end()
        if sess.shot_started:
            say("중단되었습니다. 요청이 이미 나갔을 수 있으니 기록 여부를 주최 측에 확인하세요. 다시 보내지 마세요.")
            return 5
        say("취소했습니다. 아무것도 제출하지 않았습니다.")
        return 2

    say("")
    titles = {"RECORDED": "[성공] 응답이 기록되었습니다.", "RECORDED_PROBABLE": "[성공으로 보임] 확인 페이지를 받았습니다.",
              "CLOSED": "[실패] 폼이 닫혀 있어 기록되지 않았습니다.", "REJECTED": "[실패] 폼이 응답을 거절했습니다 (기록되지 않음).",
              "LOGIN": "[실패] 로그인이 필요한 폼입니다 (기록되지 않음).", "RATE_LIMITED": "[실패] 구글이 요청을 제한했습니다 (기록되지 않음).",
              "NOT_FOUND": "[실패] 폼을 찾을 수 없습니다.", "NOT_SENT": "[실패] 요청을 보내지 못했습니다 (기록되지 않음).",
              "UNKNOWN": "[불명] 기록 여부를 알 수 없습니다. 다시 보내지 말고 주최 측에 확인하세요."}
    say(titles.get(shot.outcome, shot.outcome) + "  " + shot.detail)
    report_shot(shot, "제출 확인 페이지의", cal)
    log = save_log(shot, "fire", {"mode": a.mode, "shift_ms": shift, "form": sess.t.view_url})
    if log:
        say("")
        say("증빙 기록 저장: %s (서버 응답 원문은 같은 이름의 .html)" % log)
    if shot.outcome in ("REJECTED", "LOGIN", "RATE_LIMITED", "NOT_SENT"):
        say("브라우저에서 직접 제출하세요: %s" % sess.t.view_url)
    return EXIT.get(shot.outcome, 5)


# ---- calibration on a form the user owns -------------------------------------------------------
def gviz(sheet_url: str, tq: str, cafile: Optional[str]) -> dict:
    u = urllib.parse.urlsplit(sheet_url if "://" in sheet_url else "https://" + sheet_url)
    m = re.match(r"^/spreadsheets/d/([A-Za-z0-9_-]+)", u.path)
    if not m:
        raise Abort("응답 시트 주소가 아닙니다 (…/spreadsheets/d/<ID>/edit 형태여야 합니다).")
    gid = re.search(r"gid=(\d+)", u.query + "#" + u.fragment)
    query = {"tqx": "out:json", "headers": "1", "tq": tq}
    if gid:
        query["gid"] = gid.group(1)
    path = "/spreadsheets/d/%s/gviz/tq?%s" % (m.group(1), urllib.parse.urlencode(query))
    conn = RawConn(u.scheme, u.hostname, u.port or (443 if u.scheme == "https" else 80), cafile=cafile).connect()
    try:
        r = conn.request("GET", path, [("User-Agent", UA), ("Accept-Encoding", "identity")], timeout=30.0)
    finally:
        conn.close()
    mm = re.search(r"setResponse\((.*)\)\s*;?\s*$", r.text, re.S)
    if r.status != 200 or not mm:
        raise Abort("응답 시트를 읽지 못했습니다 (HTTP %d). 시트를 '링크가 있는 모든 사용자 - 뷰어'로 공유했는지 확인하세요."
                    % r.status)
    data = json.loads(mm.group(1))
    if data.get("status") != "ok":
        raise Abort("응답 시트 조회 오류: %s" % json.dumps(data.get("errors"), ensure_ascii=False))
    return data["table"]


def read_stamps(sheet_url: str, tokens: List[str], cafile: Optional[str]) -> Dict[str, int]:
    """token -> recorded timestamp as milliseconds within the day (sheet time zone), with real milliseconds.
    (The sheet's displayed text and CSV export are rounded to whole seconds; the query functions are not.)"""
    cols = [c["id"] for c in gviz(sheet_url, "select * limit 1", cafile)["cols"]]
    others = [c for c in cols if c != "A"]
    tq = "select hour(A), minute(A), second(A), millisecond(A)%s where A is not null order by A desc limit 200" % (
        "".join(", " + c for c in others))
    out: Dict[str, int] = {}
    for row in gviz(sheet_url, tq, cafile)["rows"]:
        cells = row.get("c") or []
        if len(cells) < 4 or any(c is None or c.get("v") is None for c in cells[:4]):
            continue
        h, mi, s, milli = (int(round(float(c["v"]))) for c in cells[:4])
        text = " ".join(str(c.get("v")) for c in cells[4:] if c and c.get("v") is not None)
        for tok in tokens:
            if tok in text and tok not in out:
                out[tok] = ((h * 60 + mi) * 60 + s) * 1000 + milli
    return out


def mod_gap(stamp_in_day_ms: float, reference_epoch_ms: float) -> float:
    """stamp - reference, where the stamp is only known within its day in an unknown time zone.
    Time zones differ by multiples of 15 minutes, so compare modulo 15 minutes."""
    q = 900_000.0
    return ((stamp_in_day_ms - reference_epoch_ms) % q + q / 2) % q - q / 2


def cmd_calibrate(a) -> int:
    setup_process()
    pairs = [tuple(x.split("=", 1)) for x in a.answer]
    sess = Session(a.url, a.cafile)
    code, detail, form, _r = open_form(sess)
    if form is None:
        raise Abort("연습용 폼을 열 수 없습니다: %s" % detail)
    check_supported(form)
    text_qs = [q for q in form.questions if q.type in (0, 1)]
    if a.token_field:
        token_q = find_question(form, a.token_field)
    elif text_qs:
        token_q = text_qs[0]
    else:
        raise Abort("연습용 폼에 단답형 질문이 하나 필요합니다 (표식 문자열을 넣어 응답을 구분합니다).")
    tok_key = "entry.%d" % token_q.entry.id
    given = [p for p in pairs if find_question(form, p[0]).entry.id != token_q.entry.id]
    asked = ask_answers(form, given + [(tok_key, "x")])        # ask for the other questions only
    pairs = [p for p in asked if find_question(form, p[0]).entry.id != token_q.entry.id]
    say("연습용 폼: %s  (표식을 넣을 질문: %s)" % (form.title, token_q.title))
    say("이 폼에 응답 %d건을 실제로 제출합니다. 반드시 '본인이 만든 연습용 폼'이어야 합니다." % a.n)
    try:
        read_stamps(a.sheet, [], a.cafile)
    except Abort as e:
        raise Abort("%s\n(응답 시트: 폼 편집 화면 > 응답 > Sheets에 연결, 그 시트를 링크 공유)" % e)
    if not a.yes:
        if input("진행할까요? [y/N] ").strip().lower() not in ("y", "yes", "ㅛ"):
            return 2
    run = "%04x" % random.getrandbits(16)
    shots = []
    initial_sync(sess)
    for i in range(a.n):
        token = "cal%s%02d" % (run, i)
        plan = Plan(sess, pairs + [("entry.%d" % token_q.entry.id, token)], a.email)
        plan.on_page(sess.page())
        if plan.error or plan.data is None:
            raise Abort("연습용 폼에 제출할 내용을 만들지 못했습니다: %s" % (plan.error or "폼이 닫혀 있음"))
        target = next_whole_second(sess, 9.0 if i == 0 else 5.0)
        say("보정 %d/%d - 목표 %s, 표식 %s" % (i + 1, a.n, fmt_clock(target), token))
        countdown(sess, target, pages=1, on_page=plan.on_page, rotate=False)
        shot = timed_send(sess, plan.data, "POST", target, a.guard_ms)
        say("    -> %s  (PC 출발 목표 %s, 확인 페이지 %s)" % (
            shot.outcome, fmt_rel(shot.left_pc_lo - target) if shot.t_before_ns else "-",
            fmt_rel(shot.resp.server_time_ms - target) if shot.resp is not None and shot.resp.server_time_ms else "-"))
        if shot.outcome in ("RECORDED", "RECORDED_PROBABLE"):
            shots.append((token, target, shot))
        elif shot.outcome in ("CLOSED", "LOGIN", "RATE_LIMITED", "REJECTED"):
            raise Abort("연습용 폼이 제출을 받지 않았습니다: %s" % shot.detail)
    if not shots:
        raise Abort("기록된 제출이 없습니다.")
    say("응답 시트에 반영되기를 기다립니다 (최대 %d초)..." % a.sheet_wait)
    tokens = [t for t, _T, _s in shots]
    stamps: Dict[str, int] = {}
    end = time.time() + a.sheet_wait
    while time.time() < end:
        stamps = read_stamps(a.sheet, tokens, a.cafile)
        if len(stamps) == len(tokens):
            break
        time.sleep(2.0)
    if not stamps:
        raise Abort("응답 시트에서 방금 제출한 응답을 찾지 못했습니다. 시트가 이 폼에 연결되어 있는지 확인하세요.")
    say("")
    say("  표식        목표 대비 기록 시각   PC 출발→기록 지연   기록→확인페이지")
    lags, gaps, landings = [], [], []
    for token, target, shot in shots:
        if token not in stamps:
            say("  %-10s  (시트에서 찾지 못함)" % token)
            continue
        landing = mod_gap(stamps[token], target)
        lag = mod_gap(stamps[token], shot.left_pc_lo)
        gap = -mod_gap(stamps[token], shot.resp.server_time_ms) if shot.resp.server_time_ms else None
        lags.append(lag)
        landings.append(landing)
        if gap is not None:
            gaps.append(gap)
        say("  %-10s  %-18s  %-18s  %s" % (token, fmt_rel(landing), fmt_rel(lag), fmt_rel(gap) if gap is not None else "-"))
    say("")
    say("기록 시각(목표 대비): 최소 %s / 중간 %s / 최대 %s" % (
        fmt_rel(min(landings)), fmt_rel(statistics.median(landings)), fmt_rel(max(landings))))
    ok = min(lags) >= 0 and min(landings) >= 0
    if not ok:
        warn("목표보다 이르게 기록된 응답이 있습니다. '절대 이르지 않음' 가정이 이 환경에서 성립하지 않습니다. "
             "실제 제출에는 --guard-ms 를 %d 이상으로 키우세요." % (int(-min(landings)) + 30))
    else:
        say("safe 모드는 이 환경에서 한 번도 목표보다 이르지 않았습니다.")
    profile = {
        "version": 1, "created": _dt.datetime.now().astimezone().isoformat(), "n": len(lags),
        "lag_min_ms": round(min(lags), 1), "lag_median_ms": round(statistics.median(lags), 1),
        "lag_max_ms": round(max(lags), 1),
        "conf_gap_min_ms": round(min(gaps), 1) if gaps else None,
        "conf_gap_median_ms": round(statistics.median(gaps), 1) if gaps else None,
        "guard_ms": a.guard_ms,
    }
    path = a.calibration or os.path.join(os.path.dirname(os.path.abspath(__file__)), CALIBRATION_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=1)
    say("보정 자료 저장: %s" % path)
    say("  PC 출발→기록 최소 지연 %.0f ms. fast 모드는 이 값에서 여유(기본 30 ms)를 뺀 만큼 일찍 보냅니다." % profile["lag_min_ms"])
    return 0 if ok else 5


def cmd_selftest(_a) -> int:
    """Local timer quality: how late does this PC wake up, and how exact is the busy-wait?"""
    setup_process()
    say("이 PC의 타이머 정밀도를 확인합니다 (약 5초)...")
    late_sleep, late_spin = [], []
    for _ in range(40):
        t = now_ns() + 20_000_000
        time.sleep(0.02)
        late_sleep.append(ms(now_ns() - t))
    for _ in range(60):
        t = now_ns() + 60_000_000
        wait_until(t)
        late_spin.append(ms(now_ns() - t))
    late_spin.sort()
    say("  시간 기준               : %s" % CLOCK_NAME)
    say("  sleep 만 썼을 때 늦음    : 중간 %.3f ms / 최대 %.3f ms" % (statistics.median(late_sleep), max(late_sleep)))
    say("  도구 방식(대기+스핀) 늦음 : 중간 %.4f ms / 최대 %.3f ms" % (statistics.median(late_spin), late_spin[-1]))
    ok = statistics.median(late_spin) < 0.2 and late_spin[-1] < 20
    say("  판정: %s" % ("정상" if ok else "이 PC는 타이밍이 불안정합니다. 다른 프로그램을 닫고 전원을 연결한 뒤 다시 확인하세요."))
    return 0 if ok else 5


# ------------------------------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="gform_ontime.py", description="구글 폼 응답을 구글 서버 시계 기준 정각에 1건 제출합니다.")
    ap.add_argument("--cafile", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("inspect", help="폼의 질문/보기/상태를 보여 줍니다 (읽기 전용)")
    p.add_argument("url")
    p.set_defaults(fn=cmd_inspect)

    p = sub.add_parser("clock", help="구글 서버 시계와 이 PC 시계의 차이를 측정합니다 (읽기 전용)")
    p.add_argument("url")
    p.add_argument("--seconds", type=int, default=20)
    p.set_defaults(fn=cmd_clock)

    p = sub.add_parser("rehearse", help="제출 없이 전 과정을 연습합니다: 발사 순간에 읽기 요청을 보내 어디에 떨어지는지 봅니다")
    p.add_argument("url")
    p.add_argument("--at", help='목표 시각 "YYYY-MM-DD HH:MM:SS" (생략하면 약 --lead 초 뒤의 정각 초)')
    p.add_argument("--utc-offset")
    p.add_argument("--lead", type=float, default=15.0, help="--at 생략 시 몇 초 뒤를 목표로 할지 (기본 15)")
    p.add_argument("--count", type=int, default=3, help="반복 횟수 (기본 3)")
    p.add_argument("--guard-ms", type=float, default=10.0)
    p.set_defaults(fn=cmd_rehearse)

    p = sub.add_parser("fire", help="목표 시각에 응답 1건을 제출합니다")
    p.add_argument("url")
    p.add_argument("--at", required=True, help='목표 시각 "YYYY-MM-DD HH:MM:SS" (24시간제, 이 PC의 시간대)')
    p.add_argument("--utc-offset", help="--at 의 시간대를 직접 지정 (예: +09:00)")
    p.add_argument("--answer", action="append", default=[], metavar='"질문=값"', help="질문 제목(또는 entry 번호)과 답. 여러 번 사용")
    p.add_argument("--email", help="이메일 주소를 입력받는 폼일 때")
    p.add_argument("--mode", choices=("safe", "fast"), default="safe")
    p.add_argument("--guard-ms", type=float, default=10.0, help="safe: 목표 뒤로 두는 여유 (기본 10)")
    p.add_argument("--margin-ms", type=float, default=None, help="fast: 최소 지연에서 남기는 여유 (기본 30)")
    p.add_argument("--lead-ms", type=float, default=None, help="fast: 보정 자료 대신 직접 지정하는 최소 지연")
    p.add_argument("--calibration", help="보정 자료 파일 경로")
    p.add_argument("--no-calibration", action="store_true")
    p.add_argument("--no-wait-open", action="store_true", help="폼이 닫혀 있으면 기다리지 않고 종료")
    p.add_argument("--poll-ms", type=float, default=300.0, help="닫힌 폼이 열렸는지 확인하는 간격 (기본 300, 최소 200)")
    p.add_argument("--wait-max", type=float, default=600.0, help="목표 시각 이후 폼이 열리기를 기다리는 최대 초")
    p.add_argument("--on-unknown", choices=("stop", "retry"), default="stop",
                   help="응답을 못 받았을 때: stop=재전송 안 함(기본), retry=중복 위험을 감수하고 1회 재전송")
    p.add_argument("--no-backend-pick", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--yes", action="store_true", help="확인 질문 없이 진행")
    p.set_defaults(fn=cmd_fire)

    p = sub.add_parser("calibrate", help="본인 연습용 폼에 여러 번 제출해 실제 기록 시각을 측정합니다")
    p.add_argument("url")
    p.add_argument("--sheet", required=True, help="연습용 폼의 응답 시트 주소 (링크 공유: 뷰어)")
    p.add_argument("--answer", action="append", default=[], metavar='"질문=값"')
    p.add_argument("--email")
    p.add_argument("--token-field", help="표식을 넣을 단답형 질문 (기본: 첫 단답형)")
    p.add_argument("-n", type=int, default=6, help="제출 횟수 (기본 6)")
    p.add_argument("--guard-ms", type=float, default=10.0)
    p.add_argument("--sheet-wait", type=int, default=90)
    p.add_argument("--calibration", help="보정 자료를 저장할 경로")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_calibrate)

    p = sub.add_parser("selftest", help="이 PC의 타이머 정밀도 점검")
    p.set_defaults(fn=cmd_selftest)

    a = ap.parse_args(argv)
    if getattr(a, "poll_ms", 300.0) < 200.0:
        a.poll_ms = 200.0
    try:
        return a.fn(a)
    except Abort as e:
        status_end()
        say("[중단] %s" % e)
        return e.code
    except NetError as e:
        status_end()
        say("[네트워크 오류] %s" % e)
        return 5
    except KeyboardInterrupt:
        status_end()
        say("취소했습니다.")
        return 2
    finally:
        while _sessions:
            _sessions.pop().close()
        while _held_locks:                       # closing the file releases the lock
            try:
                _held_locks.pop().close()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
