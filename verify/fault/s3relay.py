#!/usr/bin/env python3
"""Test-only fault-injection relay for the object store (docs/FLY-DEPLOYMENT-PLAN.md, cross-host acceptance tests).

A plain TCP pass-through: the frontend sets PANDO_S3_CONNECT=<relay host:port> and keeps PANDO_S3=https://<store>/...,
so TLS still terminates at the store (the relay only ever sees ciphertext and cannot read or alter requests). Not part
of any ordinary request path: without PANDO_S3_CONNECT the server connects to the store directly.

Modes, switched at run time through a control port bound to 127.0.0.1 only (HTTP GET /<mode>[?ms=N]):
  pass      forward everything (default)
  delay     forward, holding each chunk for ms milliseconds in each direction (added latency)
  stall     accept connections and read from them, forward nothing (the store looks alive but does not answer);
            connections already open are frozen too
  refuse    close new connections immediately; open ones are reset
  trickle   forward, but at most one byte every ms milliseconds (a store that keeps a connection alive while
            delivering almost nothing); ?dir=down trickles only the store's responses, ?dir=up only requests
  GET /state returns the mode and counters as JSON.

Usage: s3relay.py LISTEN_HOST:PORT UPSTREAM_HOST:PORT CONTROL_PORT
"""
import json, socket, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Relay:
    def __init__(self, listen, upstream, control_port):
        self.upstream = upstream; self.mode = "pass"; self.ms = 0; self.dir = "both"; self.lock = threading.Lock()
        self.conns = set(); self.stats = {"accepted": 0, "refused": 0, "bytes_up": 0, "bytes_down": 0, "plaintext_http_chunks": 0}
        h, p = listen.rsplit(":", 1)
        fam = socket.AF_INET6 if ":" in h.strip("[]") else socket.AF_INET
        self.ls = socket.socket(fam, socket.SOCK_STREAM); self.ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.ls.bind((h.strip("[]"), int(p))); self.ls.listen(256); self.port = self.ls.getsockname()[1]
        relay = self
        class Ctl(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                path, _, q = self.path.partition("?")
                m = path.strip("/")
                if m in ("pass", "delay", "stall", "refuse", "trickle"):
                    qs = dict(kv.split("=", 1) for kv in q.split("&") if "=" in kv)
                    relay.dir = qs.get("dir", "both"); relay.set(m, int(qs.get("ms", 0)))
                elif m != "state":
                    self.send_response(404); self.end_headers(); return
                body = json.dumps({"mode": relay.mode, "ms": relay.ms, **relay.stats}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)
        self.ctl = ThreadingHTTPServer(("127.0.0.1", control_port), Ctl); self.control_port = self.ctl.server_address[1]
        threading.Thread(target=self.ctl.serve_forever, daemon=True).start()
        threading.Thread(target=self._accept, daemon=True).start()

    def set(self, mode, ms=0):
        with self.lock:
            self.mode, self.ms = mode, ms
            doomed = list(self.conns) if mode == "refuse" else []
        for c in doomed:
            try: c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00"); c.close()
            except OSError: pass

    def _accept(self):
        while True:
            try: c, _ = self.ls.accept()
            except OSError: return
            if self.mode == "refuse":
                self.stats["refused"] += 1; c.close(); continue
            self.stats["accepted"] += 1
            try:
                h, p = self.upstream.rsplit(":", 1); u = socket.create_connection((h.strip("[]"), int(p)), timeout=5); u.settimeout(None)
            except OSError:
                c.close(); continue
            with self.lock: self.conns.update((c, u))
            for a, b, k in ((c, u, "bytes_up"), (u, c, "bytes_down")):
                threading.Thread(target=self._pump, args=(a, b, k), daemon=True).start()

    def _pump(self, a, b, k):
        try:
            while True:
                d = a.recv(65536)
                if not d: break
                while self.mode == "stall": time.sleep(0.05)   # hold: nothing is forwarded
                if self.mode == "refuse": break
                if self.mode == "delay" and self.ms: time.sleep(self.ms / 1000)
                if self.mode == "trickle" and (self.dir == "both" or (self.dir == "down") == (k == "bytes_down")):
                    for i in range(len(d)):
                        if self.mode != "trickle":   # switched away mid-chunk: forward the rest at once
                            b.sendall(d[i:]); self.stats[k] += len(d) - i; break
                        b.sendall(d[i:i + 1]); self.stats[k] += 1; time.sleep((self.ms or 200) / 1000)
                    continue
                if b"HTTP/1.1" in d: self.stats["plaintext_http_chunks"] += 1   # must stay 0 over TLS
                b.sendall(d); self.stats[k] += len(d)
        except OSError:
            pass
        for s in (a, b):
            try: s.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            try: s.close()
            except OSError: pass
        with self.lock: self.conns.difference_update((a, b))

if __name__ == "__main__":
    r = Relay(sys.argv[1], sys.argv[2], int(sys.argv[3]))
    print(json.dumps({"listen_port": r.port, "control_port": r.control_port}), flush=True)
    threading.Event().wait()
