"""Minimal healthz stub for services that are not yet implemented.

Returns 200 on /healthz and 501 on everything else.
Used by docker-compose.assistant.yml until the real services are built.
"""

import http.server
import sys


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok\n")
        else:
            self.send_response(501)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":false,"message":"not implemented yet"}\n')

    def do_POST(self):
        return self.do_GET()

    def log_message(self, format, *args):
        # Quiet logs — only errors
        if args and str(args[0]).startswith("5"):
            super().log_message(format, *args)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    server = http.server.HTTPServer(("0.0.0.0", port), Handler)
    print(f"healthz stub listening on :{port}")
    server.serve_forever()
