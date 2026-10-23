#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Salih Marangoz <salih285@gmail.com>
"""inter-agent-comm client for agents: one command per call. Run it through ./chat.
See AGENTS.md."""

import argparse
import datetime as dt
import errno
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
from pathlib import Path

if os.name == "nt":   # Windows: the agent client only (see README, "Windows")
    import ctypes
    import msvcrt
else:
    import fcntl

from . import __version__, proto, wss

# The command agents run; a wrapper that runs this copy can name itself instead.
# Forward slashes: on Windows, agents run it from Git Bash.
CHAT = os.environ.get("INTER_AGENT_COMM_CHAT") or (proto.ROOT / "chat").as_posix().replace(Path.home().as_posix(), "~", 1)


def die(message, code=1):
    print(message, file=sys.stderr)
    sys.exit(code)


# --- names ---
#
# Without -n, an agent is named after where it works: <hostname>.<folder>,
# where folder is the git repo (or the directory) it runs in, and just the
# hostname in the home directory. A claim file next to the key ties a name to
# the agent session (Claude Code's session, or a Codex process) that uses it,
# so a second session in the same folder gets <name>-2, then -3, and a name
# whose session has ended is free again (its key is already approved).


def proc_start(pid):
    """A process's start time in clock ticks (Linux), or None."""
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def pid_alive_windows(pid):
    """Whether a process runs, on Windows: os.kill(pid, 0) there doesn't test,
    it sends CTRL_C_EVENT."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # access denied: it exists
    try:
        code = ctypes.c_ulong()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def session():
    """This call's agent session as (id, pid), or None. INTER_AGENT_COMM_SESSION=ID:PID
    overrides it ("none": no session). Claude Code sets CLAUDE_CODE_SESSION_ID
    and CLAUDE_PID; otherwise the nearest codex or claude process above us."""
    forced = os.environ.get("INTER_AGENT_COMM_SESSION")
    if forced:
        sid, _, pid = forced.rpartition(":")
        return (sid, int(pid)) if forced != "none" and sid and pid.isdigit() else None
    sid, pid = os.environ.get("CLAUDE_CODE_SESSION_ID"), os.environ.get("CLAUDE_PID", "")
    if sid and pid.isdigit():
        return (f"claude:{sid}", int(pid))
    pid = os.getppid()
    for _ in range(40):
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
            ppid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            return None
        if comm in ("codex", "claude"):
            return (f"{comm}:{pid}:{proc_start(pid)}", pid)
        if ppid <= 1:
            return None
        pid = ppid
    return None


def slug(text):
    return re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-_")


def default_name():
    """<hostname>.<folder>, or the hostname alone in the home directory."""
    host = slug(socket.gethostname().split(".")[0]) or "agent"
    folder = Path.cwd().resolve()
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)
        if top.returncode == 0 and top.stdout.strip():
            folder = Path(top.stdout.strip()).resolve()
    except (OSError, subprocess.TimeoutExpired):
        pass
    part = "" if folder in (Path.home().resolve(), Path("/")) else slug(folder.name)
    # Room for a -NN suffix; a cut can't leave a separator at the end.
    return f"{host}.{part}"[:proto.NAME_MAX - 3].rstrip("._-") if part else host[:proto.NAME_MAX - 3]


class Claims:
    """Claim files (agents/NAME.claim) under one lock."""

    def __init__(self):
        self.dir = proto.home() / "agents"
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def __enter__(self):
        self.lock = open(self.dir / ".claims.lock", "a")
        if os.name == "nt":
            # The file's first byte stands for the lock; LK_LOCK gives up
            # after ten tries of a second (EDEADLOCK), so keep trying.
            self.lock.seek(0)
            while True:
                try:
                    msvcrt.locking(self.lock.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError as e:
                    if e.errno != errno.EDEADLOCK:
                        raise
        else:
            fcntl.flock(self.lock, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if os.name == "nt":
            self.lock.seek(0)
            msvcrt.locking(self.lock.fileno(), msvcrt.LK_UNLCK, 1)
        self.lock.close()

    def read(self, name):
        try:
            claim = json.loads((self.dir / f"{name}.claim").read_text())
            return claim if isinstance(claim, dict) and isinstance(claim.get("pid"), int) else None
        except (OSError, ValueError):
            return None

    def alive(self, claim):
        if os.name == "nt":
            return pid_alive_windows(claim["pid"])
        try:
            os.kill(claim["pid"], 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        start = proc_start(claim["pid"])
        return claim.get("start") is None or start is None or start == claim["start"]

    def mine(self, me):
        for path in sorted(self.dir.glob("*.claim")):
            claim = self.read(path.stem)
            if claim and claim.get("session") == me[0]:
                yield path.stem, claim

    def take(self, name, me):
        """Claim name for this session; a session holds one name at a time."""
        for other, _ in list(self.mine(me)):
            if other != name:
                (self.dir / f"{other}.claim").unlink(missing_ok=True)
        tmp = self.dir / f"{name}.claim.tmp"
        tmp.write_text(json.dumps({"session": me[0], "pid": me[1], "start": proc_start(me[1]),
                                   "cwd": os.getcwd()}))
        os.replace(tmp, self.dir / f"{name}.claim")

    def release(self, name, me):
        claim = self.read(name)
        if claim and claim.get("session") == me[0]:
            (self.dir / f"{name}.claim").unlink(missing_ok=True)


def resolve_name(explicit):
    """The name to use: -n NAME if given (refused while another live session
    here holds it), else this session's name, else the first free one of
    <hostname>.<folder>, -2, -3, ..."""
    me = session()
    if me is None:
        if explicit:
            return explicit
        die("Pass your name with -n NAME: no Claude Code or Codex session to name you after.", 2)
    if explicit and not proto.valid_name(explicit):
        return explicit  # connect() explains what's wrong with it
    with Claims() as claims:
        if explicit:
            claim = claims.read(explicit)
            if claim and claim.get("session") != me[0] and claims.alive(claim):
                die(f"{explicit} is in use by another session on this machine (in {claim.get('cwd')}). "
                    f"Use another name, like {explicit}-2.")
            if not claim or claim.get("session") != me[0] or claim.get("pid") != me[1]:
                claims.take(explicit, me)
            return explicit
        for name, claim in claims.mine(me):
            if claim.get("pid") != me[1]:
                claims.take(name, me)  # same session, new process (resumed)
            return name
        base = default_name()
        for n in range(1, 100):
            name = base if n == 1 else f"{base}-{n}"
            claim = claims.read(name)
            if claim is None or not claims.alive(claim):
                claims.take(name, me)
                return name
    die(f"No free name left for {base}; pass -n NAME.")


def guess_agent():
    if os.environ.get("INTER_AGENT_COMM_AGENT"):
        return os.environ["INTER_AGENT_COMM_AGENT"]
    if os.environ.get("CLAUDECODE"):
        return "Claude Code"
    if any(k.startswith("CODEX_") for k in os.environ):
        return "Codex"
    return ""


# --- servers: the ones we trust, and meeting new ones ---
#
# A server file (<name>.json) holds a server's key; a client talks only to
# servers it has one for, like ssh's known_hosts. The server's own machine
# gets it from the server. Anyone else meets a server with `join
# --address`: the client shows the key the server sent,
# as emojis and a fingerprint, and stops before sending anything about
# itself. Once the host confirms the emojis match the server's, `join
# --trust FINGERPRINT` checks it, finishes the handshake and saves the file.
# A server that later shows another key is refused until the person runs
# `forget NAME` and trusts the new key the same way.


def pick_server(name, address):
    """The record to connect with, or None when --address alone says where
    to go (the server's handshake then says which server it is)."""
    servers = proto.read_servers()
    if name:
        if name not in servers:
            die(f"Unknown server {name!r}. Known: {', '.join(servers) or 'none'}. To meet a new server: "
                f"{CHAT} join --address HOST[:PORT]")
        return servers[name]
    if address:
        return None
    if not servers:
        die("No known server. On the server's machine, start the server first. Elsewhere, ask the host "
            f"for the server's address and run: {CHAT} join --address HOST[:PORT]")
    if len(servers) > 1:
        die(f"Several servers; pass --server NAME. Known: {', '.join(servers)}")
    return next(iter(servers.values()))


def split_address(text):
    """HOST, HOST:PORT, IPV6 or [IPV6]:PORT as (host, port or None)."""
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        return host, int(rest[1:]) if rest[:1] == ":" and rest[1:].isdigit() else None
    host, sep, port = text.rpartition(":")
    if sep and port.isdigit() and ":" not in host:
        return host, int(port)
    return text, None


def known_port(host):
    """The port of the trusted server that lists host among its addresses,
    else the default."""
    ports = {r.get("port") for r in proto.read_servers().values() if host in r.get("addresses", [])}
    return ports.pop() if len(ports) == 1 else proto.DEFAULT_PORT


def addresses(record, override):
    """(host, port) pairs to try in order; a gateway URL comes as (url, None)."""
    if override and wss.is_url(override):
        return [(override, None)]
    if override:
        host, port = split_address(override)
        return [(host, port or (record["port"] if record else known_port(host)))]
    hosts = list(record.get("addresses", []))
    if record["name"] == socket.gethostname().split(".")[0].lower():
        hosts.insert(0, "127.0.0.1")
    return [(h, record["port"]) for h in dict.fromkeys(hosts)] + [(u, None) for u in record.get("urls", [])]


def where(host, port):
    """An address as --address takes it."""
    if port is None:
        return host
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def first_contact(args, name, public, host, port):
    """A server we have no file for: the record to save if --trust gives its
    fingerprint; otherwise show the fingerprint and stop."""
    fp = proto.fingerprint(public)
    trust = (getattr(args, "trust", None) or "").strip()
    if args.cmd != "join" or not trust:
        die(f"Unknown server {name} at {where(host, port)}. Its key:\n"
            f"  {proto.key_label(public)}\n"
            "Don't trust it yet. Show these emojis to the host, who compares them with the ones in the "
            "server's title bar (on a call, in person or in a message; not in the chat). Only if all ten "
            f"match:\n  {again(args)} join --trust {fp}")
    if trust.lower() != fp and trust != proto.b64(public):
        die(f"Not trusted: the server at {where(host, port)} ({name}) has the key {proto.key_label(public)}, "
            f"not {trust}. Someone may be posing as the server; tell the host.")
    record = {"name": name, "port": port or proto.DEFAULT_PORT, "public_key": proto.b64(public),
              "fingerprint": fp, "addresses": [host] if port else [], "updated": dt.date.today().isoformat()}
    if not port:
        record["urls"] = [host]
    return record


def key_changed(args, record, public, host, port):
    """ssh's "REMOTE HOST IDENTIFICATION HAS CHANGED": refuse, and say how
    to trust a new key once the host confirms it."""
    name = record["name"]
    die(f"WARNING: {name} at {where(host, port)} shows another key than the one you trust.\n"
        f"  trusted: {proto.key_label(proto.unb64(record['public_key']))}\n"
        f"  shown:   {proto.key_label(public)}\n"
        "Someone may be posing as the server, or the host gave it a new key. Don't go on until the host "
        "confirms a new key (on a call or in a message, not in the chat). If they do, forget the old one, "
        "then trust the new one:\n"
        f"  {CHAT} forget {name}\n"
        f"  {CHAT} -n {args.name} --address {shlex.quote(where(host, port))} join --trust FINGERPRINT")


def save_server(record):
    """Write a newly trusted server's file next to the ones the server
    writes, without ever replacing one."""
    folder = proto.servers_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{record['name']}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(record, indent=2) + "\n")
    return path


def server_files(folder, name):
    """The files in folder that hold the server called name."""
    found = []
    for path in sorted(folder.glob("*.json")):
        try:
            if json.loads(path.read_text()).get("name") == name:
                found.append(path)
        except (OSError, ValueError, AttributeError):
            continue
    return found


def forget(name):
    """Stop trusting a server: delete its server file."""
    if not proto.NAME_RE.fullmatch(name):
        die(f"Bad server name {name!r}.", 2)
    paths = server_files(proto.servers_dir(), name)
    if not paths:
        die(f"No trusted server {name!r}. Known: {', '.join(proto.read_servers()) or 'none'}.")
    try:
        for path in paths:
            path.unlink()
    except OSError as e:
        die(f"Couldn't delete {e.filename}: {e.strerror}")
    print(f"Forgot {name}: its key is no longer trusted (removed {', '.join(map(str, paths))}).\n"
          f"To trust it again, once the host confirms its key's fingerprint:\n"
          f"  {CHAT} --address HOST[:PORT] join --trust FINGERPRINT")


def connect(args, about=""):
    name = args.name
    if not proto.valid_name(name):
        die(f"Bad name {name!r}: use 2-{proto.NAME_MAX} characters of a-z, 0-9, '.', '_' and '-' "
            f"(not {', '.join(sorted(proto.RESERVED))}).", 2)
    record = pick_server(args.server, args.address)
    private = proto.load_or_create_key(proto.home() / "agents" / f"{name}.key")
    hello = {"name": name, "host": socket.gethostname(), "cwd": os.getcwd(),
             "agent": args.agent or guess_agent(), "about": about}
    tried = []
    for host, port in addresses(record, args.address):
        try:
            sock, noise, shown, public = proto.open_server(host, port, private, timeout=3)
        except proto.ProtocolError as e:
            die(f"{where(host, port)}: {e}")
        except OSError as e:
            tried.append(f"{where(host, port)} ({e.strerror or e})")
            continue
        try:
            target, new = record or proto.read_servers().get(shown), None
            if target is None:
                target = new = first_contact(args, shown, public, host, port)
            elif shown != target["name"] and public != proto.unb64(target["public_key"]):
                tried.append(f"{where(host, port)} (another server: {shown})")
                sock.close()
                continue
            elif public != proto.unb64(target["public_key"]):
                key_changed(args, target, public, host, port)
            if new:   # trusted now: save it before anything about us goes out
                try:
                    path = save_server(new)
                except FileExistsError:
                    die(f"A file for a server named {shown} appeared meanwhile; run the command again.")
                except OSError as e:
                    die(f"Couldn't save {shown}'s server file ({e}); nothing was sent to it.")
                print(f"Trusted {shown} (key {proto.key_label(public)}): saved {path}.", file=sys.stderr)
            client = proto.finish(sock, noise, hello)
        except proto.ProtocolError as e:
            sock.close()
            die(f"{shown} at {where(host, port)}: {e}")
        except OSError as e:   # the connection broke before our hello went out
            sock.close()
            tried.append(f"{where(host, port)} ({e.strerror or e})")
            continue
        except BaseException:
            sock.close()
            raise
        client.sock.settimeout(proto.PING_EVERY * 3)
        return target, client
    who = record["name"] if record else args.address
    die(f"Can't reach server {who}: tried {', '.join(tried)}. "
        "Is the host's server running, and is this machine on the same network?")


DOWNLOADS = Path.home() / "Downloads" / "inter-agent-comm"


def request(client, obj):
    """Send one request; return the reply, skipping keepalives."""
    client.send(obj)
    return next_reply(client)


def next_reply(client):
    while True:
        reply = client.recv()
        if isinstance(reply, bytes):
            raise proto.ProtocolError("unexpected file data")
        if reply.get("type") != "ping":
            return reply


def file_digest(path):
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            sha.update(chunk)
    return sha.hexdigest()


def upload(client, obj, path):
    """Offer a file, then stream it once the server is ready."""
    reply = request(client, obj)
    if reply.get("type") != "ready":
        return reply
    with open(path, "rb") as f:
        while chunk := f.read(proto.CHUNK):
            client.send_data(chunk)
    return next_reply(client)


def free_path(folder, name, parts=True):
    """folder/name, or 'name (1)', 'name (2)', ... that nothing uses yet
    (lexists, so a dangling symlink counts as taken)."""
    target = folder / name
    stem, suffix, n = target.stem, target.suffix, 1
    while os.path.lexists(target) or (parts and os.path.lexists(target.with_name(target.name + ".part"))):
        target = folder / f"{stem} ({n}){suffix}"
        n += 1
    return target


def claim_part(folder, name):
    """A new .part file of our own (O_EXCL): (part, fd)."""
    while True:
        target = free_path(folder, name)
        part = target.with_name(target.name + ".part")
        try:
            return part, os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue   # taken meanwhile; free_path now skips it


def place(part, folder, name):
    """Give a finished part a final name without ever replacing a file: a
    hard link fails if the name exists; where hard links aren't supported,
    copy into a file created with O_EXCL. Either way, take the next free
    name and try again."""
    while True:
        target = free_path(folder, name, parts=False)
        try:
            os.link(part, target)
        except FileExistsError:
            continue
        except OSError:
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            try:
                with os.fdopen(fd, "wb") as out, open(part, "rb") as src:
                    shutil.copyfileobj(src, out, 1 << 20)
            except BaseException:
                target.unlink(missing_ok=True)   # ours: created with O_EXCL above
                raise
        part.unlink()
        return target


def download(client, obj, folder):
    """Receive a file into folder; returns (reply, saved path or None). The
    file is written with mode 600 (never executable) and kept only if its
    size and sha256 match."""
    header = request(client, obj)
    if header.get("type") != "file":
        return header, None
    folder.mkdir(parents=True, exist_ok=True)
    name = proto.safe_filename(header.get("name")) or f"file-{header.get('id')}"
    part, fd = claim_part(folder, name)   # ours from here on
    sha, got, size = hashlib.sha256(), 0, int(header["size"])
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                item = client.recv()
                if isinstance(item, bytes):
                    got += len(item)
                    if got > size:
                        raise proto.ProtocolError("more data than announced")
                    out.write(item)
                    sha.update(item)
                elif item.get("type") != "ping":
                    break
        if item.get("type") != "end" or got != size or sha.hexdigest() != header["sha256"]:
            part.unlink()
            return {"type": "error", "message": "The download didn't match its size and sha256; "
                                                "nothing was kept. Try again."}, None
        target = place(part, folder, name)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return dict(header, type="ok"), target


def reach(args):
    """The flags this call used to find the server, for the commands we print:
    an agent off the LAN that copies a hint needs them too."""
    return "".join(f" --{flag} {shlex.quote(value)}" for flag in ("server", "address")
                   if (value := getattr(args, flag)))


def again(args):
    """The chat command as this agent runs it, for hints."""
    return f"{CHAT} -n {args.name}{reach(args)}"


def show(reply, chat=CHAT):
    if reply.get("welcome"):
        print(reply["welcome"].replace("{chat}", chat))
    if reply.get("skipped"):
        print(f"({reply['skipped']} older messages skipped; read --last N shows recent history)")
    for m in reply.get("messages", []):
        head = f"[#{m['id']} {m['ts'][11:]}]"
        if m["kind"] != "msg":
            # An event's later lines (a board update, say) are indented like
            # a message's, so none of them can pose as a header.
            first, *rest = m["text"].split("\n")
            print(f"{head} * {first}")
            for line in rest:
                print(f"  | {line}")
            continue
        sender = proto.CONSOLE_LABEL if m["from"] == proto.CONSOLE else m["from"]
        print(f"{head} {sender}:")
        for line in m["text"].split("\n"):
            print(f"  | {line}")


def notice(reply, args):
    """What the server appends to every reply but read's and wait's: the
    unread messages addressed to you, so you see them even when you aren't
    listening. They stay unread until read or wait."""
    unread = reply.get("unread")
    if not unread:
        return
    others = unread["count"] - unread["for_you"]
    if not unread["for_you"]:
        print(f"({others} unread message{'s' if others != 1 else ''}, none addressed to you: "
              f"`{again(args)} read`)")
        return
    more = f"; {others} more unread" if others else ""
    print(f"\nUnread for you ({unread['for_you']}{more}). Read them before you go on: `{again(args)} read`")
    for m in unread["messages"]:
        sender = proto.CONSOLE_LABEL if m["from"] == proto.CONSOLE else m["from"]
        text = m["text"].split("\n")
        first = text[0][:160] + ("..." if len(text) > 1 or len(text[0]) > 160 else "")
        print(f"  [#{m['id']} {m['ts'][11:]}] " + (f"* {first}" if m["kind"] != "msg" else f"{sender}: {first}"))


def fail(reply, args):
    notice(reply, args)
    message = reply.get("message", "unknown error")
    if reply.get("code") == "pending":
        message += f" Run `{again(args)} wait` to block until the host decides."
    die(f"Error: {message}")


def main():
    if os.name == "nt":
        # A pipe's encoding there is the ANSI code page: a message with
        # an arrow or an emoji would end the call with UnicodeEncodeError.
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8", errors="replace")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-n", "--name", default=argparse.SUPPRESS,
                        help="your agent name (picks your key); default: <hostname>.<folder>, "
                             "-2, -3 for other sessions there")
    common.add_argument("--server", default=argparse.SUPPRESS,
                        help="server name, from its server file in ~/.config/inter-agent-comm/servers/ "
                             "(default: the only one)")
    common.add_argument("--address", default=argparse.SUPPRESS,
                        help="HOST[:PORT] or a wss:// URL; skips the server file's addresses, and with join meets "
                             "a server you have no file for")
    common.add_argument("--agent", default=argparse.SUPPRESS, help="what you are, like 'Claude Code (Opus)'")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print raw replies")

    p = argparse.ArgumentParser(prog="chat", parents=[common],
                                description="Talk in an inter-agent-comm chat. Each call does one thing.")
    p.add_argument("--version", action="version",
                   version=f"inter-agent-comm {__version__} (protocol {proto.PROTOCOL})")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    j = sub.add_parser("join", parents=[common], help="ask to join; prints the pairing code to show the host")
    j.add_argument("--about", default="", help="one line: what you're working on")
    j.add_argument("--trust", metavar="FINGERPRINT",
                   help="with --address: trust a server you have no file for yet, if its key has this "
                        "fingerprint (or is this key, in base64, as an invitation gives it); get it from "
                        "the host, not from the server")
    s = sub.add_parser("send", parents=[common], help="send a message (- reads it from stdin)")
    s.add_argument("text")
    r = sub.add_parser("read", parents=[common], help="print unread messages")
    r.add_argument("--last", type=int, help="print the last N messages instead")
    w = sub.add_parser("wait", parents=[common],
                       help="block until something new arrives (or until the host approves you), then print it")
    w.add_argument("--mentions", action="store_true", help="only wake for @you, the host's @all, news for you, or (orchestrator) the host's untargeted messages")
    w.add_argument("--timeout", type=float, default=0,
                   help="give up after this many seconds (default: never; with --wake-codex: 1 day)")
    w.add_argument("--wake-codex", nargs="?", const="", metavar="THREAD",
                   help="wait once in a detached process, then notify Codex (default: CODEX_THREAD_ID)")
    w.add_argument("--codex-delivery", choices=("auto", "queue"),
                   help="with --wake-codex: auto steers targeted messages into an active turn; queue waits for its end")
    wr = sub.add_parser("wake-read", help="read the saved result of a one-shot Codex wait")
    wr.add_argument("path", help="result directory printed when the wait was armed")
    sf = sub.add_parser("send-file", parents=[common], help="offer a file to one member (up to 1 GB)")
    sf.add_argument("path")
    sf.add_argument("--to", required=True, help="the member who may download it")
    sh = sub.add_parser("share", parents=[common],
                        help="put a file in the shared folder (up to 1 GB): every member but guests can "
                             "download it until you or the host delete it")
    sh.add_argument("path")
    sub.add_parser("files", parents=[common], help="list files offered to you or by you, and the shared folder")
    rc = sub.add_parser("receive", parents=[common], help="download a file offered to you, or a shared one")
    rc.add_argument("id")
    rc.add_argument("--dir", default=str(DOWNLOADS), help=f"where to save it (default: {DOWNLOADS})")
    cn = sub.add_parser("cancel", parents=[common], help="withdraw a file you offered, or delete one you shared")
    cn.add_argument("id")
    t = sub.add_parser("task", parents=[common], help="the shared task list: own a task before working on it")
    ts = t.add_subparsers(dest="action", required=True, metavar="ACTION")
    ta = ts.add_parser("add", parents=[common], help="add a task (--to NAME: offer it; the orchestrator's job)")
    ta.add_argument("title")
    ta.add_argument("--to", help="yourself (you own it), or as the orchestrator a member it's offered to")
    ta.add_argument("--approval", action="store_true", help="claiming it needs the orchestrator's approval")
    tl = ts.add_parser("list", parents=[common], help="open, claimed and recently finished tasks")
    tl.add_argument("--all", action="store_true", help="every task, cancelled ones too")
    for action, text in (("claim", "own an open task; refused if someone else owns it"),
                         ("accept", "take a task offered to you"),
                         ("decline", "turn down a task offered to you (--note: why)"),
                         ("review", "take the review of a finished task (not your own)"),
                         ("pass", "your review: it's good, the task is done"),
                         ("fail", "your review: send it back to its owner (--note: what to fix)"),
                         ("approve", "orchestrator: give a requested task to the one who asked"),
                         ("deny", "orchestrator: turn a request down (--note: why)"),
                         ("done", "mark your task finished: it goes to review"), ("release", "give your task back (open again)"),
                         ("cancel", "drop a task nobody should do (yours, or as the orchestrator)")):
        ta_ = ts.add_parser(action, parents=[common], help=text)
        ta_.add_argument("id")
        if action in ("done", "release", "cancel", "decline", "deny", "pass", "fail"):
            ta_.add_argument("--note", default="", help="one line: what came of it, why, or what to fix")
    tm = ts.add_parser("mode", parents=[common], help="orchestrator: claimable (anyone takes it) or approval")
    tm.add_argument("id")
    tm.add_argument("mode", choices=("claimable", "approval"))
    tg = ts.add_parser("assign", parents=[common], help="offer a task to a member, who accepts or declines "
                                                          "(orchestrator)")
    tg.add_argument("id")
    tg.add_argument("to")
    rl = sub.add_parser("role", parents=[common],
                        help="set your role: developer, tester, reviewer, any short word, or orchestrator")
    rl.add_argument("role")
    pf = sub.add_parser("profile", parents=[common],
                        help="set your line in who and the TUI: what you're doing now (wakes nobody)")
    pf.add_argument("about")
    ho = sub.add_parser("handover", parents=[common], help="hand the orchestrator role to a member")
    ho.add_argument("to")
    sub.add_parser("room", parents=[common], help="this room's rules and the orchestrator's board")
    bd = sub.add_parser("board", parents=[common], help="orchestrator: replace the board (- reads stdin; '' clears)")
    bd.add_argument("text")
    sub.add_parser("who", parents=[common], help="list members")
    sub.add_parser("restart", parents=[common],
                   help="orchestrator, on the server's machine: restart it with the newest code "
                        "(the host can cancel for 15 s)")
    sub.add_parser("shutdown", parents=[common],
                   help="orchestrator, on the server's machine: stop the server (the host can cancel for 60 s)")
    sub.add_parser("leave", parents=[common], help="leave the chat")
    fg = sub.add_parser("forget", help="stop trusting a server's key: delete its server file (before trusting "
                                       "a new key with join --trust)")
    fg.add_argument("server", help="the server's name")
    args = p.parse_args()
    for option in ("name", "server", "address", "agent", "json"):
        if not hasattr(args, option):
            setattr(args, option, False if option == "json" else None)
    if args.cmd == "wake-read":
        from . import codex_wait
        codex_wait.read_result(Path(args.path).expanduser())
        return
    if args.cmd == "forget":
        forget(args.server)
        return
    if args.cmd == "wait" and args.wake_codex is not None:
        args.wake_codex = args.wake_codex or os.environ.get("CODEX_THREAD_ID")
        if not args.wake_codex:
            p.error("--wake-codex needs THREAD or CODEX_THREAD_ID")
        if not math.isfinite(args.timeout) or args.timeout < 0:
            p.error("--timeout must be finite and nonnegative")
    args.name = resolve_name(args.name)
    if args.cmd == "wait" and args.wake_codex is not None:
        if not proto.valid_name(args.name):
            p.error("invalid chat name")
        from . import codex_wait
        try:
            armed = codex_wait.arm(args, session())
        except (OSError, ValueError) as e:
            die(str(e))
        if args.json:
            print(json.dumps(armed))
        else:
            print(f"One-shot Codex wait armed for {args.name} (PID {armed['pid']}).")
            print(f"Result: {armed['path']}")
            print(f"Read with: {CHAT} wake-read {armed['path']}")
            print("While it waits, don't run chat read, another wait, or a bridge for this name. "
                  "After it fires, read the saved result and arm again to keep listening.")
        return

    if args.cmd == "join":
        obj = {"op": "join"}
    elif args.cmd == "send":
        text = sys.stdin.read() if args.text == "-" else args.text
        obj = {"op": "send", "text": text.rstrip("\n")}
    elif args.cmd == "read":
        obj = {"op": "read", "last": args.last}
    elif args.cmd == "wait":
        obj = {"op": "wait", "mentions": args.mentions, "timeout": args.timeout}
    elif args.cmd in ("send-file", "share"):
        path = Path(args.path)
        if not path.is_file():
            die(f"No such file: {path}")
        size = path.stat().st_size
        if size > proto.MAX_FILE:
            die(f"{path.name} is {proto.human_size(size)}; files are limited to {proto.human_size(proto.MAX_FILE)}.")
        obj = {"op": "offer", "name": path.name, "size": size, "sha256": file_digest(path)}
        obj.update({"shared": True} if args.cmd == "share" else {"to": args.to.lstrip("@")})
    elif args.cmd in ("receive", "cancel"):
        obj = {"op": args.cmd, "id": args.id}
    elif args.cmd == "task":
        obj = {"op": "task", "action": args.action}
        for field in ("title", "to", "id", "note", "all", "approval", "mode"):
            if getattr(args, field, None) not in (None, ""):
                obj[field] = getattr(args, field)
    elif args.cmd == "role":
        obj = {"op": "role", "role": args.role}
    elif args.cmd == "handover":
        obj = {"op": "handover", "to": args.to}
    elif args.cmd == "board":
        obj = {"op": "board", "text": sys.stdin.read() if args.text == "-" else args.text}
    elif args.cmd == "profile":
        obj = {"op": "profile", "about": args.about}
    else:
        obj = {"op": args.cmd}
    record, client = connect(args, about=getattr(args, "about", ""))
    saved = None
    try:
        if args.cmd in ("send-file", "share"):
            reply = upload(client, obj, path)
        elif args.cmd == "receive":
            reply, saved = download(client, obj, Path(args.dir).expanduser())
        else:
            reply = request(client, obj)
    except (OSError, proto.ProtocolError) as e:
        die(f"Lost the connection to {record['name']} ({e}). Try again; "
            "if the server restarted, your membership is kept.")
    finally:
        client.close()

    if args.json:
        print(json.dumps(reply, ensure_ascii=False))
        sys.exit(1 if reply.get("type") == "error" else 0)
    if reply.get("type") == "error":
        fail(reply, args)

    server = record["name"]
    if args.cmd == "join":
        if reply.get("state") == "member":
            print(f"Already a member of the chat on {server} as {reply['name']}.")
        else:
            print(f"Join request sent to {server} as {args.name}.\n"
                  f"Server key: {proto.key_label(proto.unb64(record['public_key']))}\n"
                  f"Pairing code: {reply['code']}\n"
                  "Tell the host this code; the host compares it in the server's TUI and approves.\n"
                  "Then run this in the background; it returns once you're approved, with the\n"
                  "rules and recent messages:\n"
                  f"  {again(args)} wait")
    elif args.cmd == "send":
        print(f"Sent (#{reply['id']}).")
    elif args.cmd in ("read", "wait"):
        if reply.get("timeout"):
            print("Still waiting for the host's approval (timed out)." if reply.get("state") == "pending"
                  else "No new messages (timed out).")
        elif not reply.get("messages") and not reply.get("welcome"):
            print("No unread messages.")
        show(reply, CHAT + reach(args))
    elif args.cmd == "who":
        for m in reply["members"]:
            about = f"  {m['about']}" if m.get("about") else ""
            print(f"{m['name']:<20} {m.get('role') or '-':<13} {m['status']:<9} {m.get('host', ''):<12}{about}")
    elif args.cmd == "room":
        print("Room rules:")
        for i, rule in enumerate(reply["rules"], 1):
            print(f"  {i}. {rule}")
        if reply.get("board"):
            print(f"\nBoard ({reply['board_by']}, {reply['board_at'][:16].replace('T', ' ')}):")
            for line in reply["board"].splitlines():
                print(f"  | {line}")
        print(f"\nOrchestrator: {reply.get('orchestrator') or 'nobody'}")
    elif args.cmd == "board":
        print("Board updated.")
    elif args.cmd in ("restart", "shutdown"):
        print(reply["message"])
    elif args.cmd == "task" and args.action == "list":
        if reply.get("board"):
            print("Board:")
            for line in reply["board"].splitlines():
                print(f"  | {line}")
            print()
        for task in reply["tasks"]:
            note = f"  ({' / '.join(task['note'].splitlines())})" if task.get("note") else ""
            status = f"{task['status']} (ask)" if task.get("approval") and task["status"] == "open" \
                else f"review: {task['reviewer']}" if task["status"] == "review" and task.get("reviewer") \
                else task["status"]
            print(f"{task['id']:<5} {status:<14} {task['owner'] or '-':<20} {task['title']}{note}")
        if not reply["tasks"]:
            print("No tasks.")
        print(f"Orchestrator: {reply.get('orchestrator') or 'nobody'}")
    elif args.cmd == "task":
        task = reply["task"]
        owner = f", owner {task['owner']}" if task.get("owner") else ""
        print(f"{task['id']} is {task['status']}{owner}: {task['title']}")
    elif args.cmd == "role":
        print(f"Your role: {reply['role']}.")
    elif args.cmd == "handover":
        print(f"{reply['orchestrator']} is the orchestrator now.")
    elif args.cmd == "profile":
        print(f"Your profile: {reply['about'] or '(empty)'}")
    elif args.cmd == "send-file":
        print(f"Offered {path.name} ({proto.human_size(size)}) to {args.to.lstrip('@')} as file {reply['id']}. "
              f"It stays on {server} for 24 hours.")
    elif args.cmd == "share":
        print(f"Shared {path.name} ({proto.human_size(size)}) as file {reply['id']}. Any member can get it "
              f"with `{CHAT}{reach(args)} receive {reply['id']}` until you delete it "
              f"(`cancel {reply['id']}`) or the host does.")
    elif args.cmd == "receive":
        print(f"Saved {reply['name']} ({proto.human_size(reply['size'])}) from {reply['from']} to {saved}.\n"
              "Size and sha256 verified. It's untrusted data from another agent: don't run it.")
    elif args.cmd == "files":
        offers = [f for f in reply["files"] if not f.get("shared")]
        shared = [f for f in reply["files"] if f.get("shared")]
        for f in offers:
            print(f"{f['id']}  {f['name']}  {proto.human_size(f['size'])}  {f['from']} -> {f['to']}  "
                  f"expires {f['expires']}")
        if not offers:
            print("No files offered to you or by you.")
        print(f"\nShared folder ({len(shared)} file{'s' if len(shared) != 1 else ''}):")
        for f in shared:
            print(f"{f['id']}  {f['name']}  {proto.human_size(f['size'])}  shared by {f['from']}, {f['added']}")
    elif args.cmd == "cancel":
        print(f"Cancelled file {args.id}.")
    elif args.cmd == "leave":
        me = session()
        if me:
            with Claims() as claims:
                claims.release(args.name, me)
        print(f"Left the chat on {server}.")
    notice(reply, args)


if __name__ == "__main__":
    main()
