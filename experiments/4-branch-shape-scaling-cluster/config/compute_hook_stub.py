import http.server
class Handler(http.server.BaseHTTPRequestHandler):
    def do_PUT(self):
        length = int(self.headers.get("Content-Length", 0))
        if length:
            self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()
    def log_message(self, fmt, *args):
        pass
http.server.HTTPServer(("0.0.0.0", 9999), Handler).serve_forever()
