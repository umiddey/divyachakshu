import http.server
import json
import sys

MODE = sys.argv[1]
PORT = int(sys.argv[2])
BODIES = {
    "429": (429, {"error": {"message": "5 hour usage limit reached", "type": "rate_limit_error", "code": "1308"}}),
    "500": (500, {"error": {"message": "internal failure", "type": "server_error"}}),
    "400": (400, {"error": {"message": "model not found", "type": "invalid_request_error", "code": "model_not_found"}}),
}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        code, body = BODIES[MODE]
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
