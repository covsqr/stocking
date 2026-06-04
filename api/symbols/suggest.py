from http.server import BaseHTTPRequestHandler

from .._demo import candidates, read_json, send_json


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = read_json(self)
        send_json(self, {"items": candidates(payload.get("limit", 8))})
