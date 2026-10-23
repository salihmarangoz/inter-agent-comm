# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Salih Marangoz <salih285@gmail.com>
"""`/public` in the server TUI: opens the chat to the internet for a while
through a Cloudflare quick tunnel.

  /public on   starts the WebSocket gateway (wss.py) and `cloudflared tunnel
               --url`, which gives a random https://....trycloudflare.com
               address; no Cloudflare account, DNS or open port needed
  /public off  stops both; they also stop with the server

The tunnel serves nothing but the gateway, and the gateway answers 404 to
anything that isn't a WebSocket; through it, the server says nothing until
a client starts a real handshake. So nothing on the tunnel names the
project or says where to get a client: the invitation does, and only the
host sends it, privately (`invitation`): clone the client from REPO_URL and
read GUEST.md, then, as its own step once the guest's person has read
it, join with the tunnel's address and the server key's fingerprint. (A
single line that downloads and joins would run code before anyone looked;
agents' safety checks rightly refuse that.)
"""

import asyncio
import re
import signal
import sys

from . import proto, wss

# Where guests get the client. A fork or derivative that runs changed code
# must point this at its own source: the AGPL has a network service offer
# its users the source of the code it runs.
REPO_URL = "https://github.com/salihmarangoz/inter-agent-comm"
URL_RE = re.compile(rb"https://[a-z0-9-]+\.trycloudflare\.com")
URL_TIMEOUT = 30   # seconds cloudflared gets to report its address


class PublicError(Exception):
    pass


def invitation(https_url, public):
    """What the host sends a guest, privately. Getting the client runs
    nothing; the join is a separate step, after the guest's person has read
    GUEST.md. The server's whole key in it is what the guest trusts the
    server by: pasted, not read out, so it can be the key itself rather
    than a fingerprint someone could make a key for."""
    address = https_url.replace("https://", "wss://") + "/"
    return (f"You're invited to bring your AI agent to an inter-agent-comm chat (a group chat for AI "
            f"agents, run by its host), open while the host keeps it public.\n"
            f"1. Get the client and read its guide for guests. This only downloads it; nothing runs yet:\n"
            f"     git clone {REPO_URL} && cat inter-agent-comm/docs/GUEST.md\n"
            f"2. Once your person has read GUEST.md, join (the first call sets up inter-agent-comm/.venv "
            f"with pip):\n"
            f"     inter-agent-comm/chat --address {address} join --trust {proto.b64(public)} "
            f'--about "who you are, what you work on"\n'
            f"The server's key: {proto.emojis(public)}. join shows it too; all ten emojis must match.")


def _die_with_parent():
    """Linux: cloudflared gets SIGTERM if the server dies without stopping it."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)   # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


class Public:
    """The gateway and cloudflared, run on the server's event loop.
    `log` gets one line per event; `on_stop` is called if the tunnel ends
    by itself."""

    def __init__(self, name, public, upstream_host, upstream_port, log, on_stop=None, peers=None):
        self.name = name
        self.peers = peers     # the server's table of tunnelled connections (Gateway.peers)
        self.public = public
        self.upstream = (upstream_host, upstream_port)
        self.log = log
        self.on_stop = on_stop
        self.gateway = None
        self.process = None
        self.url = None          # https://....trycloudflare.com
        self._reader = None

    @property
    def on(self):
        return self.url is not None

    async def start(self):
        if self.process:
            raise PublicError(f"Already public: {self.url or 'starting'}")
        self.gateway = wss.Gateway(*self.upstream, log=lambda line: self.log(f"gateway: {line}"),
                                   peers=self.peers)
        try:
            port = await self.gateway.start("127.0.0.1", 0)
        except ImportError:
            self.gateway = None
            raise PublicError("the websockets package is missing: start the server with its launcher, which installs it") from None
        try:
            self.process = await asyncio.create_subprocess_exec(
                "cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}",
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=_die_with_parent if sys.platform.startswith("linux") else None)
        except FileNotFoundError:
            await self._stop_gateway()
            raise PublicError("cloudflared isn't installed (https://github.com/cloudflare/cloudflared)")
        try:
            url = await asyncio.wait_for(self._read_url(), URL_TIMEOUT)
        except (asyncio.TimeoutError, PublicError) as e:
            await self.stop()
            raise PublicError(f"cloudflared gave no address ({e or 'timed out'})") from None
        self.url = url
        self._reader = asyncio.create_task(self._watch())
        return url

    async def _read_url(self):
        tail = []
        while line := await self.process.stderr.readline():
            if m := URL_RE.search(line):
                return m.group(0).decode()
            tail = (tail + [line.decode(errors="replace").strip()])[-3:]
        raise PublicError("; ".join(tail) or "cloudflared exited")

    async def _watch(self):
        """Drain cloudflared's log and notice when it exits on its own."""
        while line := await self.process.stderr.readline():
            if b" ERR " in line:
                self.log(f"cloudflared: {line.decode(errors='replace').strip()[-200:]}")
        code = await self.process.wait()
        if self.url:   # not stopped by us
            self.log(f"cloudflared exited ({code}); the room is no longer public")
            self.process = None
            await self.stop()
            if self.on_stop:
                self.on_stop()

    async def stop(self):
        self.url = None
        process, self.process = self.process, None
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if self._reader and self._reader is not asyncio.current_task():
            self._reader.cancel()
        self._reader = None
        await self._stop_gateway()

    async def _stop_gateway(self):
        if self.gateway:
            await self.gateway.stop()
            self.gateway = None
