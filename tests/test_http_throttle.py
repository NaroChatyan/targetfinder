"""Per-host politeness: one request in flight per host, consecutive requests ≥ HOST_DELAY apart, different hosts independent."""
import io, threading, time, unittest, urllib.request
from tests import helpers  # noqa: F401  (sys.path)
from targetfinder import http, config


class FakeResponse(io.BytesIO):
    status = 200
    headers = {}
    def __enter__(self): return self
    def __exit__(self, *a): return False


class Throttle(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.lock = threading.Lock()
        def fake_urlopen(req, timeout=None):
            with self.lock:
                self.calls.append((req.full_url, time.monotonic()))
            time.sleep(0.05)   # a request takes time; the next one to the same host must still wait the full delay
            return FakeResponse(b"{}")
        self._orig = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        self._delay = dict(config.HOST_DELAY)
        config.HOST_DELAY["default"] = 0.4
        http.reset_throttle()

    def tearDown(self):
        urllib.request.urlopen = self._orig
        config.HOST_DELAY.clear(); config.HOST_DELAY.update(self._delay)
        http.reset_throttle()

    def _spawn(self, urls):
        ts = [threading.Thread(target=http.fetch, args=(u,)) for u in urls]
        for t in ts: t.start()
        for t in ts: t.join()

    def test_two_requests_same_host_are_spaced(self):
        self._spawn(["https://x.test/a", "https://x.test/b"])
        self.assertEqual(len(self.calls), 2)
        gap = abs(self.calls[1][1] - self.calls[0][1])
        self.assertGreaterEqual(gap, config.HOST_DELAY["default"] - 0.01, f"requests only {gap:.3f}s apart")

    def test_different_hosts_not_serialized(self):
        self._spawn(["https://x.test/a", "https://y.test/b"])
        gap = abs(self.calls[1][1] - self.calls[0][1])
        self.assertLess(gap, config.HOST_DELAY["default"] / 2, f"different hosts serialized: {gap:.3f}s")

    def test_user_agent_identifies_project(self):
        self.assertTrue(config.USER_AGENT.startswith("targetfinder/"), config.USER_AGENT)
        self.assertIn("(+http", config.USER_AGENT)

    def test_delays_are_conservative(self):
        for host, d in self._delay.items():   # the configured table, not the one this test lowered in setUp
            self.assertGreaterEqual(d, 1.0, host)
        self.assertGreaterEqual(self._delay["hackerone.com"], 1.5)
        self.assertLessEqual(config.REPO_POLL_WORKERS, 4)


if __name__ == "__main__":
    unittest.main()
