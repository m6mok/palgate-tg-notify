from http.server import HTTPServer, BaseHTTPRequestHandler
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from typing import Optional
import threading
import time

from metrics import metrics


class MetricsHandler(BaseHTTPRequestHandler):
    """HTTP handler for Prometheus metrics endpoint"""

    def do_GET(self) -> None:
        if self.path == '/metrics':
            self.send_response(200)
            self.send_header('Content-Type', CONTENT_TYPE_LATEST)
            self.end_headers()

            # Update uptime before generating metrics
            metrics.update_uptime()

            self.wfile.write(generate_latest())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        """Override to suppress default logging"""
        pass


class MetricsServer:
    """Simple HTTP server for exposing Prometheus metrics"""

    def __init__(self, host: str = '0.0.0.0', port: int = 8000):
        self.host = host
        self.port = port
        self.server: Optional[HTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.running = False

    def start(self) -> None:
        """Start the metrics server in a background thread"""
        if self.running:
            return

        self.server = HTTPServer((self.host, self.port), MetricsHandler)
        self.thread = threading.Thread(target=self._run_server, daemon=True)
        self.thread.start()
        self.running = True

    def _run_server(self) -> None:
        """Run the HTTP server"""
        if self.server:
            self.server.serve_forever()

    def stop(self) -> None:
        """Stop the metrics server"""
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.running = False


# Global metrics server instance
metrics_server = MetricsServer()
