"""Проверка ожидания готовности на настоящем локальном HTTP-сервере."""

import importlib.util
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "wait_for_health", Path(__file__).resolve().parents[1] / "scripts" / "wait_for_health.py"
)
wait_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wait_module)


@contextmanager
def server(*, recover):
    class Handler(BaseHTTPRequestHandler):
        calls = 0

        def do_GET(self):
            Handler.calls += 1
            if recover and Handler.calls <= 2:
                self.connection.close()
                return
            self.send_response(200 if recover else 503)
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok" if recover else "starting"}).encode())

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/api/health", Handler
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def test_health_wait_recovers_from_initial_connection_resets():
    with server(recover=True) as (url, handler):
        assert wait_module.wait_for_health(url, timeout=3) == {"status": "ok"}
        assert handler.calls == 3


def test_health_wait_fails_with_diagnostics_after_bounded_deadline():
    with server(recover=False) as (url, handler):
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="не готов"):
            wait_module.wait_for_health(url, timeout=0.15)
        assert time.monotonic() - started < 2
        assert handler.calls >= 1
