from http.server import BaseHTTPRequestHandler

from .._demo import read_json, send_json, state_payload


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        send_json(self, state_payload())

    def do_POST(self):
        payload = read_json(self)
        send_json(self, state_payload(payload.get("symbols"), payload.get("profile", "balanced")))
