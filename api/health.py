from http.server import BaseHTTPRequestHandler

from ._demo import send_json


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        send_json(self, {"ok": True, "name": "Stock RL Trader", "runtime": "vercel-preview"})
