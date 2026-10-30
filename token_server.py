#!/usr/bin/env python3
"""Minimal LiveKit access-token issuer for the local demo.

Mints a signed LiveKit JWT on request instead of baking long-lived tokens
into the UI. Mirrors what a real app's backend would do: the browser asks
for a token by identity + room, the server signs it with the API secret.
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

API_KEY = os.environ.get("LIVEKIT_API_KEY", "access_token")
API_SECRET = os.environ.get("LIVEKIT_API_SECRET")
TOKEN_TTL_SECONDS = 3600

if not API_SECRET:
    sys.exit(
        "LIVEKIT_API_SECRET is not set. Export the same secret used to deploy "
        "livekit-server-minimal.yaml, e.g.:\n"
        "  export LIVEKIT_API_SECRET=$(openssl rand -base64 32)"
    )


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_token(identity: str, room: str) -> str:
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": API_KEY,
        "sub": identity,
        "iat": now,
        "nbf": now,
        "exp": now + TOKEN_TTL_SECONDS,
        "identity": identity,
        "name": identity,
        "video": {"roomJoin": True, "room": room},
    }
    signing_input = f"{b64url(json.dumps(header).encode())}.{b64url(json.dumps(payload).encode())}"
    signature = hmac.new(API_SECRET.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{b64url(signature)}"


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/token":
            self.send_response(404)
            self._cors()
            self.end_headers()
            return
        qs = urllib.parse.parse_qs(parsed.query)
        identity = qs.get("identity", [""])[0].strip()
        room = qs.get("room", [""])[0].strip()
        if not identity or not room:
            self.send_response(400)
            self._cors()
            self.end_headers()
            self.wfile.write(b'{"error":"identity and room query params are required"}')
            return
        token = make_token(identity, room)
        body = json.dumps({"token": token, "identity": identity, "room": room}).encode()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"[token-server] {self.address_string()} - {fmt % args}")


if __name__ == "__main__":
    port = 8082
    print(f"LiveKit token server listening on http://localhost:{port}/token?identity=...&room=...")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
