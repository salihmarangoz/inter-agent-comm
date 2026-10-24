# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Salih Marangoz <salih285@gmail.com>
"""A guest's way to a room behind a Tor onion service: a TCP connection to
NAME.onion:PORT through the Tor running on the guest's machine (its SOCKS5
port), for proto.open_server.

  client (chat.py) --socks5--> local tor ==Tor network==> host's tor --> relay --> server

The Noise handshake runs end to end through it, as it does on the LAN.
Tor's SOCKS port: $INTER_AGENT_COMM_TOR (HOST:PORT), else the tor service's
127.0.0.1:9050, else Tor Browser's 127.0.0.1:9150.
"""

import os
import socket
import struct

PROXIES = (("127.0.0.1", 9050), ("127.0.0.1", 9150))
TIMEOUT = 60   # seconds: reaching an onion service the first time can take a while
# SOCKS5 replies (RFC 1928) as Tor uses them
REPLIES = {1: "Tor couldn't reach it", 4: "no such onion service is reachable now",
           5: "the onion service refused the connection", 6: "it took too long"}


def is_onion(host):
    return host.lower().endswith(".onion")


def proxies():
    configured = os.environ.get("INTER_AGENT_COMM_TOR", "").strip()
    if not configured:
        return PROXIES
    host, _, port = configured.rpartition(":")
    if not port.isdigit():
        raise OSError(f"INTER_AGENT_COMM_TOR={configured!r}: expected HOST:PORT of Tor's SOCKS port")
    return ((host or "127.0.0.1", int(port)),)


def _recv_exact(sock, n):
    data = b""
    while len(data) < n:
        part = sock.recv(n - len(data))
        if not part:
            raise ConnectionError("Tor closed the connection")
        data += part
    return data


def connect(host, port, timeout=TIMEOUT):
    """A socket to host:port through Tor. Raises OSError, with what to do,
    when there's no Tor here or the onion service doesn't answer."""
    timeout = max(timeout, TIMEOUT)
    try:
        name = host.encode("ascii")
    except UnicodeEncodeError:
        raise OSError(f"{host!r} isn't an onion address: they're plain letters and digits") from None
    if len(name) > 255 or not 0 < port < 65536:
        raise OSError(f"{host}:{port} isn't an onion address and port")
    tried = []
    for proxy in proxies():
        try:
            sock = socket.create_connection(proxy, timeout=timeout)
        except OSError:
            tried.append(f"{proxy[0]}:{proxy[1]}")
            continue
        try:
            sock.sendall(b"\x05\x01\x00")   # SOCKS5, no authentication
            if _recv_exact(sock, 2) != b"\x05\x00":
                raise OSError(f"{proxy[0]}:{proxy[1]} isn't Tor's SOCKS port")
            sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + struct.pack(">H", port))
            version, reply, _, kind = _recv_exact(sock, 4)
            if version != 5:
                raise OSError(f"{proxy[0]}:{proxy[1]} isn't Tor's SOCKS port")
            if reply:
                raise OSError(f"{host}: {REPLIES.get(reply, f'Tor answered {reply}')}. The room may not be "
                              "public right now; if the host says it is, try again in a minute")
            _recv_exact(sock, {1: 4, 4: 16}.get(kind) or _recv_exact(sock, 1)[0])   # the bound address
            _recv_exact(sock, 2)
            return sock
        except BaseException:
            sock.close()
            raise
    raise OSError(f"no Tor on this machine (nothing at {' or '.join(tried)}): install and start it "
                  "(sudo apt install tor), or keep Tor Browser open, or set INTER_AGENT_COMM_TOR=HOST:PORT")
