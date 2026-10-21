import json
import struct
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from sandbox_bench import transport
except ImportError:
    transport = None


def frame(value, flag=0):
    raw = json.dumps(value).encode()
    return bytes([flag]) + struct.pack(">I", len(raw)) + raw


class ConnectTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(transport, "transport implementation is missing")

    def test_split_utf8_and_omitted_proto_zero_exit(self):
        stream = (frame({"event": {"start": {"pid": 1}}})
                  + frame({"event": {"data": {"stdout": "4g=="}}})
                  + frame({"event": {"data": {"stdout": "nJM="}}})
                  + frame({"event": {"data": {"stderr": "ZXJy"}}})
                  + frame({"event": {"end": {"exited": True}}}) + frame({}, 2))
        self.assertEqual(transport.parse_connect(stream),
                         {"stdout": "✓", "stderr": "err", "exit_code": 0})

    def test_nonzero_exit_is_a_result(self):
        stream = frame({"event": {"end": {"exitCode": 7, "exited": True}}}) + frame({}, 2)
        self.assertEqual(transport.parse_connect(stream)["exit_code"], 7)

    def test_rejects_incomplete_compressed_and_error_streams(self):
        end = frame({"event": {"end": {"exitCode": 0, "exited": True}}})
        cases = [b"", b"\x00\x00", end, frame({}, 2), frame({}, 1),
                 end + frame({"error": {"message": "SECRET"}}, 2),
                 frame({"event": {"end": {"error": "SECRET", "exited": True}}}) + frame({}, 2),
                 frame({"event": {"end": {"exited": False}}}) + frame({}, 2),
                 frame({"event": {"data": {"stdout": "!!!"}}}) + end + frame({}, 2),
                 end + frame({}, 2) + end,
                 frame({"event": {"end": {"exitCode": "bad", "exited": True}}}) + frame({}, 2),
                 frame({"event": {"end": {"exitCode": True, "exited": True}}}) + frame({}, 2),
                 b"\x00\x00\x00\x00\x02{}" + frame({}, 2)]
        for raw in cases:
            with self.subTest(raw=raw[:8]):
                with self.assertRaises(transport.TransportError) as caught:
                    transport.parse_connect(raw)
                self.assertNotIn("SECRET", str(caught.exception))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/redirect":
            self.server.target_hits = 0
            self.send_response(302)
            self.send_header("Location", "/target")
            self.end_headers()
        elif self.path == "/target":
            self.server.target_hits += 1
            self.send_response(200)
            self.end_headers()
        elif self.path == "/error":
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"SECRET")
        elif self.path == "/slow":
            self.send_response(200)
            self.end_headers()
            try:
                for _ in range(30):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.025)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_response(200)
            self.send_header("X-Next-Token", "page2")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.assertIsNotNone(transport, "transport implementation is missing")

    def test_response_records_header_and_complete_times(self):
        result = transport.request("GET", self.url, timeout=1)
        self.assertEqual(result.body, b'{"ok":true}')
        self.assertEqual(result.headers["x-next-token"], "page2")
        self.assertGreaterEqual(result.complete_s, result.headers_s)
        self.assertGreaterEqual(result.headers_s, 0)

    def test_redirect_does_not_forward_credentials(self):
        with self.assertRaises(transport.TransportError):
            transport.request("GET", self.url + "/redirect",
                              headers={"X-API-Key": "SECRET"}, timeout=1)
        self.assertEqual(self.server.target_hits, 0)

    def test_error_text_does_not_include_body_or_credentials(self):
        with self.assertRaises(transport.TransportError) as caught:
            transport.request("GET", self.url + "/error",
                              headers={"X-API-Key": "SECRET"}, timeout=1)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(caught.exception.status, 403)

    def test_slow_drip_cannot_reset_total_deadline(self):
        started = time.monotonic()
        with self.assertRaises(transport.TransportError):
            transport.request("GET", self.url + "/slow", timeout=0.12)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_invalid_deadline_and_credential_url_fail_before_network(self):
        for url, timeout in [(self.url, 0), (self.url, float("nan")),
                             ("http://user:SECRET@localhost/", 1)]:
            with self.assertRaises(transport.TransportError) as caught:
                transport.request("GET", url, timeout=timeout)
            self.assertNotIn("SECRET", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
