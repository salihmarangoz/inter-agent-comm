# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Salih Marangoz <salih285@gmail.com>
"""The chat over WebSocket, for reaching the server from outside the LAN
through a Cloudflare quick tunnel.

Cloudflare's tunnels carry HTTP and WebSocket, not raw TCP, so:

  client (chat.py) --wss--> Cloudflare --> cloudflared --ws--> gateway --tcp--> server

The gateway only moves bytes. The Noise handshake still runs end to end
between client and server, so Cloudflare and the gateway see ciphertext
and can't pose as the server; joining still needs the host's approval.

Every tunnelled connection reaches the gateway from cloudflared on
127.0.0.1, so the gateway takes the visitor's address from Cloudflare's
CF-Connecting-IP header and limits connections per address itself.

  python wss.py gateway --listen 127.0.0.1:23233 --upstream 127.0.0.1:23232
"""

import argparse
import asyncio
import collections
import ipaddress
import socket
import sys
import time

# Per visitor address. One chat command is one connection; a waiting agent
# holds one more. A file transfer is one connection for its whole length.
MAX_OPEN_PER_IP = 8
MAX_NEW_PER_MINUTE = 60   # per visitor address
MAX_OPEN = 100            # all visitors together (a quick tunnel takes 200)
MAX_MESSAGE = 1 << 20     # bytes in one WebSocket message; frames are under 64 KiB
CHUNK = 65536
UNREACHABLE = 1011        # close code: the gateway can't reach the chat server
CF = "CF"                 # the way in the gateway records for the server (proto.CF)
RELAYED_GRACE = 60        # seconds after a relayed connection ends: the server has looked it up by then


def _ws_options():
    # Noise ciphertext doesn't compress. The server pings while a client
    # waits, so the WebSocket's own pings only keep Cloudflare's idle
    # timeout away during long gaps.
    return dict(compression=None, max_size=MAX_MESSAGE, ping_interval=30, ping_timeout=30)


# --- client side: a blocking socket look-alike for proto.connect ---

class Stream:
    """The calls proto.py and chat.py make on a socket (sendall, recv,
    settimeout, close), carried over one WebSocket."""

    def __init__(self, ws, timeout):
        self.ws = ws
        self.timeout = timeout
        self.buf = bytearray()
        self.eof = False

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, data):
        from websockets.exceptions import ConnectionClosed
        try:
            self.ws.send(bytes(data))
        except ConnectionClosed as e:
            raise ConnectionError("the server closed the connection") from e

    def recv(self, n):
        from websockets.exceptions import ConnectionClosed
        while not self.buf and not self.eof:
            try:
                data = self.ws.recv(timeout=self.timeout)
            except ConnectionClosed as e:
                if e.rcvd and e.rcvd.code == UNREACHABLE:
                    raise ConnectionRefusedError("the gateway can't reach the chat server") from None
                self.eof = True
                break
            except TimeoutError:
                raise socket.timeout("timed out") from None
            if isinstance(data, str):
                raise ConnectionError("unexpected text message from the gateway")
            self.buf += data
        part = bytes(self.buf[:n])
        del self.buf[:n]
        return part

    def close(self):
        self.ws.close()


def is_url(address):
    return address.startswith(("ws://", "wss://"))


def open_stream(url, timeout):
    """Connect to a gateway URL. Raises OSError when it can't be reached,
    like socket.create_connection does."""
    try:
        from websockets.exceptions import InvalidHandshake, InvalidURI
        from websockets.sync.client import connect
    except ImportError:
        raise OSError("connecting by URL needs the websockets package "
                      "(run it through the chat launcher, which installs it)") from None
    try:
        ws = connect(url, open_timeout=timeout, close_timeout=2, legacy=True, **_ws_options())
    except (InvalidHandshake, InvalidURI, TimeoutError) as e:
        raise OSError(f"no gateway at {url} ({e})") from None
    return Stream(ws, timeout)


def forget_later(peers, local, entry):
    """A relay's entry in the server's table, once its connection has ended:
    the server pops the ones it served, but not one it never accepted (a
    listener closing, say). Left there, it would tag an unrelated local
    connection from the same port. Popping it now could beat the server's
    own lookup, which comes after the handshake, so it waits RELAYED_GRACE;
    and it drops only this entry, not a new one for the same port."""
    def drop():
        if peers.get(local) is entry:
            del peers[local]
    asyncio.get_running_loop().call_later(RELAYED_GRACE, drop)


# --- gateway ---

class Gateway:
    """Accepts WebSocket connections on a local address and gives each its
    own TCP connection to the chat server. `log` gets refusals, failures and
    downloads; `trace`, if given, every connection opened and closed."""

    def __init__(self, upstream_host, upstream_port, log=None, trace=None, peers=None):
        self.upstream = (upstream_host, upstream_port)
        # (host, port) of each upstream connection's own end -> ("CF", its visitor), so
        # a server in this process can tell tunnelled agents from local ones.
        self.peers = {} if peers is None else peers
        # A table given by the chat server is cleaned by the server when it's done with each
        # connection: popping it here could happen before the server looks the visitor up,
        # and the guest would pass as a local agent.
        self.owns_peers = peers is None
        self.log = log or (lambda line: print(line, file=sys.stderr, flush=True))
        self.trace = trace or (lambda line: None)
        self.open = collections.Counter()           # visitor address -> open connections
        self.recent = collections.defaultdict(collections.deque)   # address -> start times
        self.server = None

    async def start(self, host="127.0.0.1", port=0):
        from websockets.asyncio.server import ServerConnection, serve

        class Quiet(ServerConnection):
            """Every refusal the library makes on its own (a request too long, a
            half handshake, a bad header) is the same plain 404 as _admit's, so
            none of them says what answers here. _admit's 429 keeps its status."""
            def __init__(self, protocol, *args, **kwargs):
                super().__init__(protocol, *args, **kwargs)
                self.reject_as_is = reject = protocol.reject
                protocol.reject = lambda status, text: reject(404, "Not found\n")
        self.server = await serve(self._relay, host, port, process_request=self._admit,
                                  server_header=None, create_connection=Quiet, **_ws_options())
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    @staticmethod
    def visitor(connection, request):
        """Who the limits count: Cloudflare's CF-Connecting-IP when the
        connection comes from this machine (cloudflared), else the peer.
        An IPv6 address counts as its /64, since one user usually holds a
        whole /64 and could change addresses inside it at will."""
        address = ipaddress.ip_address(connection.remote_address[0])
        header = request.headers.get("CF-Connecting-IP", "").strip()
        if header and address.is_loopback:
            try:
                address = ipaddress.ip_address(header)
            except ValueError:
                pass
        if address.version == 6:
            if address.ipv4_mapped:
                return str(address.ipv4_mapped)
            return str(ipaddress.ip_network(f"{address}/64", strict=False))
        return str(address)

    def _admit(self, connection, request):
        ip = self.visitor(connection, request)
        connection.visitor = ip
        # Anything but a WebSocket on / gets a plain 404: nothing here names
        # the project to someone who found the tunnel's address.
        upgrade = [v.lower() for v in request.headers.get_all("Upgrade")]
        if request.path != "/" or upgrade != ["websocket"]:
            return connection.respond(404, "Not found\n")
        now = time.monotonic()
        recent = self.recent[ip]
        while recent and now - recent[0] > 60:
            recent.popleft()
        if not recent:
            del self.recent[ip]   # keeps the table to the addresses seen in the last minute
        reason = None
        if sum(self.open.values()) >= MAX_OPEN:
            reason = "gateway full"
        elif self.open[ip] >= MAX_OPEN_PER_IP:
            reason = "too many open connections"
        elif len(recent) >= MAX_NEW_PER_MINUTE:
            reason = "too many connections this minute"
        if reason:
            self.log(f"refused {ip}: {reason}")
            return connection.reject_as_is(429, f"{reason}\n")
        self.recent[ip].append(now)
        self.open[ip] += 1
        # Counted until the connection closes, whether or not the
        # handshake finishes and _relay runs.
        asyncio.get_running_loop().create_task(self._release(connection, ip))
        return None

    async def _release(self, connection, ip):
        await connection.wait_closed()
        self.open[ip] -= 1
        if self.open[ip] <= 0:
            del self.open[ip]

    async def _relay(self, ws):
        ip = ws.visitor
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(*self.upstream), 5)
        except (OSError, asyncio.TimeoutError) as e:
            self.log(f"{ip}: the chat server isn't reachable ({e or type(e).__name__})")
            await ws.close(UNREACHABLE)   # no reason text: it would tell a stranger what's behind
            return
        self.trace(f"{ip}: connected")
        local = tuple(writer.get_extra_info("sockname")[:2])
        entry = self.peers[local] = (CF, ip)   # before any byte goes up, so before the server's handshake ends
        try:
            await self._pipe(ws, reader, writer)
        finally:
            if self.owns_peers:
                self.peers.pop(local, None)
            else:
                forget_later(self.peers, local, entry)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        self.trace(f"{ip}: closed")

    async def _pipe(self, ws, reader, writer):
        from websockets.exceptions import ConnectionClosed

        async def up():
            async for message in ws:
                if isinstance(message, str):
                    return
                writer.write(message)
                await writer.drain()

        async def down():
            while data := await reader.read(CHUNK):
                await ws.send(data)

        tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                try:
                    await t
                except (asyncio.CancelledError, ConnectionClosed, OSError):
                    pass
            await ws.close()


def _host_port(text, default_host):
    host, sep, port = text.rpartition(":")
    if not sep or not port.isdigit():
        raise argparse.ArgumentTypeError(f"expected HOST:PORT, got {text!r}")
    return host or default_host, int(port)


def main():
    # Run on its own, the gateway can't tell the server how its connections came in: each would
    # look like a local agent (and a local orchestrator may restart the server). Use the server's Share.
    p = argparse.ArgumentParser(description="WebSocket gateway in front of the chat server (for tests; "
                                            "use the server's Share window, which tags its connections CF)")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gateway", help="relay WebSocket connections to the chat server")
    g.add_argument("--listen", type=lambda t: _host_port(t, "127.0.0.1"), default=("127.0.0.1", 23233))
    g.add_argument("--upstream", type=lambda t: _host_port(t, "127.0.0.1"), default=("127.0.0.1", 23232))
    args = p.parse_args()

    async def run():
        gateway = Gateway(*args.upstream, trace=lambda line: print(line, file=sys.stderr, flush=True))
        port = await gateway.start(*args.listen)
        gateway.log(f"gateway on ws://{args.listen[0]}:{port}/ -> {args.upstream[0]}:{args.upstream[1]}")
        await asyncio.Event().wait()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
