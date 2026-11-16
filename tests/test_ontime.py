"""Tests for gform_ontime.py. Everything runs against the local mock (tests/mock_forms_server.py);
nothing here talks to Google.

    python -m unittest discover -s tests -v          (about 3 minutes: each timed shot needs ~10 s)
    GFORM_SLOW_TESTS=1 python -m unittest ...        (adds the full 100-second countdown)
"""
import contextlib
import datetime as dt
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import gform_ontime as g                                                    # noqa: E402
from mock_forms_server import ENTRY_GRADE, ENTRY_NAME, MockFormsServer      # noqa: E402

GRADE = "entry.%d" % ENTRY_GRADE
NAME = "entry.%d" % ENTRY_NAME


def at(server_ms):
    return dt.datetime.fromtimestamp(server_ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + "+00:00"


def run(argv):
    """Run the CLI in-process; returns (exit code, printed text)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = g.main(argv)
    return code, buf.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)                 # evidence logs land here

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def target(self, mock, lead_s=11.0):
        return math.ceil((mock.state.now_ms() + lead_s * 1000) / 1000) * 1000

    def fire(self, mock, T, *extra, answers=("학년=2", "이름=홍길동")):
        argv = ["fire", mock.form_url, "--at", at(T), "--yes", "--no-calibration"]
        for a in answers:
            argv += ["--answer", a]
        return run(argv + list(extra))

    def assert_no_probe_of_form_response(self, mock):
        self.assertEqual(mock.state.violations, [], "the tool sent a GET/HEAD to /formResponse")
        posts = [r for r in mock.state.requests if r[0] == "POST"]
        self.assertEqual(len(posts), len(mock.state.responses))


# ------------------------------------------------------------------------------------------------
class Units(unittest.TestCase):
    def page(self):
        with MockFormsServer() as m:
            s = g.Session(m.form_url)
            try:
                return s.page().text
            finally:
                s.close()

    def test_parse_form_and_payload(self):
        form = g.parse_form(self.page())
        self.assertEqual([q.title for q in form.questions], ["학년", "이름"])
        self.assertEqual(form.questions[0].entry.options, ["1", "2", "3"])
        self.assertFalse(form.signin_required)
        answers = g.normalize_answers(form, [("학년", "2"), (NAME, "홍 길동&a=b+c")])
        fields = g.build_fields(form, answers)
        keys = [k for k, _ in fields]
        self.assertEqual(keys[:3], [GRADE, GRADE + "_sentinel", NAME])
        self.assertEqual(keys[3:], ["fvv", "partialResponse", "pageHistory", "fbzx", "submissionTimestamp"])
        self.assertEqual(dict(fields)["submissionTimestamp"], "-1")          # passed through untouched
        body = g.urllib.parse.urlencode(fields)
        self.assertEqual(g.urllib.parse.parse_qs(body, keep_blank_values=True)[NAME], ["홍 길동&a=b+c"])

    def test_answers_are_validated_locally(self):
        form = g.parse_form(self.page())
        with self.assertRaises(g.Abort):
            g.normalize_answers(form, [("학년", "9")])            # not an option
        with self.assertRaises(g.Abort):
            g.normalize_answers(form, [("없는질문", "1")])
        with self.assertRaises(g.Abort):
            g.normalize_answers(form, [("학년", "1"), (GRADE, "2")])   # answered twice

    def test_parse_at(self):
        self.assertEqual(g.parse_at("2026-11-23 22:00:00+09:00", None), 1795438800000.0)
        self.assertEqual(g.parse_at("2026-11-23T13:00:00Z", None), 1795438800000.0)
        self.assertEqual(g.parse_at("2026-11-23 22:00:00.250", "+09:00"), 1795438800250.0)
        self.assertEqual(g.parse_at("2026-11-23 22:00", "+0900"), 1795438800000.0)
        for bad in ("22:00:00", "10시", "2026-11-23", "11/23 22:00", "2026-13-01 10:00:00"):
            with self.assertRaises(g.Abort, msg=bad):
                g.parse_at(bad, None)

    def test_clock_bounds_and_ageing(self):
        c = g.ServerClock()
        t = g.now_ns()
        self.assertTrue(c.add(100.0, 160.0, t, "fe"))
        self.assertTrue(c.add(120.0, 300.0, t, "page"))
        lo, hi = c.bounds(t)
        self.assertEqual((lo, hi), (120.0, 160.0))
        lo2, hi2 = c.bounds(t + 10_000_000_000)                    # 10 s later: 5 ms wider on each side
        self.assertAlmostEqual(lo2, 115.0)
        self.assertAlmostEqual(hi2, 165.0)

    def test_clock_contradiction_keeps_newest(self):
        c = g.ServerClock()
        t = g.now_ns()
        c.add(100.0, 160.0, t, "fe")
        self.assertFalse(c.add(500.0, 560.0, t + 1, "fe"))         # the counter jumped (PC slept)
        self.assertEqual(c.bounds(t + 1), (500.0, 560.0))
        self.assertEqual(c.resets, 1)

    def test_clock_drops_front_end_when_it_disagrees_with_forms_backend(self):
        c = g.ServerClock()
        t = g.now_ns()
        c.add(100.0, 110.0, t, "fe")
        self.assertFalse(c.add(200.0, 260.0, t + 1, "page"))       # first disagreement: newest wins
        self.assertFalse(c.add(100.0, 110.0, t + 2, "fe"))         # second: stop trusting the front end
        self.assertFalse(c.trust_frontend)
        c.add(205.0, 250.0, t + 3, "page")
        c.add(100.0, 110.0, t + 4, "fe")                           # ignored from now on
        self.assertEqual(c.bounds(t + 4)[0], 205.0 - 1e-9 * 0.5)

    def test_proxy_hint_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write("Outbound HTTPS goes through a local proxy at http://127.0.0.1:45695\n(set via HTTPS_PROXY)\n")
        old = os.environ.get("GFORM_PROXY_HINT_FILE")
        try:
            os.environ["GFORM_PROXY_HINT_FILE"] = f.name
            self.assertEqual(g._proxy_hint(), "http://127.0.0.1:45695")
            os.environ["GFORM_PROXY_HINT_FILE"] = f.name + ".missing"
            self.assertIsNone(g._proxy_hint())
            del os.environ["GFORM_PROXY_HINT_FILE"]
            self.assertIsNone(g._proxy_hint())
        finally:
            os.unlink(f.name)
            if old is not None:
                os.environ["GFORM_PROXY_HINT_FILE"] = old

    def test_mod_gap_ignores_the_sheet_time_zone(self):
        ref = 1795438800000.0                                       # 2026-11-23 13:00:00 UTC
        kst_in_day = (22 * 3600) * 1000 + 73                        # 22:00:00.073 in a KST sheet
        self.assertAlmostEqual(g.mod_gap(kst_in_day, ref), 73.0)
        ist_in_day = (18 * 3600 + 30 * 60) * 1000 + 73              # same instant in a +05:30 sheet
        self.assertAlmostEqual(g.mod_gap(ist_in_day, ref), 73.0)
        self.assertAlmostEqual(g.mod_gap(kst_in_day - 100, ref), -27.0)

    def test_classify(self):
        def resp(status, body="", **headers):
            return g.Resp(status, headers, [], body.encode(), 0, 0, 0, None, None, None, True)
        form_page = '<input type="hidden" name="fbzx" value="1"> FB_PUBLIC_LOAD_DATA_'
        self.assertEqual(g.classify(resp(200, "응답이 기록되었습니다."), True)[0], "RECORDED")
        # Google's real confirmation page (2026-11-16) embeds an empty FB_PUBLIC_LOAD_DATA_; it is still a success
        real_confirm = ('<title>11/23 치킨데이 알바</title> 응답이 기록되었습니다. '
                        '<a href="https://docs.google.com/forms/d/e/x/viewform?usp=form_confirm">다른 응답 제출</a>'
                        '<script>var FB_PUBLIC_LOAD_DATA_ = [null,[],"/forms","11/23 치킨데이 알바",null,null,null,"",'
                        'null,0,0,null,"",0,"e/x",0,"[]",0,0,1];</script>')
        self.assertEqual(g.classify(resp(200, real_confirm), True)[0], "RECORDED")
        self.assertEqual(g.classify(resp(200, "감사합니다!"), True)[0], "RECORDED_PROBABLE")
        self.assertEqual(g.classify(resp(200, form_page), True)[0], "REJECTED")
        self.assertEqual(g.classify(resp(200, form_page), False)[0], "OPEN")
        self.assertEqual(g.classify(resp(400, form_page), True)[0], "REJECTED")
        self.assertEqual(g.classify(resp(302, location="/forms/d/e/x/closedform"), True)[0], "CLOSED")
        self.assertEqual(g.classify(resp(200, "양식에서 더 이상 응답을 받지 않습니다."), True)[0], "CLOSED")
        self.assertEqual(g.classify(resp(302, location="https://accounts.google.com/ServiceLogin"), True)[0], "LOGIN")
        self.assertEqual(g.classify(resp(429), True)[0], "RATE_LIMITED")
        self.assertEqual(g.classify(resp(302, location="https://www.google.com/sorry/index"), True)[0], "RATE_LIMITED")
        self.assertEqual(g.classify(resp(503), True)[0], "UNKNOWN")


# ------------------------------------------------------------------------------------------------
class TimedShots(Base):
    CONFIGS = [
        dict(skew_ms=1234.5, up_ms=4, down_ms=4, jitter_ms=2, backend_ms=30, backend_jitter_ms=15, proc_ms=10,
             date_lag_ms=4),
        dict(skew_ms=-86_400_000 - 8765.4, up_ms=1, down_ms=12, jitter_ms=6, backend_ms=5, backend_jitter_ms=2,
             proc_ms=0, date_lag_ms=8),                              # clock a day off, asymmetric path, fast backend
        dict(skew_ms=3_600_000 + 0.7, up_ms=15, down_ms=1, jitter_ms=8, backend_ms=0, backend_jitter_ms=0,
             proc_ms=0, date_lag_ms=0),                              # worst case for "early": no server-side delay
    ]

    def test_safe_mode_is_never_early_and_submits_exactly_once(self):
        for cfg in self.CONFIGS:
            with self.subTest(cfg=cfg), MockFormsServer(**cfg) as m:
                T = self.target(m)
                code, out = self.fire(m, T, answers=("학년=2", "이름=홍 길동&테스트+1"))
                self.assertEqual(code, 0, out)
                self.assertEqual(len(m.state.responses), 1, out)
                r = m.state.responses[0]
                self.assertGreaterEqual(r["arrival_ms"], T, out)        # even the arrival is not early
                self.assertGreaterEqual(r["stamp_ms"], T, out)
                self.assertLess(r["stamp_ms"] - T, 400, out)            # and it is not needlessly late
                self.assertEqual(r["fields"][GRADE], ["2"])
                self.assertEqual(r["fields"][NAME], ["홍 길동&테스트+1"])
                self.assertEqual(r["fields"]["submissionTimestamp"], ["-1"])
                self.assertIn("COMPASS=", r["cookie"])                  # backend affinity cookie is replayed
                self.assertNotIn("NID=", r["cookie"])
                self.assertEqual(r["headers"]["content-type"], "application/x-www-form-urlencoded")
                self.assert_no_probe_of_form_response(m)
                self.assertTrue([f for f in os.listdir(".") if f.endswith(".json")], "no evidence log written")

    def test_date_header_only_when_pages_carry_no_millisecond_clock(self):
        with MockFormsServer(skew_ms=-555.5, up_ms=5, down_ms=5, jitter_ms=3, backend_ms=10, proc_ms=0,
                             expose_server_time=False) as m:
            T = self.target(m)
            code, out = self.fire(m, T)
            self.assertEqual(code, 0, out)
            self.assertEqual(len(m.state.responses), 1)
            self.assertGreaterEqual(m.state.responses[0]["arrival_ms"], T, out)

    def test_target_off_the_whole_second(self):
        with MockFormsServer(skew_ms=42.0, up_ms=3, down_ms=3, jitter_ms=1, backend_ms=0, proc_ms=0) as m:
            T = self.target(m) + 250
            code, out = run(["fire", m.form_url, "--at", at(T)[:19] + ".250+00:00", "--yes", "--no-calibration",
                             "--answer", "학년=1", "--answer", "이름=가"])
            self.assertEqual(code, 0, out)
            self.assertGreaterEqual(m.state.responses[0]["arrival_ms"], T, out)
            self.assertLess(m.state.responses[0]["arrival_ms"] - T, 150, out)

    def test_rehearsal_submits_nothing(self):
        with MockFormsServer(skew_ms=999.0, up_ms=3, down_ms=3, jitter_ms=1, backend_ms=20, proc_ms=5) as m:
            code, out = run(["rehearse", m.form_url, "--count", "1", "--lead", "6"])
            self.assertEqual(code, 0, out)
            self.assertEqual(m.state.responses, [])
            self.assertFalse([r for r in m.state.requests if r[0] == "POST"])
            self.assertEqual(m.state.violations, [])
            self.assertIn("제출하지 않습니다", out)

    def test_dead_connection_is_replaced_before_the_shot(self):
        with MockFormsServer(skew_ms=10.0, up_ms=2, down_ms=2, backend_ms=5) as m:
            sess = g.Session(m.form_url)
            form = g.parse_form(sess.page().text)
            g.initial_sync(sess)
            T = self.target(m, 5.0)
            g._quiet = True
            try:
                g.countdown(sess, T, pages=0, rotate=False, show=False)
                data = g.make_post(sess, form, g.build_fields(form, g.normalize_answers(form, [("이름", "끊김")])))
                sess.conn.sock.close()                                   # the peer "hung up" on us
                shot = g.timed_send(sess, data, "POST", T, 10.0)
            finally:
                g._quiet = False
                sess.close()
            self.assertEqual(shot.outcome, "RECORDED", shot.detail)
            self.assertEqual(len(m.state.responses), 1)
            self.assertGreaterEqual(m.state.responses[0]["arrival_ms"], T)


class SingleInstance(Base):
    def test_second_fire_for_the_same_form_refuses_to_run(self):
        with MockFormsServer(skew_ms=321.0, up_ms=3, down_ms=3, jitter_ms=1, backend_ms=10, proc_ms=0) as m:
            T = self.target(m, 12.0)
            tool = os.path.join(os.path.dirname(HERE), "gform_ontime.py")
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            base = [sys.executable, tool, "fire", m.form_url, "--at", at(T), "--yes", "--no-calibration",
                    "--answer", "학년=2"]
            first = subprocess.Popen(base + ["--answer", "이름=첫번째"], stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, env=env)
            try:
                time.sleep(2.0)
                second = subprocess.run(base + ["--answer", "이름=두번째"], capture_output=True, env=env, timeout=60)
                out1, _ = first.communicate(timeout=90)
            finally:
                if first.poll() is None:
                    first.kill()
            out2 = second.stdout.decode("utf-8", "replace")
            self.assertEqual(second.returncode, 2, out2)
            self.assertIn("이미 실행 중", out2)
            self.assertEqual(first.returncode, 0, out1.decode("utf-8", "replace"))
            self.assertEqual(len(m.state.responses), 1)
            self.assertEqual(m.state.responses[0]["fields"][NAME], ["첫번째"])
            self.assert_no_probe_of_form_response(m)

    def test_lock_is_released_when_the_process_ends(self):
        with MockFormsServer() as m:
            target = g.parse_target(m.form_url)
            code = ("import sys; sys.path.insert(0, %r); import gform_ontime as g; "
                    "g.single_instance(g.parse_target(%r)); print('locked')" % (os.path.dirname(HERE), m.form_url))
            for _ in range(2):                                    # the second run must get the lock again
                r = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=30)
                self.assertIn(b"locked", r.stdout, r.stderr)
            self.assertTrue(target.base)


class Refusals(Base):
    def test_bad_option_is_refused_before_anything_is_sent(self):
        with MockFormsServer() as m:
            code, out = self.fire(m, self.target(m), answers=("학년=9", "이름=가"))
            self.assertEqual(code, 2, out)
            self.assertEqual(m.state.responses, [])
            self.assert_no_probe_of_form_response(m)

    def test_all_empty_response_is_refused(self):
        with MockFormsServer() as m:
            code, out = self.fire(m, self.target(m), answers=())
            self.assertEqual(code, 2, out)
            self.assertEqual(m.state.responses, [])

    def test_past_or_ambiguous_target_is_refused(self):
        with MockFormsServer() as m:
            code, out = self.fire(m, m.state.now_ms() - 60_000)
            self.assertEqual(code, 2, out)
            code, out = run(["fire", m.form_url, "--at", "22:00:00", "--yes", "--answer", "이름=가"])
            self.assertEqual(code, 2, out)
            self.assertEqual(m.state.responses, [])

    def test_rate_limited_aborts_without_posting(self):
        with MockFormsServer(rate_limited=True) as m:
            code, out = self.fire(m, self.target(m))
            self.assertNotEqual(code, 0, out)
            self.assertEqual(m.state.responses, [])
            self.assertFalse([r for r in m.state.requests if r[0] == "POST"])

    def test_fast_mode_needs_calibration(self):
        with MockFormsServer() as m:
            code, out = self.fire(m, self.target(m), "--mode", "fast")
            self.assertEqual(code, 2, out)
            self.assertEqual(m.state.responses, [])


class ClosedForms(Base):
    def test_closed_form_is_submitted_right_after_it_opens(self):
        with MockFormsServer(skew_ms=-2500.0, up_ms=4, down_ms=4, jitter_ms=2, backend_ms=15, proc_ms=5) as m:
            T = self.target(m, 11.0)
            m.state.open_at_server_ms = T + 1300                      # the organiser opens it 1.3 s late
            code, out = self.fire(m, T, "--poll-ms", "200")
            self.assertEqual(code, 0, out)
            self.assertEqual(len(m.state.responses), 1, out)
            lag = m.state.responses[0]["stamp_ms"] - m.state.open_at_server_ms
            self.assertGreaterEqual(lag, 0)
            self.assertLess(lag, 700, out)
            self.assert_no_probe_of_form_response(m)

    def test_form_that_opens_before_the_target_is_still_held_until_the_target(self):
        with MockFormsServer(skew_ms=777.0, up_ms=4, down_ms=4, jitter_ms=2, backend_ms=10, proc_ms=0) as m:
            T = self.target(m, 12.0)
            m.state.open_at_server_ms = T - 5000
            code, out = self.fire(m, T)
            self.assertEqual(code, 0, out)
            self.assertEqual(len(m.state.responses), 1, out)
            self.assertGreaterEqual(m.state.responses[0]["arrival_ms"], T, out)
            self.assertLess(m.state.responses[0]["stamp_ms"] - T, 400, out)

    def test_no_wait_open(self):
        with MockFormsServer() as m:
            m.state.open_at_server_ms = m.state.now_ms() + 3_600_000
            code, out = self.fire(m, self.target(m), "--no-wait-open")
            self.assertEqual(code, 3, out)
            self.assertEqual(m.state.responses, [])


class UnknownOutcome(Base):
    def test_no_answer_after_sending_is_never_resent_by_default(self):
        with MockFormsServer(up_ms=2, down_ms=2, drop_post_response=True) as m:
            code, out = self.fire(m, self.target(m))
            self.assertEqual(code, 5, out)
            self.assertEqual(len(m.state.responses), 1, "must not resend on an unknown outcome")
            self.assertIn("다시 보내지", out)

    def test_retry_is_only_done_on_explicit_request(self):
        with MockFormsServer(up_ms=2, down_ms=2, drop_post_response=True) as m:
            code, out = self.fire(m, self.target(m), "--on-unknown", "retry")
            self.assertEqual(len(m.state.responses), 2, out)          # the documented price of 'retry'


class Calibration(Base):
    def test_calibrate_then_fast_mode(self):
        with MockFormsServer(skew_ms=123456.0, up_ms=4, down_ms=4, jitter_ms=1, backend_ms=25, backend_jitter_ms=4,
                             proc_ms=15, date_lag_ms=3) as m:
            cal = os.path.join(self._tmp.name, "cal.json")
            code, out = run(["calibrate", m.form_url, "--sheet", m.sheet_url, "--answer", "학년=1", "-n", "2",
                             "--yes", "--calibration", cal, "--sheet-wait", "20"])
            self.assertEqual(code, 0, out)
            self.assertEqual(len(m.state.responses), 2)
            prof = json.load(open(cal, encoding="utf-8"))
            self.assertGreater(prof["lag_min_ms"], 30)                 # up + backend + proc is about 45 ms here
            self.assertLess(prof["lag_min_ms"], 90)
            T = self.target(m)
            code, out = run(["fire", m.form_url, "--at", at(T), "--yes", "--mode", "fast", "--calibration", cal,
                             "--answer", "학년=2", "--answer", "이름=빠름"])
            self.assertEqual(code, 0, out)
            stamp = m.state.responses[-1]["stamp_ms"]
            self.assertGreaterEqual(stamp, T, out)
            self.assertLess(stamp - T, 80, out)                        # aimed at T + 30 ms margin


@unittest.skipUnless(os.environ.get("GFORM_SLOW_TESTS"), "set GFORM_SLOW_TESTS=1 for the full-length countdown")
class FullCountdown(Base):
    def test_full_timeline_with_backend_pick(self):
        with MockFormsServer(skew_ms=-31337.0, up_ms=5, down_ms=5, jitter_ms=3, backend_ms=20, backend_jitter_ms=10,
                             proc_ms=10, date_lag_ms=4, backend_modes=(80.0, 0.0, 40.0)) as m:
            T = self.target(m, 100.0)
            code, out = self.fire(m, T)
            self.assertEqual(code, 0, out)
            self.assertEqual(len(m.state.responses), 1)
            r = m.state.responses[0]
            self.assertGreaterEqual(r["arrival_ms"], T, out)
            self.assertIn("COMPASS=b1", r["cookie"], "the fastest backend should have been kept")
            self.assert_no_probe_of_form_response(m)


if __name__ == "__main__":
    unittest.main(verbosity=2)
