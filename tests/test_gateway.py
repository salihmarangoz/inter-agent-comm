#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Salih Marangoz <salih285@gmail.com>
"""Tests for the WebSocket gateway (wss.py): agents join, talk and share a
file through it as they would through a Cloudflare tunnel, and the
gateway's per-address limits hold. Runs on localhost only; tests/run.py calls
run_checks().

  .venv/bin/python tests/test_gateway.py
"""

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # the repo, for inter_agent_comm
from inter_agent_comm import wss  # noqa: E402
from run import Harness, chat, chat_bg, check, failures  # noqa: E402


def raw_ws(url, ip=None):
    """Open a WebSocket as cloudflared would, with the visitor's address."""
    from websockets.sync.client import connect
    headers = {"CF-Connecting-IP": ip} if ip else None
    return connect(url, additional_headers=headers, open_timeout=3, compression=None, legacy=True)


def status_of(url, ip=None):
    """The HTTP status of a WebSocket handshake; None if nothing answers."""
    from websockets.exceptions import InvalidStatus
    try:
        raw_ws(url, ip).close()
        return 101
    except InvalidStatus as e:
        return e.response.status_code
    except OSError:
        return None


FAKE_CLOUDFLARED = """#!/bin/sh
echo "2026-10-08T00:00:00Z INF |  https://fake-tunnel-name.trycloudflare.com  |" >&2
exec sleep 600
"""


def public_checks(tmp, h):
    """/public with a stand-in for cloudflared: the gateway, what it shows a
    stranger, the invitation, a guest's first contact, and stopping."""
    import urllib.error
    import urllib.request

    from inter_agent_comm import proto
    from inter_agent_comm import public

    print("public")
    bin_dir = tmp / "fake-bin"
    bin_dir.mkdir()
    (bin_dir / "cloudflared").write_text(FAKE_CLOUDFLARED)
    (bin_dir / "cloudflared").chmod(0o755)
    saved_path = os.environ["PATH"]
    lines, stopped = [], []
    pub = public.Cloudflare("127.0.0.1", h.server.port, lines.append, on_stop=lambda: stopped.append(1))
    os.environ["PATH"] = f"{bin_dir}:{saved_path}"
    try:
        url = h.call(pub.start())
    finally:
        os.environ["PATH"] = saved_path
    check("/public on: the tunnel's address", url == "wss://fake-tunnel-name.trycloudflare.com/"
          and pub.url == "https://fake-tunnel-name.trycloudflare.com", url)
    invite = public.invitation(url, h.server.public)
    fp = proto.fingerprint(h.server.public)
    gets = [line.strip() for line in invite.splitlines() if "git clone" in line or "curl" in line]
    check("the invitation gets the client from the public repo and only shows GUEST.md",
          gets == [f"git clone {public.REPO_URL} && cat inter-agent-comm/docs/GUEST.md"], invite)
    key = proto.b64(h.server.public)
    check("joining is its own step, through the tunnel's wss address, trusting the server's whole key",
          f"inter-agent-comm/chat --address wss://fake-tunnel-name.trycloudflare.com/ join --trust {key}" in invite
          and not any("chat " in line for line in gets) and "Tor" not in invite, invite)
    check("the invitation shows the key's emojis", proto.emojis(h.server.public) in invite, invite)
    port = pub.relay.port

    def http(path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
    answers = [http(path) for path in ("/", "/inter-agent-comm.tar.gz", "/robots.txt")]
    check("to a browser or a scanner the tunnel is a plain 404 that names nothing",
          all(code == 404 and b"inter" not in body.lower() and b"websocket" not in body.lower()
              for code, body in answers), answers)
    check("a WebSocket on another path is a 404 too", status_of(f"ws://127.0.0.1:{port}/x") == 404)

    def raw(request):
        """The status line and body of a hand-made request."""
        import socket
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(request)
            data = b""
            try:
                while chunk := sock.recv(4096):
                    data += chunk
            except OSError:
                pass
        head, _, body = data.partition(b"\r\n\r\n")
        return head.split(b"\r\n")[0], body
    ws_head = b"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
    probes = {"a URL over 8 KB": b"GET /" + b"a" * 9000 + b" HTTP/1.1\r\nHost: x\r\n\r\n",
              "a header over 8 KB": b"GET / HTTP/1.1\r\nHost: x\r\nX: " + b"a" * 9000 + b"\r\n\r\n",
              "Upgrade without Connection": b"GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n\r\n",
              "a handshake without a key": b"GET / HTTP/1.1\r\nHost: x\r\n" + ws_head + b"\r\n",
              "two Upgrade headers": b"GET / HTTP/1.1\r\nHost: x\r\n" + ws_head + b"Upgrade: websocket\r\n"
                                     b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"}
    answers = {label: raw(request) for label, request in probes.items()}
    check("requests the WebSocket library would refuse with its own page get the plain 404 too",
          all(line.startswith(b"HTTP/1.1 404") and body == b"Not found\n" for line, body in answers.values()),
          answers)
    guest = tmp / "guest-home"
    env = dict(os.environ, INTER_AGENT_COMM_HOME=str(guest), INTER_AGENT_COMM_SESSION="none")
    env.pop("INTER_AGENT_COMM_SERVERS", None)

    def guest_chat(*args):
        return subprocess.run([*proto.command("chat"), "-n", "guest-x", "--address",
                               f"ws://127.0.0.1:{port}/", *args], env=env, capture_output=True, text=True, timeout=30)
    r = guest_chat("join")
    check("a guest without --trust sees the key through the tunnel and stops", r.returncode == 1
          and proto.key_label(h.server.public) in r.stderr and not h.request_of("guest-x"), r.stderr)
    r = guest_chat("join", "--trust", key)
    saved = guest / "servers" / "testhost.json"
    record = json.loads(saved.read_text()) if saved.exists() else {}
    check("with the invitation's key the guest joins through the gateway",
          r.returncode == 0 and h.request_of("guest-x"), r.stdout + r.stderr)
    check("and keeps the server's wss address, no LAN address",
          record.get("urls") == [f"ws://127.0.0.1:{port}/"] and record.get("addresses") == [], record)
    check("join prints the server key, as in the invitation",
          f"Server key: {proto.key_label(h.server.public)}" in r.stdout, r.stdout)
    r = subprocess.run([*proto.command("chat"), "-n", "guest-x", "who"], env=env,
                       capture_output=True, text=True, timeout=30)
    check("later calls find the server by its saved wss address", "Waiting for the host's approval" in r.stderr,
          r.stdout + r.stderr)

    pid = pub.process.pid
    os.kill(pid, 9)
    deadline = time.time() + 5
    while not stopped and time.time() < deadline:
        time.sleep(0.1)
    check("cloudflared dying ends /public and tells the TUI", stopped and not pub.on and pub.relay is None,
          lines[-2:])
    check("the gateway port is closed then", status_of(f"ws://127.0.0.1:{port}/") is None)

    os.environ["PATH"] = f"{bin_dir}:{saved_path}"
    try:
        h.call(pub.start())
    finally:
        os.environ["PATH"] = saved_path
    pid = pub.process.pid
    h.call(pub.stop())
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    check("/public off stops cloudflared and the gateway", not alive and pub.relay is None and not stopped[1:])

    os.environ["PATH"] = str(tmp / "empty-bin")
    try:
        h.call(pub.start())
        err = ""
    except public.PublicError as e:
        err = str(e)
    finally:
        os.environ["PATH"] = saved_path
    check("without cloudflared: a clear error, nothing left running",
          "isn't installed" in err and pub.relay is None, err)


def join_limit_checks(h):
    """T6: a flood of join requests can't fill the TUI."""
    from inter_agent_comm import server
    srv = h.server

    def join(name, ip, via=None):
        public = os.urandom(32)
        via = via or (server.LOCAL if ip == "127.0.0.1" else server.LAN)
        return h.call(srv._join, server.proto.b64(public), public, {"name": name}, ip, via)

    print("join limits")
    srv_events = len(h.events)
    replies = [join(f"lan-{i}", "192.0.2.50") for i in range(server.MAX_PENDING_PER_IP + 1)]
    check("one LAN address gets MAX_PENDING_PER_IP waiting requests, then 'busy'",
          [r.get("state") for r in replies[:-1]] == ["pending"] * server.MAX_PENDING_PER_IP and
          replies[-1].get("code") == "busy", replies[-1])
    local = [join(f"here-{i}", "127.0.0.1") for i in range(server.MAX_PENDING_PER_IP + 1)]
    check("this machine (the gateway's way in) isn't limited per address",
          all(r.get("state") == "pending" for r in local), local[-1])
    n = server.MAX_PENDING - server.LOCAL_RESERVED - len(srv.requests)
    fill = [join(f"fill-{i}", f"198.51.100.{i}") for i in range(n)]
    over = join("one-too-many", "198.51.100.200")
    check("other addresses get all but LOCAL_RESERVED slots, then 'busy'",
          all(r.get("state") == "pending" for r in fill) and over.get("code") == "busy" and
          len(srv.requests) == server.MAX_PENDING - server.LOCAL_RESERVED, (over, len(srv.requests)))
    mine = [join(f"mine-{i}", "127.0.0.1") for i in range(server.LOCAL_RESERVED)]
    check("the reserved slots are this machine's, up to MAX_PENDING",
          all(r.get("state") == "pending" for r in mine) and join("mine-x", "127.0.0.1").get("code") == "busy"
          and len(srv.requests) == server.MAX_PENDING, len(srv.requests))
    check("the TUI hears about refusals once, not per attempt",
          h.events[srv_events:].count("error") == 1, h.events[srv_events:])
    h.call(lambda: srv.requests.clear())
    outside = [join(f"out-{i}", f"203.0.113.{i}", server.CF) for i in range(server.MAX_PENDING_PUBLIC + 1)]
    check("requests through the tunnels have their own cap",
          all(r.get("state") == "pending" for r in outside[:-1]) and outside[-1].get("code") == "busy",
          outside[-1])
    check("so local agents still find room", join("local-after", "127.0.0.1").get("state") == "pending")
    h.call(lambda: srv.requests.clear())
    same = [join(f"same-{i}", "198.51.100.77", server.CF) for i in range(server.MAX_PENDING_PER_IP + 1)]
    check("one visitor through Cloudflare is limited like a LAN address", same[-1].get("code") == "busy", same[-1])
    check("and apart from the same address on the LAN", join("same-lan", "198.51.100.77").get("state") == "pending")
    h.call(lambda: srv.requests.clear())
    tor = [join(f"tor-{i}", "", server.TOR) for i in range(server.MAX_PENDING_PER_IP + 1)]
    check("Tor shows no address: all its requests count as one", tor[-1].get("code") == "busy"
          and "from TOR" in tor[-1].get("message", ""), tor[-1])
    mixed = [join("tor-x", "", server.TOR), join("cf-x", "203.0.113.9", server.CF)]
    check("the tunnels share their cap, not their per-address count",
          mixed[0].get("code") == "busy" and mixed[1].get("state") == "pending", mixed)
    h.call(lambda: srv.requests.clear())
    v6 = [join(f"six-{i}", f"2001:db8:1:2::{i + 1}") for i in range(server.MAX_PENDING_PER_IP + 1)]
    check("IPv6 addresses from one /64 count as one address", v6[-1].get("code") == "busy", v6[-1])
    h.call(lambda: srv.requests.clear())
    fill = [join(f"fill-{i}", f"198.51.100.{i}") for i in range(server.MAX_PENDING - server.LOCAL_RESERVED)]
    old = time.time() - server.PENDING_TTL - 1
    h.call(lambda: [setattr(r, "seen", old) for r in srv.requests.values() if r.name.startswith("fill-")])
    check("stale requests make room", join("after-stale", "198.51.100.201").get("state") == "pending")
    check("a stale request doesn't hold its name", join("fill-0", "198.51.100.202").get("state") == "pending")
    h.call(lambda: srv.requests.clear())
    home = h.tmp / "limits-home"
    chat(home, h.servers, "-n", "patient", "join")
    patient = h.request_of("patient")
    h.call(lambda: setattr(patient, "seen", old))
    chat(home, h.servers, "-n", "patient", "wait", "--timeout", "1")
    check("an agent waiting for approval keeps its request alive",
          time.time() - h.request_of("patient").seen < 10)
    h.call(lambda: srv.requests.clear())


async def tui_public_test(tmp, bin_dir):
    """The Access window in the TUI: Cloudflare on and off, with the stand-in cloudflared."""
    import argparse

    from inter_agent_comm import server
    os.environ["INTER_AGENT_COMM_HOME"] = str(tmp / "pub-tui-home")
    os.environ["INTER_AGENT_COMM_SERVERS"] = str(tmp / "pub-tui-servers")
    os.environ["INTER_AGENT_COMM_FILES"] = str(tmp / "pub-tui-files")
    os.environ["INTER_AGENT_COMM_TRASH"] = str(tmp / "pub-tui-trash")
    saved_path, saved_copy = os.environ["PATH"], server.system_copy
    os.environ["PATH"] = f"{bin_dir}:{saved_path}"
    copied = []
    server.system_copy = lambda text: copied.append(text) or "test"   # leave the real clipboard alone
    args = argparse.Namespace(name="pubtui", port=0, listen="127.0.0.1", publish=False, orchestrator="", no_transcript=True,
                              transcripts=None)
    app = server.ServerApp(args)
    pid = None
    try:
        async with app.run_test(size=(150, 40)) as pilot:
            written = []
            write = app.write_line
            app.write_line = lambda text: (written.append(str(text)), write(text))
            label = str(app.query_one("#access-label").render())
            check("the side panel shows the ways in: --listen 127.0.0.1 is this machine only",
                  "Open to: LOCAL" in label and "LAN" not in label, label)
            app.query_one("#input").value = "/public on"
            await pilot.press("enter")
            await pilot.pause()
            check("/public on opens the Share window", isinstance(app.screen, server.AccessDialog), app.screen)
            check("LAN can't be opened when the server listens on 127.0.0.1",
                  app.screen.query_one("#toggle-LAN").disabled)
            await pilot.click("#toggle-CF")
            for _ in range(50):
                if app.tunnels["CF"] and app.tunnels["CF"].on:
                    break
                await asyncio.sleep(0.1)
            await pilot.pause()
            check("Turn on opens the tunnel and says so",
                  app.tunnels["CF"] and app.tunnels["CF"].on and "PUBLIC" in app.sub_title and
                  any("CF is open at https://fake-tunnel-name.trycloudflare.com" in w for w in written), written[-3:])
            check("and copies the invitation", copied and copied[-1].startswith("You're invited") and
                  "--address wss://fake-tunnel-name.trycloudflare.com/ join --trust" in copied[-1], copied)
            check("its gateway reports tunnelled connections to the server",
                  app.tunnels["CF"].relay.peers is app.chat.relayed)
            check("the window and the side panel show it open",
                  "open at https://fake-tunnel-name" in str(app.screen.query_one("#state-CF").render())
                  and "CF" in str(app.query_one("#access-label").render()))
            check("the ways in are saved for --resume",
                  json.loads((tmp / "pub-tui-home" / "access.json").read_text())["on"] == ["CF"])
            await pilot.press("escape")
            await pilot.pause()
            port = app.tunnels["CF"].relay.port
            app.query_one("#input").value = "/public off"
            await pilot.press("enter")
            for _ in range(50):
                if app.tunnels["CF"] is None and "PUBLIC" not in app.sub_title:
                    break
                await asyncio.sleep(0.1)
            check("/public off closes it", app.tunnels["CF"] is None and "PUBLIC" not in app.sub_title and
                  await asyncio.to_thread(status_of, f"ws://127.0.0.1:{port}/") is None)
            app.query_one("#input").value = "/access"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.click("#toggle-CF")
            for _ in range(50):
                if app.tunnels["CF"] and app.tunnels["CF"].on:
                    break
                await asyncio.sleep(0.1)
            pid = app.tunnels["CF"].process.pid
    finally:
        os.environ["PATH"] = saved_path
        server.system_copy = saved_copy
    try:
        os.kill(pid, 0)
        alive = True
    except (ProcessLookupError, TypeError):
        alive = False
    check("quitting the server stops cloudflared", pid and not alive)


def busy_wait_check(h):
    """A waiting agent gets its keepalive pings even while other messages
    keep waking the server's wait loop (it once pinged only after a quiet
    spell, and in a busy room the client timed out)."""
    import socket as socket_module
    import threading

    from inter_agent_comm import proto
    print("keepalive")
    home = h.tmp / "busy-home"
    chat(home, h.servers, "-n", "busy-waiter", "join")
    h.call(h.server.approve, h.request_of("busy-waiter").key)
    chat(home, h.servers, "-n", "busy-waiter", "read")   # takes the welcome, so the wait below waits
    saved = proto.PING_EVERY
    proto.PING_EVERY = 1   # the server reads it on every loop
    stop = threading.Event()

    def chatter():
        while not stop.is_set():
            h.call(h.server._post, "someone", "busy room, nobody named")
            time.sleep(0.2)

    threading.Thread(target=chatter, daemon=True).start()
    pings = 0
    try:
        record = json.loads((h.servers / "testhost.json").read_text())
        client = proto.connect("127.0.0.1", h.server.port, proto.load_or_create_key(home / "agents" /
                               "busy-waiter.key"), proto.unb64(record["public_key"]),
                               {"name": "busy-waiter"})
        client.sock.settimeout(1)
        client.send({"op": "wait", "mentions": True, "timeout": 0})
        end = time.time() + 4
        while time.time() < end:
            try:
                pings += client.recv().get("type") == "ping"
            except socket_module.timeout:
                pass
        client.close()
    finally:
        stop.set()
        proto.PING_EVERY = saved
    check("a waiting agent is pinged while others' messages keep coming", pings >= 3, pings)


def run_checks():
    before = len(failures)
    tmp = Path(tempfile.mkdtemp(prefix="inter-agent-comm_gw_"))
    h = Harness(tmp)
    lines = []
    gateway = wss.Gateway("127.0.0.1", h.server.port, log=lines.append, trace=lines.append,
                          peers=h.server.relayed)
    port = h.call(gateway.start())
    url = f"ws://127.0.0.1:{port}/"
    # Only the URL in the server file: every connection below goes through the gateway.
    servers = tmp / "gw-servers"
    servers.mkdir()
    record = json.loads((h.servers / "testhost.json").read_text())
    record.update(addresses=[], urls=[url])
    (servers / "testhost.json").write_text(json.dumps(record))
    a, b = tmp / "gw-a", tmp / "gw-b"

    print("gateway")
    r = chat(a, servers, "-n", "far-a", "join", "--about", "joined through the gateway")
    code = re.search(r"Pairing code: (\d{3}-\d{3})", r.stdout)
    req = h.request_of("far-a")
    check("join through the gateway: same pairing code on the server",
          r.returncode == 0 and code and req and req.code == code.group(1), r.stdout + r.stderr)
    p = chat_bg(a, servers, "-n", "far-a", "wait")
    time.sleep(1)
    check("a request through the gateway is tagged CF, with the visitor's address",
          req and req.via == "CF" and req.ip == "127.0.0.1", req and (req.via, req.ip))
    h.call(h.server.approve, req.key)
    out, err = p.communicate(timeout=10)
    check("wait returns on approval through the gateway", "The host approved you" in out, out + err)
    row = next((r for r in h.call(h.server.who) if r["name"] == "far-a"), {})
    check("who tags it CF, and a guest: approved without a choice, a tunnel's agent is one",
          row.get("via") == "CF" and row.get("guest") is True, row)
    check("and keeps (guest) after its machine, for clients that show only that",
          row.get("host", "").endswith(" (guest)"), row)
    r = chat(a, servers, "-n", "far-a", "who")
    check("the client's who shows the tag", re.search(r"far-a .* CF guest ", r.stdout), r.stdout)
    joined = [m["text"] for m in h.call(lambda: list(h.server.history)) if "far-a joined" in m["text"]]
    check("the join event says how it came, and that it's a guest",
          joined and "joined by CF as a guest, machine" in joined[-1], joined)
    r = chat(tmp / "lan-a", h.servers, "-n", "lan-a", "join")
    lan = h.request_of("lan-a")
    check("a direct connection from this machine is tagged LOCAL", lan and lan.via == "LOCAL", lan and lan.via)
    h.call(lambda: h.server.requests.pop(lan.key, None))
    chat(b, servers, "-n", "far-b", "join")
    h.call(h.server.approve, h.request_of("far-b").key)
    chat(b, servers, "-n", "far-b", "read")   # the welcome; else the next wait returns with it at once

    p = chat_bg(b, servers, "-n", "far-b", "wait", "--mentions")
    time.sleep(1)
    long_text = "@far-b " + "x" * 19000   # several Noise frames, one WebSocket message each
    r = chat(a, servers, "-n", "far-a", "send", long_text)
    out, err = p.communicate(timeout=10)
    check("a long message arrives whole", r.returncode == 0 and "x" * 19000 in out.replace("\n  | ", ""),
          r.stderr + err)

    data = os.urandom(3 * 1024 * 1024 + 17)
    src = tmp / "payload.bin"
    src.write_bytes(data)
    r = chat(a, servers, "-n", "far-a", "send-file", str(src), "--to", "far-b")
    fid = re.search(r"\b(F\w+|[0-9a-f]{6,})\b", r.stdout)
    check("send-file through the gateway", r.returncode == 0 and fid, r.stdout + r.stderr)
    if fid:
        r = chat(b, servers, "-n", "far-b", "receive", fid.group(1), "--dir", str(tmp / "got"))
        got = list((tmp / "got").glob("*")) if (tmp / "got").exists() else []
        check("receive through the gateway: same bytes",
              r.returncode == 0 and len(got) == 1 and
              hashlib.sha256(got[0].read_bytes()).digest() == hashlib.sha256(data).digest(),
              r.stdout + r.stderr)

    lan = tmp / "lan-s"   # the shared folder is for the host's own agents
    chat(lan, h.servers, "-n", "lan-s", "join")
    h.call(h.server.approve, h.request_of("lan-s").key)
    chat(lan, h.servers, "-n", "lan-s", "read")
    internal = tmp / "internal.txt"
    internal.write_text("for the host's own agents")
    r = chat(lan, h.servers, "-n", "lan-s", "share", str(internal))
    sid = re.search(r"as file ([0-9a-f]{6})", r.stdout)
    check("a member on the LAN shares a file", r.returncode == 0 and sid, r.stdout + r.stderr)
    sid = sid.group(1) if sid else "none"
    r = chat(a, servers, "-n", "far-a", "files")
    r2 = chat(a, servers, "-n", "far-a", "receive", sid, "--dir", str(tmp / "guest-got"))
    r3 = chat(a, servers, "-n", "far-a", "share", str(src))
    r4 = chat(a, servers, "-n", "far-a", "read", "--last", "50")
    check("a guest through /public can't list, download or add shared files, nor see one shared",
          sid not in r.stdout and "Shared folder (0 files)" in r.stdout and r2.returncode == 1
          and "No such file" in r2.stderr and r3.returncode == 1 and "not guests" in r3.stderr
          and "internal.txt" not in r4.stdout and "far-b" in r4.stdout,
          r.stdout + r2.stderr + r3.stderr + r4.stdout)
    h.call(h.server.set_guest, "far-a", False)
    r = chat(a, servers, "-n", "far-a", "files")
    check("trust is the host's choice, not the way in: made the host's own, a CF member sees the folder",
          sid in r.stdout, r.stdout + r.stderr)
    h.call(h.server.set_guest, "far-a", True)

    check("only / is served", status_of(f"ws://127.0.0.1:{port}/other") == 404)
    held = [raw_ws(url, "203.0.113.5") for _ in range(wss.MAX_OPEN_PER_IP)]
    check("one address can't open more than MAX_OPEN_PER_IP", status_of(url, "203.0.113.5") == 429)
    check("another address still gets in", status_of(url, "198.51.100.7") == 101)
    for ws in held:
        ws.close()
    held = [raw_ws(url, f"2001:db8:1:2::{i + 1:x}") for i in range(wss.MAX_OPEN_PER_IP)]
    check("IPv6 addresses count by their /64", status_of(url, "2001:db8:1:2:ffff::9") == 429 and
          status_of(url, "2001:db8:1:3::1") == 101)
    for ws in held:
        ws.close()
    time.sleep(0.5)
    check("closed connections are given back", h.call(lambda: gateway.open.get("203.0.113.5", 0)) == 0,
          dict(gateway.open))
    saved = wss.MAX_NEW_PER_MINUTE
    wss.MAX_NEW_PER_MINUTE = 3
    try:
        codes = [status_of(url, "192.0.2.9") for _ in range(4)]
    finally:
        wss.MAX_NEW_PER_MINUTE = saved
    check("new connections per minute are limited", codes == [101, 101, 101, 429], codes)
    status = status_of(url, "not-an-ip")
    check("a header that isn't an address is ignored (the peer counts)",
          status == 101 and any("127.0.0.1: connected" in x for x in lines), (status, lines[-3:]))
    check("refusals are logged with the visitor's address", any("refused 203.0.113.5" in x for x in lines), lines)

    public_checks(tmp, h)
    join_limit_checks(h)
    busy_wait_check(h)
    print("public in the TUI")
    asyncio.run(tui_public_test(tmp, tmp / "fake-bin"))

    h.stop()
    r = chat(a, servers, "-n", "far-a", "read")
    check("server down behind the gateway: an unreachable error, not a protocol one",
          r.returncode == 1 and "Can't reach server" in r.stderr, r.stderr)
    h.call(gateway.stop())
    return len(failures) == before


if __name__ == "__main__":
    ok = run_checks()
    print("All passed." if ok else f"{len(failures)} failed")
    sys.exit(0 if ok else 1)
