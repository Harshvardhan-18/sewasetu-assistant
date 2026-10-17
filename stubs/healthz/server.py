"""Minimal healthz and eval assets server for services prior to full dashboard implementation."""

import http.server
import os
import sys


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok\n")
        elif self.path.startswith("/eval-dashboard/assets/") or self.path.startswith("/assets/"):
            filename = os.path.basename(self.path.split("?")[0])
            assets_dir = os.path.join(os.path.dirname(__file__), "assets")
            filepath = os.path.join(assets_dir, filename)
            if os.path.isfile(filepath):
                self.send_response(200)
                mime = "image/jpeg" if filename.lower().endswith(".jpg") else "application/pdf"
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(os.path.getsize(filepath)))
                self.end_headers()
                with open(filepath, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Not Found\n")
        else:
            self.send_response(501)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":false,"message":"not implemented yet"}\n')

    def do_POST(self):
        return self.do_GET()

    def log_message(self, format, *args):
        if args and str(args[0]).startswith("5"):
            super().log_message(format, *args)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    server = http.server.HTTPServer(("0.0.0.0", port), Handler)
    print(f"healthz and assets stub listening on :{port}")
    server.serve_forever()
