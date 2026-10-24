#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Salih Marangoz <salih285@gmail.com>
"""Tests for the ways in and trust: Tor's relay and onion service (with a
stand-in tor and a stand-in SOCKS proxy), the client's way through Tor,
opening and closing LAN, Mine or Guest at approval, and the Share window.
Runs on this machine only; tests/run.py calls run_checks().

  .venv/bin/python tests/test_access.py
"""

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # the repo, for inter_agent_comm
from inter_agent_comm import proto, public, server  # noqa: E402
from run import Harness, chat, chat_bg, check, failures  # noqa: E402

ONION = "a" * 56 + ".onion"

# Stands in for tor: notes its arguments, makes a key folder's hostname once
# (a new name each time the folder is new), says it published, and waits.
FAKE_TOR = """#!/usr/bin/env python3
import os, sys, time, uuid
args = sys.argv[1:]
if "--version" in args:
    print("Tor version 0.4.9.11.")
    sys.exit(0)
keys = args[args.index("--HiddenServiceDir") + 1]
with open(os.environ["FAKE_TOR_LOG"], "a") as log:
    log.write(" ".join(args) + "\\n")
host = os.path.join(keys, "hostname")
if not os.path.exists(host):
    with open(host, "w") as f:
        f.write(uuid.uuid4().hex + "x" * 24 + ".onion\\n")
print("Oct 24 00:00:00.000 [notice] Bootstrapped 100% (done): Done", flush=True)
print("Oct 24 00:00:01.000 [info] handle_response_upload_hsdesc(): Uploaded hidden service descriptor "
      "(status 200 (\\"HS descriptor stored successfully.\\"))", flush=True)
if os.environ.get("FAKE_TOR_LONG"):   # a line over the reader's limit, then an error, then gone
    time.sleep(0.5)
    print("x" * 200000, flush=True)
    print("Oct 24 00:00:02.000 [err] something went wrong", flush=True)
    time.sleep(0.5)
    sys.exit(1)
time.sleep(600)
"""


def fake_socks(target_port, reply=0):
    """A SOCKS5 proxy on 127.0.0.1 that takes CONNECT to any NAME.onion
    and carries it to 127.0.0.1:target_port; with reply, refuses with it.
    Returns (port, the names asked for)."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    asked = []

    def exact(c, n):
        data = b""
        while len(data) < n:
            part = c.recv(n - len(data))
            if not part:
                raise ConnectionError
            data += part
        return data

    def pipe(a, b):
        try:
            while data := a.recv(65536):
                b.sendall(data)
        except OSError:
            pass
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def handle(c):
        try:
            exact(c, exact(c, 2)[1])   # the methods the client offers
            c.sendall(b"\x05\x00")      # no authentication
            _, cmd, _, kind = exact(c, 4)
            name = exact(c, exact(c, 1)[0]).decode() if kind == 3 else ""
            exact(c, 2)
            asked.append(name)
            if reply or not name.endswith(".onion"):
                c.sendall(bytes([5, reply or 4, 0, 1]) + b"\0" * 6)
                c.close()
                return
            up = socket.create_connection(("127.0.0.1", target_port))
            c.sendall(b"\x05\x00\x00\x01" + b"\0" * 6)
            threading.Thread(target=pipe, args=(c, up), daemon=True).start()
            pipe(up, c)
        except (OSError, ConnectionError):
            c.close()

    def serve():
        while True:
            try:
                c, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(c,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    return listener.getsockname()[1], asked


def relay_checks(tmp, h):
    print("tor relay and the client through Tor")
    lines = []
    relay = public.Relay("127.0.0.1", h.server.port, lines.append, peers=h.server.relayed)
    port = h.call(relay.start())
    servers = tmp / "tor-servers"
    servers.mkdir()
    key = proto.b64(h.server.public)
    home = tmp / "tor-guest"
    socks, asked = fake_socks(port)
    env = {"INTER_AGENT_COMM_TOR": f"127.0.0.1:{socks}"}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        r = chat(home, servers, "-n", "onion-a", "--address", f"{ONION}:{h.server.port}", "join", "--trust", key)
        req = h.request_of("onion-a")
        check("a guest joins by NAME.onion:PORT through this machine's Tor (SOCKS5)",
              r.returncode == 0 and req is not None and asked[-1:] == [ONION], r.stdout + r.stderr)
        check("its request is tagged TOR, with no address (Tor shows none)",
              req and req.via == "TOR" and req.ip == "", req and (req.via, req.ip))
        record = json.loads((servers / "testhost.json").read_text())
        check("it keeps the onion address for later calls",
              record.get("addresses") == [ONION] and record.get("port") == h.server.port, record)
        h.call(h.server.approve, req.key)
        r = chat(home, servers, "-n", "onion-a", "who")
        check("later calls go through Tor again; approved without a choice, it's a guest",
              r.returncode == 0 and re.search(r"onion-a .* TOR guest ", r.stdout) and len(asked) >= 2,
              r.stdout + r.stderr)
        joined = [m["text"] for m in h.call(lambda: list(h.server.history)) if "onion-a joined" in m["text"]]
        check("the join event says TOR, before the agent's own words", joined
              and joined[-1].startswith("onion-a joined by TOR as a guest, machine"), joined)
        r = chat(home, servers, "-n", "onion-a", "share", __file__)
        check("a Tor guest can't use the shared folder", r.returncode == 1 and "not guests" in r.stderr, r.stderr)
        h.call(h.server.set_guest, "onion-a", False)
        h.call(h.server.handover, proto.CONSOLE, "onion-a")
        r = chat(home, servers, "-n", "onion-a", "restart")
        check("made the host's own and the orchestrator, it still can't restart the server through Tor",
              r.returncode == 1 and "own machine" in r.stderr and not h.server.restart_pending, r.stdout + r.stderr)
        h.call(lambda: setattr(h.server, "orchestrator", ""))
        h.call(h.server.set_guest, "onion-a", True)
        r = chat(home, servers, "-n", "mine", "--address", f"{ONION}:{h.server.port}", "join", "--trust", key)
        check("mine and guest aren't agent names (/approve NAME mine)", r.returncode != 0
              and h.request_of("mine") is None, r.stderr)

        os.environ["INTER_AGENT_COMM_TOR"] = "127.0.0.1:1"
        r = chat(home, servers, "-n", "onion-a", "who")
        check("no Tor here: a clear error that says how to get it",
              r.returncode == 1 and "no Tor on this machine" in r.stderr and "apt install tor" in r.stderr, r.stderr)
        refusing, _ = fake_socks(port, reply=4)
        os.environ["INTER_AGENT_COMM_TOR"] = f"127.0.0.1:{refusing}"
        r = chat(home, servers, "-n", "onion-a", "who")
        check("an onion that doesn't answer: says the room may not be public now",
              r.returncode == 1 and "may not be public right now" in r.stderr, r.stderr)
        os.environ["INTER_AGENT_COMM_TOR"] = "nonsense"
        r = chat(home, servers, "-n", "onion-a", "who")
        check("a bad INTER_AGENT_COMM_TOR is named", r.returncode == 1 and "INTER_AGENT_COMM_TOR" in r.stderr,
              r.stderr)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    saved_max = public.MAX_OPEN_TOR
    public.MAX_OPEN_TOR = 2
    try:
        held = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(2)]
        time.sleep(0.3)
        extra = socket.create_connection(("127.0.0.1", port), timeout=5)
        extra.settimeout(5)
        try:
            closed = extra.recv(1) == b""
        except OSError:
            closed = True
        check("the relay holds at most MAX_OPEN_TOR streams, for all Tor guests together",
              closed and any("refused Tor guests: too many open connections" in x for x in lines), lines[-2:])
        for s in held + [extra]:
            s.close()
    finally:
        public.MAX_OPEN_TOR = saved_max
    h.call(relay.stop())
    check("a stopped relay takes no more", _refused(port))

    from inter_agent_comm import wss
    dead = socket.socket()   # accepts and hangs up, like a listener that closes before the server reads
    dead.bind(("127.0.0.1", 0))
    dead.listen(4)
    def hang_up():
        try:
            while True:
                dead.accept()[0].close()
        except OSError:
            pass
    threading.Thread(target=hang_up, daemon=True).start()
    table = {}
    saved_grace = wss.RELAYED_GRACE
    wss.RELAYED_GRACE = 0.3
    try:
        stale = public.Relay("127.0.0.1", dead.getsockname()[1], lambda line: None, peers=table)
        stale_port = h.call(stale.start())
        socket.create_connection(("127.0.0.1", stale_port), timeout=2).close()
        time.sleep(0.2)
        recorded = dict(table)
        time.sleep(0.6)
        check("an entry the server never took goes after a grace, so it can't tag a later local connection",
              recorded and not table, (recorded, table))
        h.call(stale.stop())
    finally:
        wss.RELAYED_GRACE = saved_grace
        dead.close()

    r = chat(tmp / "bad-onion", servers, "-n", "bad-onion", "--address", "аbc.onion:23232", "join")
    check("a non-ASCII onion address: a clean error, no traceback",
          r.returncode == 1 and "Traceback" not in r.stderr and "isn't an onion address" in r.stderr, r.stderr)


def _refused(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
        return False
    except OSError:
        return True


def tor_tunnel_checks(tmp, h):
    print("tor onion service")
    bin_dir = tmp / "tor-bin"
    bin_dir.mkdir()
    (bin_dir / "tor").write_text(FAKE_TOR.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
    (bin_dir / "tor").chmod(0o755)
    log = tmp / "fake-tor.log"
    saved_path = os.environ["PATH"]
    os.environ["PATH"] = f"{bin_dir}:{saved_path}"
    os.environ["FAKE_TOR_LOG"] = str(log)
    folder = tmp / "tor-home"
    stopped = []
    try:
        tor = public.Tor("127.0.0.1", h.server.port, lambda line: None, on_stop=lambda: stopped.append(1),
                         peers=h.server.relayed, folder=folder)
        address = h.call(tor.start())
        onion = (folder / "onion" / "hostname").read_text().strip()
        check("Tor on: the onion address, with the default port whatever the chat's is",
              address == f"{onion}:{proto.DEFAULT_PORT}" and tor.on, address)
        args = log.read_text().splitlines()[-1].split()
        check("its own tor: no system torrc, no SOCKS port, single onion service, PoW defenses on",
              args[args.index("-f") + 1] == args[args.index("--defaults-torrc") + 1] == str(folder / "torrc")
              and args[args.index("--SocksPort") + 1] == "0" and "--HiddenServiceSingleHopMode" in args
              and "--HiddenServicePoWDefensesEnabled" in args
              and f"127.0.0.1:{tor.relay.port}" in " ".join(args), args)
        check("its folders are private", all((p.stat().st_mode & 0o077) == 0
                                             for p in (folder, folder / "data", folder / "onion")))
        invite = public.invitation(address, h.server.public)
        check("the invitation joins by the onion address and says Tor is needed",
              f"chat --address {address} join --trust" in invite and "Tor must run on your machine" in invite,
              invite)
        h.call(tor.stop())
        check("off: tor and its relay are stopped, the key is kept", not tor.on and tor.relay is None
              and tor.kept_address() == onion)
        again = h.call(tor.start())
        check("on again: the same address", again == address, again)
        os.kill(tor.process.pid, 9)
        deadline = time.time() + 5
        while not stopped and time.time() < deadline:
            time.sleep(0.1)
        check("tor dying closes it and tells the TUI", stopped and not tor.on and tor.relay is None)
        os.environ["FAKE_TOR_LONG"] = "1"
        logged, ended = [], []
        noisy = public.Tor("127.0.0.1", h.server.port, logged.append, on_stop=lambda: ended.append(1),
                           peers=h.server.relayed, folder=tmp / "tor-noisy")
        try:
            h.call(noisy.start())
            deadline = time.time() + 10
            while not ended and time.time() < deadline:
                time.sleep(0.1)
        finally:
            os.environ.pop("FAKE_TOR_LONG")
        check("a line over the reader's limit is skipped: the next error is logged and the exit noticed",
              ended and any("something went wrong" in line for line in logged) and not noisy.on, logged)
        tor.keep = False
        fresh = h.call(tor.start())
        check("without keep: a new address each time", fresh != address
              and fresh.endswith(f".onion:{proto.DEFAULT_PORT}"), fresh)
        h.call(tor.stop())
        check("and its key is gone once it's off", tor.kept_address() is None)
        tor.keep = True
        new = h.call(tor.start())
        h.call(tor.stop())
        check("with keep again: that address is kept", tor.kept_address() == new.rpartition(":")[0], new)
        tor.forget()
        check("forget drops the kept key", tor.kept_address() is None)

        def running():   # no shell: its own command line would match
            return len(subprocess.run(["pgrep", "-f", "--", f"--DataDirectory {folder}/data"],
                                      capture_output=True, text=True).stdout.split())

        async def start_then_stop(after):
            task = asyncio.ensure_future(tor.start())
            for _ in range(after):
                await asyncio.sleep(0)
            await tor.stop()
            try:
                await task
                return "opened"
            except public.PublicError as e:
                return str(e)
        result = h.call(start_then_stop(0))   # the stop comes before the start begins: it opens
        check("a stop before the start begins doesn't hold it closed", result == "opened" and tor.on, result)
        h.call(tor.stop())
        time.sleep(0.3)
        for after in (1, 2, 5, 50):
            result = h.call(start_then_stop(after))
            time.sleep(0.3)
            check(f"stopped {after} steps into a start: it says so, and nothing is left open",
                  "closed while it started" in result and not tor.on and tor.relay is None
                  and tor.process is None and running() == 0, (result, tor.relay, running()))
    finally:
        os.environ["PATH"] = saved_path
    os.environ["PATH"] = f"{bin_dir}:{saved_path}"
    blocked = tmp / "tor-blocked"
    blocked.write_text("a file where tor's folder should be")
    stuck = public.Tor("127.0.0.1", h.server.port, lambda line: None, peers=h.server.relayed, folder=blocked)
    try:
        h.call(stuck.start())
        err = ""
    except public.PublicError as e:
        err = str(e)
    finally:
        os.environ["PATH"] = saved_path
    check("a tor folder that can't be made: a clear error, the relay closed, nothing starting",
          "couldn't prepare tor's files" in err and stuck.relay is None and not stuck.on, err)

    os.environ["PATH"] = str(tmp / "empty-bin")
    try:
        h.call(tor.start())
        err = ""
    except public.PublicError as e:
        err = str(e)
    finally:
        os.environ["PATH"] = saved_path
    check("without tor: a clear error, nothing left running", "isn't installed" in err and tor.relay is None, err)


def lan_checks(tmp):
    print("LAN on and off")
    lan = [a for a in server.local_addresses() if re.fullmatch(r"[\d.]+", a) and not a.startswith("127.")]
    if not lan:
        print("  skip: this machine has no LAN address")
        return
    address = lan[0]
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()

    def call(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop).result(10)

    def sync(fn, *args):
        async def run():
            return fn(*args)
        return call(run())

    async def make():
        chat_server = server.ChatServer("lantest", 0, tmp / "lan-home", None, address, None, tmp / "lan-files",
                                        trash=tmp / "lan-trash", lan=True)
        await chat_server.start()
        return chat_server
    srv = call(make())

    def reaches(host):
        try:
            socket.create_connection((host, srv.port), timeout=2).close()
            return True
        except OSError:
            return False
    check("--listen ADDRESS: the LAN address, and 127.0.0.1 for this machine and the tunnels",
          srv.lan and reaches(address) and reaches("127.0.0.1"))
    servers = tmp / "lan-servers"
    servers.mkdir()
    (servers / "lantest.json").write_text(json.dumps({"name": "lantest", "port": srv.port, "addresses": [address],
                                                      "public_key": proto.b64(srv.public)}))
    home = tmp / "lan-agent"
    saved_same = server.same_machine
    server.same_machine = lambda writer, ip: False   # this agent passes for one on another machine
    try:
        chat(home, servers, "-n", "far-lan", "join")
        req = next(r for r in srv.requests.values() if r.name == "far-lan")
        sync(srv.approve, req.key)
        check("an agent from another machine is tagged LAN; approved without a choice, a guest",
              req.via == "LAN" and srv.by_name("far-lan") and srv.by_name("far-lan").guest is True)
        chat(home, servers, "-n", "far-lan", "read")
        p = chat_bg(home, servers, "-n", "far-lan", "wait")
        time.sleep(1)
        call(srv.set_lan(False))
        try:
            out, err = p.communicate(timeout=10)
            cut = p.returncode != 0
        except Exception:
            p.kill()
            cut, err = False, "still waiting"
        check("LAN off: the LAN address is closed, this machine still gets in, and its waits are cut",
              not srv.lan and not reaches(address) and reaches("127.0.0.1") and cut, err)
        loopback = srv.listeners["127.0.0.1"]

        async def open_then_close():
            await asyncio.gather(srv.set_lan(True), srv.set_lan(False))
        call(open_then_close())
        check("Turn on then at once Turn off: closed, and nothing listens on the LAN address",
              not srv.lan and list(srv.listeners) == ["127.0.0.1"] and not reaches(address), list(srv.listeners))
        call(srv.set_lan(True))
        check("LAN on again", srv.lan and reaches(address))
        call(srv.set_lan(False))
        call(srv.set_lan(True))
        check("opening and closing LAN on an address leaves the 127.0.0.1 listener as it is",
              srv.listeners["127.0.0.1"] is loopback)
    finally:
        server.same_machine = saved_same
    call(srv.stop())

    async def make_wild():
        chat_server = server.ChatServer("wildtest", 0, tmp / "wild-home", None, None, None, tmp / "wild-files",
                                        trash=tmp / "wild-trash", lan=lan)
        await chat_server.start()
        return chat_server
    lan = False
    srv = call(make_wild())
    check("by default a server starts with LAN closed: only 127.0.0.1",
          not srv.lan and not reaches(address) and reaches("127.0.0.1"))
    call(srv.stop())
    lan = True
    srv = call(make_wild())
    on = reaches(address) and reaches("127.0.0.1") and len(srv.listeners) == 1
    call(srv.set_lan(False))
    off = not reaches(address) and reaches("127.0.0.1")
    call(srv.set_lan(True))
    check("all interfaces: one listener that takes 127.0.0.1 too; off leaves 127.0.0.1 alone",
          on and off and reaches(address))
    call(srv.stop())

    async def make_local():
        chat_server = server.ChatServer("looptest", 0, tmp / "loop-home", None, "127.0.0.1", None,
                                        tmp / "loop-files", trash=tmp / "loop-trash")
        await chat_server.start()
        return chat_server
    srv = call(make_local())
    call(srv.set_lan(True))
    check("--listen 127.0.0.1 has no LAN to open", not srv.lan and not srv.lan_possible)
    saved_same = server.same_machine
    server.same_machine = lambda writer, ip: False   # as if forwarded to 127.0.0.1 from another machine
    try:
        probe = socket.create_connection(("127.0.0.1", srv.port), timeout=2)
        probe.settimeout(2)
        try:
            hung_up = probe.recv(1) == b""
        except socket.timeout:
            hung_up = False   # the server stays silent to a client that hasn't spoken: not hung up on
        except OSError:
            hung_up = True
        probe.close()
    finally:
        server.same_machine = saved_same
    check("while LAN is closed, a connection from another machine is hung up on, even through 127.0.0.1",
          hung_up)
    call(srv.stop())

    async def make_v6():
        chat_server = server.ChatServer("v6test", 0, tmp / "v6-home", None, "::", None, tmp / "v6-files",
                                        trash=tmp / "v6-trash", lan=True)
        await chat_server.start()
        return chat_server
    try:
        srv = call(make_v6())
    except OSError:
        print("  skip: no IPv6 here")
    else:
        check("--listen :: (IPv6 only): 127.0.0.1 is still there for the tunnels' relays", reaches("127.0.0.1"))
        call(srv.stop())


def trust_checks(tmp, h):
    print("mine or guest")
    srv = h.server

    def request(name, via, ip="192.0.2.20"):
        public_key = os.urandom(32)
        h.call(srv._join, proto.b64(public_key), public_key, {"name": name}, ip, via)
        return proto.b64(public_key)
    a, b, c = request("trust-a", server.LAN), request("trust-b", server.LOCAL, "127.0.0.1"), \
        request("trust-c", server.CF, "203.0.113.1")
    h.call(srv.approve, a)
    h.call(srv.approve, b)
    h.call(srv.approve, c, False)
    check("without a choice: LAN is a guest, LOCAL the host's own; the host's choice wins over the way in",
          srv.members[a].guest is True and srv.members[b].guest is False and srv.members[c].guest is False)
    legacy = srv.members[b]
    legacy.guest = None
    legacy.via = server.TOR
    tor_legacy = h.call(srv._guest, legacy)
    legacy.via = server.LAN
    check("approved before the choice existed: a guest only through a tunnel",
          tor_legacy and not h.call(srv._guest, legacy))
    member = srv.members[b]
    member.via, member.muted, member.seen = server.TOR, True, time.time()

    class Panel:   # the bits of ServerApp that connected_through reads
        chat = srv
    here = h.call(server.ServerApp.connected_through, Panel(), server.TOR)
    check("a muted member is still connected: closing its way in warns about it", "trust-b" in here, here)
    member.via, member.muted = server.LAN, False
    h.call(srv.set_guest, "trust-b", True)
    events = [m["text"] for m in h.call(lambda: list(srv.history))[-3:]]
    check("the host can change it; the chat hears", srv.members[b].guest is True
          and any("made trust-b a guest" in e for e in events), events)
    saved = json.loads((srv.home / "members.json").read_text())
    check("it's saved with the members", saved[b].get("guest") is True and saved[c].get("guest") is False)
    for key in (a, b, c):
        h.call(srv.kick, srv.members[key].name)


async def tui_access_test(tmp, bin_dir):
    """The Access window with Tor, Guest focused for strangers, and --resume."""
    import argparse

    os.environ["INTER_AGENT_COMM_HOME"] = str(tmp / "acc-home")
    os.environ["INTER_AGENT_COMM_SERVERS"] = str(tmp / "acc-servers")
    os.environ["INTER_AGENT_COMM_FILES"] = str(tmp / "acc-files")
    os.environ["INTER_AGENT_COMM_TRASH"] = str(tmp / "acc-trash")
    os.environ["FAKE_TOR_LOG"] = str(tmp / "acc-tor.log")
    saved_path, saved_copy = os.environ["PATH"], server.system_copy
    os.environ["PATH"] = f"{bin_dir}:{saved_path}"
    copied = []
    server.system_copy = lambda text: copied.append(text) or "test"
    args = argparse.Namespace(name="acctui", port=0, listen="127.0.0.1", orchestrator="stranger",
                              no_transcript=True, transcripts=None, resume=False)
    try:
        app = server.ServerApp(args)
        async with app.run_test(size=(150, 45)) as pilot:
            written = []
            write = app.write_line
            app.write_line = lambda text: (written.append(str(text)), write(text))
            key = os.urandom(32)
            app.chat._join(proto.b64(key), key, {"name": "stranger", "host": "laptop.webshop"}, "192.0.2.30",
                           server.LAN)
            app.refresh_tables()
            await pilot.pause()
            row = [str(cell) for cell in app.query_one("#requests").get_row_at(0)]
            check("the Requests table shows the way in", "LAN" in row, row)
            app.request_dialog(proto.b64(key))
            await pilot.pause()
            check("a request from elsewhere: Guest has the focus, so Enter never makes a stranger yours",
                  app.screen.focused is not None and app.screen.focused.id == "guest"
                  and "Came in by LAN from 192.0.2.30" in "\n".join(app.screen.lines), app.screen.focused)
            check("the window says when it's the lead (--orchestrator), which can't lead as a guest",
                  "stranger is the lead (--orchestrator): as a guest it can't lead." in app.screen.lines)
            await pilot.press("enter")
            await pilot.pause()
            member = app.chat.by_name("stranger")
            check("approved as a guest, the lead gets a warning in the log",
                  any("stranger is the lead (--orchestrator), approved as a guest" in w for w in written),
                  written[-2:])
            key2 = os.urandom(32)
            app.chat._join(proto.b64(key2), key2, {"name": "second"}, "192.0.2.31", server.LAN)
            app.refresh_tables()
            app.command("/approve mine")
            check("/approve mine without a name or code approves nobody (rows shift)",
                  app.chat.by_name("second") is None)
            app.command("/approve guest")
            check("/approve guest: the selected request, as a guest",
                  app.chat.by_name("second") and app.chat.by_name("second").guest is True)
            app.command("/mine")
            check("/mine without a name changes nobody", app.chat.by_name("second").guest is True)
            app.chat.kick("second")
            row = [str(cell) for cell in app.query_one("#members").get_row_at(1)]
            check("approved as a guest; Members shows LAN and guest", member and member.guest is True
                  and "LAN guest" in row, row)
            app.member_dialog(member.key)
            await pilot.pause()
            check("its window offers Make mine, not Make orchestrator",
                  app.screen.query("#mine") and not app.screen.query("#orchestrator"))
            await pilot.click("#mine")
            await pilot.pause()
            check("Make mine makes it the host's own", member.guest is False)

            await pilot.click("#share")
            await pilot.pause()
            check("Share, top right, opens the window", isinstance(app.screen, server.AccessDialog), app.screen)
            check("with the X focused: Enter closes it and never opens a way in",
                  app.screen.focused is not None and app.screen.focused.id == "close", app.screen.focused)
            await pilot.click("#toggle-TOR")
            for _ in range(80):
                if app.tunnels["TOR"] and app.tunnels["TOR"].on:
                    break
                await asyncio.sleep(0.1)
            await pilot.pause()
            tor = app.tunnels["TOR"]
            check("Turn on opens Tor, shows the address and copies a Tor invitation",
                  tor and tor.on and tor.address in app.access_state("TOR")[0]
                  and copied and "Tor must run on your machine" in copied[-1], copied[-1:])
            check("New address is off while Tor is on", app.screen.query_one("#tor-new").disabled)
            address = tor.address
            closing = asyncio.ensure_future(app.tunnel_off("TOR"))
            await asyncio.sleep(0)
            closing_shown = app.access_state("TOR")
            await app.tunnel_on("TOR")
            check("while Tor closes it can't be opened again (they'd share its files): the window says closing",
                  app.closing["TOR"] is tor and app.tunnels["TOR"] is None
                  and closing_shown[0].endswith("closing ...") and closing_shown[3] is False, closing_shown)
            await closing
            check("closed, it can be opened again", app.closing["TOR"] is None)
            await app.tunnel_on("TOR")
            tor = app.tunnels["TOR"]
            check("at the same address", tor and tor.address == address, tor and tor.address)
            await pilot.click("#tor-keep")
            await pilot.pause()
            check("unticking Same address: kept in access.json, and the key goes when Tor closes",
                  not app.tor_keep and tor.keep is False and
                  json.loads((tmp / "acc-home" / "access.json").read_text())["tor_keep"] is False)
            await pilot.click("#tor-keep")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
        check("quitting closes Tor", tor.process is None)
        saved = json.loads((tmp / "acc-home" / "access.json").read_text())
        check("access.json held Tor as open", saved["on"] == ["TOR"], saved)

        args.resume = True
        app = server.ServerApp(args)
        async with app.run_test(size=(150, 45)) as pilot:
            for _ in range(80):
                if app.tunnels["TOR"] and app.tunnels["TOR"].on:
                    break
                await asyncio.sleep(0.1)
            check("--resume (so /restart) opens Tor again, at the same address",
                  app.tunnels["TOR"] and app.tunnels["TOR"].address == address,
                  (app.tunnels["TOR"] and app.tunnels["TOR"].address, address))
            args.resume = False
        app = server.ServerApp(args)
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause()
            check("a new start opens no tunnel", not any(app.tunnels.values()))
    finally:
        os.environ["PATH"] = saved_path
        server.system_copy = saved_copy


async def tui_lan_test(tmp):
    """LAN at start: closed unless --lan; --resume keeps it as it was."""
    import argparse

    os.environ["INTER_AGENT_COMM_HOME"] = str(tmp / "lan-tui-home")
    os.environ["INTER_AGENT_COMM_SERVERS"] = str(tmp / "lan-tui-servers")
    args = argparse.Namespace(name="lantui", port=0, listen=None, orchestrator="", no_transcript=True,
                              transcripts=None, resume=False, lan=False)
    app = server.ServerApp(args)
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        record = json.loads((tmp / "lan-tui-servers" / "lantui.json").read_text())
        check("a new start: only this machine, and the server file says 127.0.0.1",
              not app.chat.lan and record["addresses"] == ["127.0.0.1"]
              and "LAN" not in str(app.query_one("#access-label").render()), record)
        await app.lan_set(True)
        record = json.loads((tmp / "lan-tui-servers" / "lantui.json").read_text())
        check("opening LAN from the TUI: the server file gets the LAN addresses",
              app.chat.lan and record["addresses"] != ["127.0.0.1"]
              and "LAN" in str(app.query_one("#access-label").render()), record)
    args.resume = True
    app = server.ServerApp(args)
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        check("--resume opens LAN again when it was open", app.chat.lan)
    (tmp / "lan-tui-home" / "access.json").unlink()
    app = server.ServerApp(args)
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        check("--resume after a server older than access.json: LAN stays open, as it always was", app.chat.lan)
    args.resume, args.lan = False, True
    app = server.ServerApp(args)
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        check("--lan opens it at start", app.chat.lan)
    lan = [a for a in server.local_addresses() if re.fullmatch(r"[\d.]+", a) and not a.startswith("127.")]
    if lan:
        args.lan, args.listen = False, lan[0]
        app = server.ServerApp(args)
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()
            check("--listen ADDRESS opens LAN on it at start, as before 0.2", app.chat.lan)
        args.listen = None


def run_checks():
    before = len(failures)
    tmp = Path(tempfile.mkdtemp(prefix="inter-agent-comm_access_"))
    h = Harness(tmp)
    relay_checks(tmp, h)
    tor_tunnel_checks(tmp, h)
    trust_checks(tmp, h)
    h.stop()
    lan_checks(tmp)
    print("access in the TUI")
    asyncio.run(tui_access_test(tmp, tmp / "tor-bin"))
    asyncio.run(tui_lan_test(tmp))
    return len(failures) == before


if __name__ == "__main__":
    ok = run_checks()
    print("All passed." if ok else f"{len(failures)} failed")
    sys.exit(0 if ok else 1)
