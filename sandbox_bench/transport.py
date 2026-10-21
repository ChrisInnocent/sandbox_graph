"""Bounded HTTP and envd Connect JSON transport; errors never echo remote text."""
import base64
import binascii
from dataclasses import dataclass
import http.client
import json
import math
import socket
import struct
import threading
import time
from urllib.parse import urlsplit


MAX_BODY = 16 * 1024 * 1024


class TransportError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes
    headers_s: float
    complete_s: float


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TransportError("request deadline exceeded")
    return value


def validate_url(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or any(ord(c) < 33 for c in url)):
            raise ValueError
        _ = parsed.port
        return parsed
    except (ValueError, TypeError):
        raise TransportError("invalid HTTP endpoint") from None


def request(method, url, *, body=None, headers=None, timeout, on_chunk=None):
    """One request, no redirects/retries. Timeout covers headers and entire body."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise TransportError("invalid request timeout")
    parsed = validate_url(url)
    started = time.monotonic()
    deadline = started + timeout
    connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_class(parsed.hostname, parsed.port, timeout=timeout)
    expired = threading.Event()
    response = None

    def expire():
        expired.set()
        # Response may own the socket after a Connection: close response.
        sock = connection.sock
        if sock is None and response is not None and response.fp is not None:
            sock = getattr(getattr(response.fp, "raw", None), "_sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    try:
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        headers_s = time.monotonic() - started
        remaining(deadline)
        status = response.status
        if 300 <= status < 400:
            raise TransportError("HTTP redirect refused", status)
        if not 200 <= status < 300:
            raise TransportError("HTTP request failed", status)
        chunks = []
        size = 0
        while True:
            remaining(deadline)
            chunk = response.read1(min(65536, MAX_BODY + 1 - size))
            if not chunk:
                break
            if on_chunk is not None:
                on_chunk(chunk)
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BODY:
                raise TransportError("HTTP response exceeded size limit", status)
        remaining(deadline)
        if expired.is_set():
            raise TransportError("request deadline exceeded")
        if response.length not in (None, 0):
            raise TransportError("incomplete HTTP response", status)
        return Response(status, {k.lower(): v for k, v in response.getheaders()},
                        b"".join(chunks), headers_s, time.monotonic() - started)
    except TransportError:
        raise
    except (OSError, http.client.HTTPException, ValueError, UnicodeError):
        raise TransportError("request deadline exceeded" if expired.is_set()
                             else "HTTP transport failed") from None
    finally:
        timer.cancel()
        if response is not None:
            response.close()
        connection.close()


def decode_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, TypeError):
        raise TransportError("invalid JSON response") from None


def encode_connect(value):
    raw = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    return b"\x00" + struct.pack(">I", len(raw)) + raw


def parse_connect(raw):
    """Require both a completed process EndEvent and a clean end-stream envelope."""
    offset = 0
    terminal = False
    exit_code = None
    output = {"stdout": bytearray(), "stderr": bytearray()}
    try:
        while offset < len(raw):
            if terminal or len(raw) - offset < 5:
                raise ValueError
            flags, size = raw[offset], struct.unpack(">I", raw[offset + 1:offset + 5])[0]
            offset += 5
            if flags not in (0, 2) or size > MAX_BODY or offset + size > len(raw):
                raise ValueError
            message = decode_json(raw[offset:offset + size]) if size else {}
            offset += size
            if not isinstance(message, dict):
                raise ValueError
            if flags == 2:
                if message.get("error") is not None or exit_code is None:
                    raise ValueError
                terminal = True
                continue
            event = message.get("event")
            if not isinstance(event, dict) or len(event) != 1 or exit_code is not None:
                raise ValueError
            kind, value = next(iter(event.items()))
            if not isinstance(value, dict):
                raise ValueError
            if kind == "data":
                if len(value) != 1:
                    raise ValueError
                name, encoded = next(iter(value.items()))
                if name not in output or not isinstance(encoded, str):
                    raise ValueError
                output[name].extend(base64.b64decode(encoded, validate=True))
            elif kind == "end":
                if value.get("error") or value.get("exited") is not True:
                    raise ValueError
                # proto3 omits a default scalar zero, but never the EndEvent itself.
                code = value.get("exitCode", value.get("exit_code", 0))
                if type(code) is not int or not -(2**31) <= code < 2**31:
                    raise ValueError
                if "exitCode" in value and "exit_code" in value:
                    raise ValueError
                exit_code = code
            elif kind not in ("start", "keepalive"):
                raise ValueError
        if not terminal or exit_code is None:
            raise ValueError
        return {**{k: v.decode("utf-8", errors="replace") for k, v in output.items()},
                "exit_code": exit_code}
    except (ValueError, TypeError, binascii.Error, struct.error):
        raise TransportError("invalid or unsuccessful Connect stream") from None


class ConnectDecoder:
    """Bounded incremental Connect JSON decoder; hashes original output bytes."""
    def __init__(self, *, on_event=None, max_bytes=MAX_BODY):
        self.buffer=bytearray();self.raw=bytearray();self.total=0
        self.on_event=on_event or (lambda kind: None)
        self.max_bytes=max_bytes
        self.terminal=False;self.process_end=False
        self.output={"stdout":bytearray(),"stderr":bytearray()}

    def feed(self, chunk):
        self.total+=len(chunk)
        if self.total>self.max_bytes:raise TransportError("Connect stream exceeded size limit")
        self.raw.extend(chunk);self.buffer.extend(chunk)
        while len(self.buffer)>=5:
            flag=self.buffer[0];size=struct.unpack('>I',self.buffer[1:5])[0]
            if self.terminal or flag not in (0,2) or size>self.max_bytes:
                raise TransportError("invalid Connect stream frame")
            if len(self.buffer)<5+size:return
            payload=bytes(self.buffer[5:5+size]);del self.buffer[:5+size]
            data=decode_json(payload) if payload else {}
            if not isinstance(data,dict):raise TransportError("invalid Connect stream payload")
            if flag==2:
                if not self.process_end or data.get('error') is not None:
                    raise TransportError("unsuccessful Connect end stream")
                self.terminal=True;self.on_event('stream_end');continue
            event=data.get('event')
            if self.process_end or not isinstance(event,dict) or len(event)!=1:
                raise TransportError("invalid process event order")
            kind,value=next(iter(event.items()))
            if not isinstance(value,dict):raise TransportError("invalid process event")
            if kind=='data':
                if len(value)!=1:raise TransportError("invalid output event")
                stream,encoded=next(iter(value.items()))
                if stream not in self.output or not isinstance(encoded,str):raise TransportError("invalid output stream")
                try:self.output[stream].extend(base64.b64decode(encoded,validate=True))
                except (ValueError,binascii.Error):raise TransportError("invalid output base64") from None
                self.on_event(stream)
            elif kind=='end':
                if value.get('exited') is not True or value.get('error'):
                    raise TransportError("process did not exit successfully")
                self.process_end=True;self.on_event('end')
            elif kind in ('start','keepalive'):self.on_event(kind)
            else:raise TransportError("unknown process event")

    def finish(self):
        import hashlib
        if self.buffer or not self.terminal or not self.process_end:
            raise TransportError("incomplete Connect stream")
        # Final parser independently validates scalar exit code and all envelopes.
        result=parse_connect(bytes(self.raw))
        for name,value in self.output.items():
            result[name+'_bytes']=len(value)
            result[name+'_sha256']=hashlib.sha256(value).hexdigest()
        return result
