"""Loopback tests for the controlled OTA HTTP test service."""

import http.client
import json
import socket
import threading
import unittest

from scripts.ota_test_server import OtaTestServer


class OtaTestServerTests(unittest.TestCase):
    def setUp(self):
        self.server = OtaTestServer(
            ("127.0.0.1", 0),
            mode="new",
            current_version="2.6.0",
            offered_version="99.0.0",
            base_url="http://192.168.1.20:8765",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        self.addCleanup(conn.close)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.getheader("Content-Type"), response.read()

    def test_new_mode_uses_offered_version_and_explicit_base_url(self):
        status, content_type, body = self.request(
            "POST", "/ota/", json.dumps({"device": "not-logged"}),
            {"Content-Length": str(len(b'{"device": "not-logged"}')),
             "Host": "attacker.invalid"},
        )
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        self.assertEqual(payload["firmware"]["version"], "99.0.0")
        self.assertEqual(payload["firmware"]["url"],
                         "http://192.168.1.20:8765/firmware.bin")
        self.assertNotIn("force", payload["firmware"])
        self.assertIsInstance(payload["server_time"]["timestamp"], int)
        self.assertGreater(payload["server_time"]["timestamp"], 1_000_000_000_000)
        self.assertNotIn("device", body.decode())

    def test_forced_and_none_modes(self):
        for mode in ("forced", "none"):
            with self.subTest(mode=mode):
                self.server.mode = mode
                status, _, body = self.request("POST", "/ota/v1/", b"{}",
                                                {"Content-Length": "2"})
                firmware = json.loads(body)["firmware"]
                self.assertEqual(status, 200)
                self.assertEqual(firmware["version"], "2.6.0")
                if mode == "forced":
                    self.assertEqual(firmware["force"], 1)
                else:
                    self.assertNotIn("force", firmware)

    def test_stats_and_gone_firmware_track_requests(self):
        self.request("POST", "/ota/", b"{}", {"Content-Length": "2"})
        status, _, _ = self.request("GET", "/firmware.bin")
        self.assertEqual(status, 410)
        status, _, body = self.request("GET", "/stats")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {
            "ota_requests": 1, "download_requests": 1, "mode": "new"})

    def test_rejects_invalid_content_lengths_and_closes_connection(self):
        for length in (None, "-1", "65537"):
            with self.subTest(length=length):
                if length is None:
                    sock = socket.create_connection(self.server.server_address, timeout=2)
                    sock.sendall(b"POST /ota/ HTTP/1.1\r\nHost: localhost\r\n\r\n")
                    response = sock.recv(1024)
                    self.assertIn(b" 411 ", response)
                    self.assertIn(b"Connection: close", response)
                    sock.close()
                    continue
                conn = http.client.HTTPConnection(*self.server.server_address, timeout=2)
                conn.request("POST", "/ota/", body=b"", headers={"Content-Length": length})
                response = conn.getresponse()
                self.assertIn(response.status, (400, 411, 413))
                self.assertEqual(response.getheader("Connection"), "close")
                response.read()
                conn.close()


if __name__ == "__main__":
    unittest.main()
