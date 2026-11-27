"""A bounded, local HTTP service for exercising the device OTA check flow."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time


MAX_REQUEST_BYTES = 64 * 1024
CLIENT_TIMEOUT_SECONDS = 5


class OtaTestServer(ThreadingHTTPServer):
    """HTTP server that serves fixed OTA metadata and counts requests."""

    daemon_threads = True

    def __init__(self, address, *, mode, current_version, offered_version, base_url):
        if mode not in ("new", "forced", "none"):
            raise ValueError("mode must be new, forced, or none")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an explicit http(s) URL")
        self.mode = mode
        self.current_version = current_version
        self.offered_version = offered_version
        self.base_url = base_url.rstrip("/")
        self.ota_requests = 0
        self.download_requests = 0
        self._stats_lock = threading.Lock()
        super().__init__(address, OtaRequestHandler)

    def increment(self, name):
        with self._stats_lock:
            setattr(self, name, getattr(self, name) + 1)

    def get_stats(self):
        with self._stats_lock:
            return {
                "ota_requests": self.ota_requests,
                "download_requests": self.download_requests,
                "mode": self.mode,
            }


class OtaRequestHandler(BaseHTTPRequestHandler):
    """Handle the deliberately narrow OTA and stats endpoints."""

    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(CLIENT_TIMEOUT_SECONDS)

    def log_message(self, _format, *_args):
        # Request lines may contain identifiers or other device supplied data.
        return

    def _send_json(self, status, payload, *, close=False):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path not in ("/ota/", "/ota/v1/"):
            self._send_json(404, {"error": "not found"})
            return

        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or self.headers.get("Transfer-Encoding") is not None:
            self._send_json(411, {"error": "valid Content-Length required"}, close=True)
            return
        length_text = lengths[0].strip()
        if not length_text.isascii() or not length_text.isdecimal():
            self._send_json(400, {"error": "invalid Content-Length"}, close=True)
            return
        length = int(length_text)
        if length > MAX_REQUEST_BYTES:
            self._send_json(413, {"error": "request too large"}, close=True)
            return

        try:
            body = self.rfile.read(length)
            if len(body) != length:
                self.close_connection = True
                return
        except (TimeoutError, OSError):
            self.close_connection = True
            return

        server = self.server
        server.increment("ota_requests")
        if server.mode == "new":
            firmware = {
                "version": server.offered_version,
                "url": server.base_url + "/firmware.bin",
            }
        elif server.mode == "forced":
            firmware = {
                "version": server.current_version,
                "url": server.base_url + "/firmware.bin",
                "force": 1,
            }
        else:
            firmware = {
                "version": server.current_version,
                "url": server.base_url + "/firmware.bin",
            }
        self._send_json(200, {
            "firmware": firmware,
            "server_time": {"timestamp": int(time.time() * 1000), "timezone_offset": 480},
        })

    def do_GET(self):
        if self.path == "/stats":
            self._send_json(200, self.server.get_stats())
        elif self.path == "/firmware.bin":
            self.server.increment("download_requests")
            self._send_json(410, {"error": "firmware download intentionally unavailable"})
        else:
            self._send_json(404, {"error": "not found"})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mode", choices=("new", "forced", "none"), default="new")
    parser.add_argument("--current-version", default="2.6.0")
    parser.add_argument("--offered-version", default="99.0.0")
    parser.add_argument("--base-url", required=True,
                        help="explicit device-reachable http(s) origin; Host headers are ignored")
    args = parser.parse_args(argv)

    server = OtaTestServer(
        (args.host, args.port),
        mode=args.mode,
        current_version=args.current_version,
        offered_version=args.offered_version,
        base_url=args.base_url,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
