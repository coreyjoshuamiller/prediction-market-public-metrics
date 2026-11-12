"""A minimal websocket client (standard library only) for Hyperliquid's public feed.

Supports what the trade collector needs: a TLS connection, masked text frames out, text /
ping / close frames in (with continuation frames and 16/64-bit lengths), and a read timeout.
"""
import base64
import json
import os
import select
import socket
import ssl
import struct


class WSClosed(Exception):
    pass


class WebSocket:
    def __init__(self, host, path="/ws", port=443, timeout=30):
        raw = socket.create_connection((host, port), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: pm-metrics\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSClosed("closed during handshake")
            head += chunk
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise WSClosed(f"handshake failed: {status.decode(errors='replace')}")
        self.buf = head.split(b"\r\n\r\n", 1)[1]

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WSClosed("connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send_json(self, obj):
        self._send(0x1, json.dumps(obj).encode())

    def recv_json(self, timeout=None):
        """The next text message, parsed; None if nothing arrives within `timeout` seconds."""
        if not self.buf and not self.sock.pending():
            if not select.select([self.sock], [], [], timeout)[0]:
                return None
        # once a frame has started, read all of it (a stall here means a dead connection)
        self.sock.settimeout(30)
        message, opcode = b"", None
        try:
            while True:
                b0, b1 = self._read(2)
                op, fin, n = b0 & 0x0F, b0 & 0x80, b1 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", self._read(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._read(8))[0]
                payload = self._read(n)  # server frames aren't masked
                if op == 0x8:
                    raise WSClosed("server closed the connection")
                if op == 0x9:
                    self._send(0xA, payload)
                    continue
                if op == 0xA:
                    continue
                if op != 0x0:
                    opcode = op
                message += payload
                if fin:
                    break
        except socket.timeout:
            raise WSClosed("timed out mid-frame") from None
        return json.loads(message) if opcode == 0x1 else None

    def close(self):
        try:
            self._send(0x8, b"")
            self.sock.close()
        except OSError:
            pass
