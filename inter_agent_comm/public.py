# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Salih Marangoz <salih285@gmail.com>
"""The ways in from the internet, opened from the server TUI's Share
window (/share): a Cloudflare quick tunnel and a Tor onion service.

  Cloudflare  `cloudflared tunnel --url` in front of the WebSocket gateway
              (wss.py): a random https://....trycloudflare.com address,
              new each time; no Cloudflare account, DNS or open port
  Tor         a `tor` of the server's own in front of Relay, a plain TCP
              relay: NAME.onion, the same address every time if the host
              keeps its key; no account or open port. The guest needs Tor
              (onion.py).

Each way in has its own relay, which records every connection it carries
for the server (`peers`): the server marks those agents CF or TOR, never
LOCAL, though they reach it from this machine. Cloudflare's gateway counts
its limits per visitor (CF-Connecting-IP); Tor shows no visitor address,
so Relay counts all Tor guests together. Keeping them apart matters: a Tor
guest could send any CF-Connecting-IP header to the gateway.

Nothing on either names the project or says where to get a client: the
invitation does, and only the host sends it, privately (`invitation`):
clone the client from REPO_URL and read GUEST.md, then, as its own step
once the guest's person has read it, join with the address and the
server's key. (A single line that downloads and joins would run code
before anyone looked; agents' safety checks rightly refuse that.)
"""

import asyncio
import collections
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import proto, wss

# Where guests get the client. A fork or derivative that runs changed code
# must point this at its own source: the AGPL has a network service offer
# its users the source of the code it runs.
REPO_URL = "https://github.com/salihmarangoz/inter-agent-comm"
URL_RE = re.compile(rb"https://[a-z0-9-]+\.trycloudflare\.com")
CF_TIMEOUT = 30    # seconds cloudflared gets to report its address
TOR_TIMEOUT = 120  # seconds tor gets to start and publish the onion address
# Tor guests all look alike to Relay, so these count them together.
MAX_OPEN_TOR = 32
MAX_NEW_TOR_PER_MINUTE = 120


class PublicError(Exception):
    pass


def invitation(address, public):
    """What the host sends a guest, privately. Getting the client runs
    nothing; the join is a separate step, after the guest's person has read
    GUEST.md. The server's whole key in it is what the guest trusts the
    server by: pasted, not read out, so it can be the key itself rather
    than a fingerprint someone could make a key for. address is what the
    guest passes to --address: a wss:// URL or NAME.onion:PORT."""
    tor = ".onion" in address
    need = ("This room is reached through Tor: Tor must run on your machine (sudo apt install tor, or keep "
            "Tor Browser open). " if tor else "")
    return (f"You're invited to bring your AI agent to an inter-agent-comm chat (a group chat for AI "
            f"agents, run by its host), open while the host keeps it public.\n"
            f"1. Get the client and read its guide for guests. This only downloads it; nothing runs yet:\n"
            f"     git clone {REPO_URL} && cat inter-agent-comm/docs/GUEST.md\n"
            f"2. Once your person has read GUEST.md, join (the first call sets up inter-agent-comm/.venv "
            f"with pip). {need}\n"
            f"     inter-agent-comm/chat --address {address} join --trust {proto.b64(public)} "
            f'--about "who you are, what you work on"\n'
            f"The server's key: {proto.emojis(public)}. join shows it too; all ten emojis must match.")


def _die_with_parent():
    """Linux: the tunnel's process gets SIGTERM if the server dies without stopping it."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)   # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


class Relay:
    """Tor's way in: the onion service hands each guest's stream to this
    listener on 127.0.0.1, which opens its own connection to the chat
    server and records it in `peers` as ("TOR", ""), so the server marks
    the agent TOR and never takes it for one on this machine."""

    def __init__(self, upstream_host, upstream_port, log, peers=None):
        self.upstream = (upstream_host, upstream_port)
        self.log = log
        self.peers = {} if peers is None else peers
        # As with the gateway: the server's own table is cleaned by the server.
        self.owns_peers = peers is None
        self.server = None
        self.port = None
        self.open = set()                  # writers of the guests' streams
        self.recent = collections.deque()  # when each of the last minute's streams came
        self.refused_noted = 0.0

    async def start(self, host="127.0.0.1", port=0):
        self.server = await asyncio.start_server(self._serve, host, port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self):
        if self.server:
            self.server.close()
            for writer in list(self.open):
                writer.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), 5)
            except asyncio.TimeoutError:
                pass
            self.server = None

    async def _serve(self, reader, writer):
        now = time.monotonic()
        while self.recent and now - self.recent[0] > 60:
            self.recent.popleft()
        reason = "too many open connections" if len(self.open) >= MAX_OPEN_TOR else \
            "too many connections this minute" if len(self.recent) >= MAX_NEW_TOR_PER_MINUTE else None
        if reason:
            if now - self.refused_noted > 60:   # one line a minute, not one per attempt
                self.refused_noted = now
                self.log(f"refused Tor guests: {reason}")
            writer.close()
            return
        self.recent.append(now)
        self.open.add(writer)
        local = None
        try:
            try:
                up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(*self.upstream), 5)
            except (OSError, asyncio.TimeoutError) as e:
                self.log(f"the chat server isn't reachable ({e or type(e).__name__})")
                return
            local = tuple(up_writer.get_extra_info("sockname")[:2])
            # before any byte goes up; a list, so each entry is its own object (see wss.forget_later)
            entry = self.peers[local] = [proto.TOR, ""]
            try:
                await _pipe(reader, writer, up_reader, up_writer)
            finally:
                up_writer.close()
        finally:
            if local and self.owns_peers:
                self.peers.pop(local, None)
            elif local:
                wss.forget_later(self.peers, local, entry)
            self.open.discard(writer)
            writer.close()


async def _pipe(reader, writer, up_reader, up_writer):
    """Bytes both ways until either side closes."""
    async def copy(src, dst):
        while data := await src.read(65536):
            dst.write(data)
            await dst.drain()

    tasks = [asyncio.create_task(copy(reader, up_writer)), asyncio.create_task(copy(up_reader, writer))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, OSError):
                pass


async def _end(process):
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


class Tunnel:
    """A tunnel's process and its relay, run on the server's event loop.
    `log` gets one line per event; `on_stop` is called if the tunnel ends
    by itself. `address` is what a guest passes to --address while it's on."""

    tag = name = program = install = ""
    output = "stderr"   # where the process tells its progress

    def __init__(self, upstream_host, upstream_port, log, on_stop=None, peers=None):
        self.upstream = (upstream_host, upstream_port)
        self.log = log
        self.on_stop = on_stop
        self.peers = peers     # the server's table of tunnelled connections
        self.relay = None
        self.process = None
        self.address = None
        self.progress = ""     # while starting: how far it got
        self._reader = None
        self._busy = False     # start() is running
        self._epoch = 0        # stop() counts up: a start that sees it change was closed meanwhile

    @property
    def on(self):
        return self.address is not None

    @classmethod
    def installed(cls):
        return shutil.which(cls.program) is not None

    async def start(self):
        """Open it: the address, or PublicError. A stop() while it starts
        wins: whatever it started by then is stopped, and it raises."""
        if self._busy or self.process:
            raise PublicError(f"{self.name} is already {'on' if self.on else 'starting'}")
        self._busy, epoch, self.progress = True, self._epoch, "starting"
        closed = PublicError(f"{self.name} was closed while it started")
        try:
            try:
                relay, port = await self._start_relay()
            except ImportError:
                raise PublicError("the websockets package is missing: start the server with its launcher, "
                                  "which installs it") from None
            if epoch != self._epoch:
                await relay.stop()
                raise closed
            self.relay = relay
            try:
                command = self._command(port)
            except OSError as e:   # its folder: not ours (after a sudo), or a file
                await self._stop_relay()
                raise PublicError(f"couldn't prepare {self.program}'s files ({e})") from None
            try:
                process = await asyncio.create_subprocess_exec(
                    *command, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE if self.output == "stdout" else asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE if self.output == "stderr" else asyncio.subprocess.DEVNULL,
                    preexec_fn=_die_with_parent if sys.platform.startswith("linux") else None)
            except FileNotFoundError:
                await self._stop_relay()
                raise PublicError(f"{self.program} isn't installed ({self.install})") from None
            except OSError as e:
                await self._stop_relay()
                raise PublicError(f"couldn't start {self.program} ({e})") from None
            if epoch != self._epoch:   # stop() has stopped the relay
                await _end(process)
                raise closed
            self.process = process
            try:
                address = await asyncio.wait_for(self._ready(process), self.timeout)
            except (asyncio.TimeoutError, PublicError, ValueError) as e:   # ValueError: a line too long
                if epoch != self._epoch:
                    raise closed from None
                await self.stop()
                raise PublicError(f"{self.program} gave no address ({e or 'timed out'})") from None
            if epoch != self._epoch:
                raise closed
            self.address, self.progress = address, ""
            self._reader = asyncio.create_task(self._watch(process))
            return address
        finally:
            self._busy = False
            if not self.on:
                self.progress = ""

    async def _ready(self, process):
        """Read the process's output until it's reachable: its address."""
        stream, tail = getattr(process, self.output), []
        while line := await stream.readline():
            address = self._parse(line)
            if address:
                return address
            tail = (tail + [line.decode(errors="replace").strip()])[-3:]
        raise PublicError("; ".join(tail) or f"{self.program} exited")

    async def _watch(self, process):
        """Drain the process's output and notice when it exits on its own."""
        stream = getattr(process, self.output)
        while True:
            try:
                line = await stream.readline()
            except ValueError:   # a line over the stream's limit: skipped, the rest still read
                continue
            if not line:
                break
            if self._is_error(line):
                self.log(f"{self.program}: {line.decode(errors='replace').strip()[-200:]}")
        code = await process.wait()
        if self.process is process:   # not stopped by us
            self.log(f"{self.program} exited ({code}); {self.name} is closed")
            self.process = None
            await self.stop()
            if self.on_stop:
                self.on_stop()

    async def stop(self):
        self._epoch += 1
        self.address, self.progress = None, ""
        process, self.process = self.process, None
        if process:
            await _end(process)
        if self._reader and self._reader is not asyncio.current_task():
            self._reader.cancel()
        self._reader = None
        await self._stop_relay()
        self._stopped()

    async def _stop_relay(self):
        if self.relay:
            await self.relay.stop()
            self.relay = None

    def _stopped(self):
        """After the process and the relay stopped."""


class Cloudflare(Tunnel):
    """A Cloudflare quick tunnel in front of the WebSocket gateway."""

    tag, name, program = proto.CF, "the Cloudflare tunnel", "cloudflared"
    install = "https://github.com/cloudflare/cloudflared"
    timeout = CF_TIMEOUT

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.url = None   # https://....trycloudflare.com

    async def _start_relay(self):
        relay = wss.Gateway(*self.upstream, log=lambda line: self.log(f"gateway: {line}"), peers=self.peers)
        return relay, await relay.start("127.0.0.1", 0)

    def _command(self, port):
        return ["cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"]

    def _parse(self, line):
        if m := URL_RE.search(line):
            self.url = m.group(0).decode()
            return self.url.replace("https://", "wss://") + "/"
        return None

    @staticmethod
    def _is_error(line):
        return b" ERR " in line

    def _stopped(self):
        self.url = None


def tor_folder():
    """The server's own Tor: its data, its onion key (onion/) and an empty torrc."""
    return proto.home() / "tor"


def kept_onion(folder=None):
    """The onion address a start would use: the kept key's, if any."""
    try:
        return ((folder or tor_folder()) / "onion" / "hostname").read_text().strip() or None
    except OSError:
        return None


def forget_onion(folder=None):
    """A new address next time: drop the kept key."""
    shutil.rmtree((folder or tor_folder()) / "onion", ignore_errors=True)


def tor_version():
    """tor's version as a tuple, (0,) if it doesn't say: proof-of-work came in 0.4.8."""
    try:
        out = subprocess.run(["tor", "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return (0,)
    m = re.search(r"Tor version (\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in m.groups()) if m else (0,)


class Tor(Tunnel):
    """A Tor onion service of the server's own: its own tor process and
    data folder (the system's tor and its config stay out of it), in front
    of Relay. Single onion service: the host's side takes one hop instead
    of three, which made it about twice as fast (benchmark in PUBLIC.md).
    Guests' addresses stay hidden; the relays the host's tor talks to see the
    host's address, as Cloudflare does. Its key, and so its address, is kept
    in `folder` unless `keep` is off. The onion's port is always the default
    one, whatever --port says, so a kept address stays whole."""

    tag, name, program = proto.TOR, "the Tor onion service", "tor"
    install = "sudo apt install tor, or https://www.torproject.org"
    timeout = TOR_TIMEOUT
    output = "stdout"

    def __init__(self, *args, folder=None, keep=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.folder = Path(folder) if folder else tor_folder()
        self.keep = keep
        self.pow = False   # tor has proof-of-work defenses (0.4.8 and later); asked at each start

    @property
    def key_dir(self):
        return self.folder / "onion"

    def kept_address(self):
        return kept_onion(self.folder)

    def forget(self):
        forget_onion(self.folder)

    async def _start_relay(self):
        # tor --version off the event loop: a slow tor mustn't hold up the chat
        self.pow = await asyncio.to_thread(tor_version) >= (0, 4, 8)
        relay = Relay(*self.upstream, log=lambda line: self.log(f"tor relay: {line}"), peers=self.peers)
        return relay, await relay.start("127.0.0.1", 0)

    def _command(self, port):
        if not self.keep:
            self.forget()
        for folder in (self.folder, self.folder / "data", self.key_dir):
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(folder, 0o700)   # tor refuses folders others can read
        empty = self.folder / "torrc"   # neither the system's torrc nor its defaults
        empty.write_text("")
        return ["tor", "-f", str(empty), "--defaults-torrc", str(empty), "--SocksPort", "0",
                "--DataDirectory", str(self.folder / "data"), "--HiddenServiceDir", str(self.key_dir),
                "--HiddenServicePort", f"{proto.DEFAULT_PORT} 127.0.0.1:{port}",
                "--HiddenServiceNonAnonymousMode", "1", "--HiddenServiceSingleHopMode", "1",
                *(["--HiddenServicePoWDefensesEnabled", "1"] if self.pow else []),
                "--__OwningControllerProcess", str(os.getpid()),
                # notices, and the onion service's info lines: they say when the address is published
                "--Log", "[rend]info notice stdout"]

    def _parse(self, line):
        if m := re.search(rb"Bootstrapped (\d+)%", line):
            self.progress = "publishing the address" if m.group(1) == b"100" else f"starting, {int(m.group(1))}%"
        elif b"Uploaded hidden service descriptor" in line and b"200" in line:
            onion = self.kept_address()
            if not onion:
                raise PublicError(f"no onion address in {self.key_dir}")
            return f"{onion}:{proto.DEFAULT_PORT}"
        return None

    @staticmethod
    def _is_error(line):
        return b"[err]" in line

    def _stopped(self):
        if not self.keep:
            self.forget()
