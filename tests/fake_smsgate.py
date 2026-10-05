"""A stand-in for the SMSGate phone app (local server mode), for tests only.

Speaks the little subset Santa uses: ``GET /health``, ``POST /message`` and
``GET /message/<id>``, with HTTP Basic authentication. Messages go through the
states ``Pending`` -> ``final_state`` after ``polls_before_final`` status calls.
"""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeSmsGate:
    def __init__(self, username="user", password="secret", final_state="Sent", polls_before_final=1, error="No service"):
        self.username, self.password = username, password
        self.final_state, self.polls_before_final, self.error = final_state, polls_before_final, error
        self.received = []          # [{"id", "numbers", "text", "polls"}]
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, status, body=None):
                raw = json.dumps(body if body is not None else {}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _authorized(self):
                want = "Basic " + base64.b64encode(f"{outer.username}:{outer.password}".encode()).decode()
                if self.headers.get("Authorization") != want:
                    self._reply(401, {"message": "unauthorized"})
                    return False
                return True

            def do_GET(self):
                if not self._authorized():
                    return
                if self.path == "/health":
                    return self._reply(200, {"status": "pass"})
                if self.path.startswith("/message/"):
                    mid = self.path.rsplit("/", 1)[1]
                    msg = next((m for m in outer.received if m["id"] == mid), None)
                    if not msg:
                        return self._reply(404, {"message": "not found"})
                    msg["polls"] += 1
                    state = outer.final_state if msg["polls"] >= outer.polls_before_final else "Pending"
                    recipients = [{"phoneNumber": n, "state": state, **({"error": outer.error} if state == "Failed" else {})}
                                  for n in msg["numbers"]]
                    return self._reply(200, {"id": mid, "state": state, "recipients": recipients})
                self._reply(404)

            def do_POST(self):
                if not self._authorized():
                    return
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if self.path != "/message" or not body.get("phoneNumbers") or "textMessage" not in body:
                    return self._reply(400, {"message": "bad request"})
                mid = f"m{len(outer.received) + 1}"
                outer.received.append({"id": mid, "numbers": body["phoneNumbers"], "text": body["textMessage"]["text"], "polls": 0})
                self._reply(202, {"id": mid, "state": "Pending"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
