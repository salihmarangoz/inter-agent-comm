# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Salih Marangoz <salih285@gmail.com>
"""Shared by server.py and chat.py: keys, the Noise handshake, framing, names.

Wire format:
  frame   = 2-byte big-endian length + one Noise message
  object  = JSON, split into chunks of up to CHUNK bytes; each chunk is one
            frame whose plaintext starts with 1 (more chunks follow) or 0 (last)
The handshake is Noise XX, and the client speaks first: until a client
sends a handshake's first message, the server sends nothing, so from
outside it's a port that stays silent. The server's second message carries
its key and, encrypted, its name and version. The client checks that key
against its server file (<name>.json, see read_servers), or on first
contact shows its fingerprint for its person to confirm, and only then
sends its own key and a hello in the third message.
"""

import base64
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import struct
import sys
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from noise.connection import Keypair, NoiseConnection

from . import wss

PROTOCOL = "2"   # the wire protocol; the release version is __version__ in __init__.py
NOISE_NAME = b"Noise_XX_25519_ChaChaPoly_BLAKE2s"
PROLOGUE = b"inter-agent-comm"
DEFAULT_PORT = 23232
CHUNK = 60000             # plaintext bytes per Noise message (Noise allows 65535)
MAX_OBJECT = 4_000_000    # one JSON object, all chunks together
MAX_TEXT = 20000          # characters in one chat message
HANDSHAKE_TIMEOUT = 10
PING_EVERY = 20           # seconds between keepalives while a client waits
MAX_FILE = 1024 ** 3      # bytes per shared file
FILES_TOTAL = 4 * 1024 ** 3   # bytes of shared files the server keeps at once
FILE_TTL = 24 * 3600      # seconds a file offer is kept
SHARED_TOTAL = 4 * 1024 ** 3  # bytes the shared folder keeps; no expiry
DATA = b"\x02"            # first plaintext byte of a raw file-data frame
NAME_MAX = 48
NAME_RE = re.compile(rf"[a-z0-9][a-z0-9._-]{{1,{NAME_MAX - 1}}}")
CONSOLE = "host"    # the name the human at the server's console speaks as; nobody else can
CONSOLE_LABEL = f"{CONSOLE} (the human at the server, verified by the server)"
RESERVED = {CONSOLE, "server", "system", "all", "everyone", "here"}

PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parent   # the repo: the launchers, docs and pyproject.toml


class ProtocolError(Exception):
    pass


class Closed(ConnectionError):
    """The other side closed the connection."""


def home():
    """Keys and server state, outside the repo."""
    path = Path(os.environ.get("INTER_AGENT_COMM_HOME") or Path.home() / ".config" / "inter-agent-comm")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def command(name):
    """How Python starts one of the package's commands: the package folder
    run by path, as the launchers do (see __main__.py)."""
    return [sys.executable, str(PACKAGE), name]


def state_root(path=None):
    """/tmp/inter-agent-comm-<uid> (or path): the Codex wait's and bridge's
    state. In a folder everyone can write to, another user could make it
    first and swap our files, so it must be a real directory of ours, mode
    700."""
    path = Path(path or f"/tmp/inter-agent-comm-{os.getuid()}")
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise PermissionError(f"{path} isn't a private directory of yours (owned by you, mode 700); "
                              "remove it and try again")
    return path


def files_dir():
    """Where the server keeps offered files, outside the repo."""
    return Path(os.environ.get("INTER_AGENT_COMM_FILES") or Path.home() / ".cache" / "inter-agent-comm" / "files")


def servers_dir():
    """The server files: the ones this machine's server writes and the ones
    clients trust. $INTER_AGENT_COMM_SERVERS, else servers/ in the config
    folder."""
    env = os.environ.get("INTER_AGENT_COMM_SERVERS")
    return Path(env) if env else home() / "servers"


def read_servers():
    """Server records by name, from the *.json files in servers_dir()."""
    records = {}
    for path in sorted(servers_dir().glob("*.json")):
        record = json.loads(path.read_text())
        records[record["name"]] = record
    return records


def b64(data):
    return base64.b64encode(bytes(data)).decode()


def unb64(text):
    return base64.b64decode(text, validate=True)


def load_or_create_key(path):
    """A private X25519 key as raw bytes, created (mode 600) on first use."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        raw = X25519PrivateKey.generate().private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(b64(raw) + "\n")
    return unb64(path.read_text().strip())


def public_key(private):
    return X25519PrivateKey.from_private_bytes(private).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def new_pairing_code():
    """Six random digits for one join request. The server sends them to the
    agent over Noise, and the host compares what the agent shows with the
    TUI. Random per request, so seeing an agent's code once doesn't help
    anyone make a key that gets the same code."""
    n = secrets.randbelow(1_000_000)
    return f"{n // 1000:03d}-{n % 1000:03d}"


def fingerprint(public):
    return hashlib.blake2s(bytes(public)).hexdigest()[:16]


# Matrix's 64 SAS emojis, chosen to be told apart at a glance; the seven
# that need a variation selector (and so render at odd widths in terminals)
# are swapped for ones that don't.
EMOJI = "🐶🐱🦁🐎🦄🐷🐘🐰🐼🐓🐧🐢🐟🐙🦋🌷🌳🌵🍄🌏🌙⛄🔥🍌🍎🍓🌽🍕🎂🍒😀🤖🎩👓🔧🎅👍☔⌛⏰🎁💡📕📏📎🧲🔒🔑🔨📞🏁🚂🚲🚁🚀🏆⚽🎸🎺🔔⚓🎧📁📌"


def emojis(public):
    """The fingerprint's first 60 bits as ten emojis in two groups of five,
    for people to compare by eye. Ten, not a few: the key is long-lived and
    anyone can ask for it, so a short code could be matched by a key made
    for it."""
    n = int(fingerprint(public), 16) >> 4
    marks = [EMOJI[(n >> (54 - 6 * i)) & 63] for i in range(10)]
    return "".join(marks[:5]) + " " + "".join(marks[5:])


def key_label(public):
    """A key as people see it: its emojis, then its fingerprint for --trust."""
    return f"{emojis(public)} ({fingerprint(public)})"


def valid_name(name):
    return bool(NAME_RE.fullmatch(name)) and name not in RESERVED


WINDOWS_DEVICES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}


def valid_server_name(name):
    """A server's name, also its file's: no Windows device names (nul.json
    there is the null device, not a file)."""
    return isinstance(name, str) and bool(NAME_RE.fullmatch(name)) and name.split(".")[0] not in WINDOWS_DEVICES


def clean(text, limit, newlines=True):
    """Drop control characters (terminal escapes), keeping newlines and tabs
    only where allowed. Unicode's line and paragraph separators count as
    newlines, so no reader sees a line break the indenting missed."""
    keep = "\n\t" if newlines else ""
    text = str(text).replace("\u2028", "\n").replace("\u2029", "\n")
    text = "".join(c for c in text if c in keep or not (ord(c) < 32 or 127 <= ord(c) < 160))
    return text[:limit]


def one_line(text, limit):
    """clean() for text shown on one line, its line breaks as " / "."""
    return clean(" / ".join(clean(text, limit * 2).splitlines()), limit, newlines=False)


def safe_filename(name):
    """A plain file name, or None: no directories, no control characters,
    no leading dots (so no hidden files, '.' or '..')."""
    name = Path(str(name).replace("\\", "/")).name
    name = clean(name, 200, newlines=False).strip().lstrip(".").strip()
    return name or None


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


CODE_RE = re.compile(r"```.*?(?:```|\Z)|`[^`\n]*`", re.S)


def mentions(text, name, everyone=True):
    """True if text calls name with @name, or everyone with @all when
    everyone is allowed. A handle inside `code` or a ``` block is quoted,
    not called, so it doesn't count."""
    names = re.escape(name) + ("|all" if everyone else "")
    pattern = rf"(?<![\w@.-])@(?:{names})(?![\w-]|\.\w)"
    return re.search(pattern, CODE_RE.sub(" ", text)) is not None


def _noise(initiator, private):
    noise = NoiseConnection.from_name(NOISE_NAME)
    if initiator:
        noise.set_as_initiator()
    else:
        noise.set_as_responder()
    noise.set_prologue(PROLOGUE)
    noise.set_keypair_from_private_bytes(Keypair.STATIC, private)
    noise.start_handshake()
    return noise


def _frame(message):
    return struct.pack(">H", len(message)) + bytes(message)


def seal(noise, obj):
    """Encrypt one JSON object into frames."""
    data = json.dumps(obj, ensure_ascii=False).encode()
    if len(data) > MAX_OBJECT:
        raise ProtocolError("message too large")
    parts = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)] or [b""]
    last = len(parts) - 1
    return b"".join(_frame(noise.encrypt((b"\x00" if i == last else b"\x01") + part))
                    for i, part in enumerate(parts))


def seal_data(noise, data):
    """Encrypt up to CHUNK bytes of raw file data into one frame."""
    return _frame(noise.encrypt(DATA + data))


class Opener:
    """Decrypts frames. Returns a JSON object once its last chunk is in, the
    bytes of a file-data frame, or None while a JSON object is incomplete."""

    def __init__(self, noise):
        self.noise = noise
        self.buffer = bytearray()
        self.limit = MAX_OBJECT   # the server lowers it for keys that aren't members

    def feed(self, message):
        try:
            plain = self.noise.decrypt(message)
        except InvalidTag:
            raise ProtocolError("corrupted or forged frame")
        if plain[:1] == DATA:
            if self.buffer:
                raise ProtocolError("file data inside a JSON object")
            return bytes(plain[1:])
        self.buffer += plain[1:]
        if len(self.buffer) > self.limit:
            raise ProtocolError("message too large")
        if plain[:1] == b"\x01":
            return None
        data, self.buffer = bytes(self.buffer), bytearray()
        try:
            return json.loads(data)
        except ValueError:
            raise ProtocolError("bad JSON")


# --- client side (blocking sockets) ---

def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        part = sock.recv(n - len(buf))
        if not part:
            raise Closed("the server closed the connection")
        buf += part
    return bytes(buf)


def _recv_frame(sock):
    (n,) = struct.unpack(">H", _recv_exact(sock, 2))
    return _recv_exact(sock, n)


def _read_reply_frame(sock):
    """The server's first frame. A server of version 1 starts with a
    plaintext banner instead; say so rather than reading it as a frame."""
    head = _recv_exact(sock, 2)
    if head == b"in":
        raise ProtocolError("this server runs an older inter-agent-comm; ask the host to update it")
    (n,) = struct.unpack(">H", head)
    return _recv_exact(sock, n)


class Client:
    def __init__(self, sock, noise):
        self.sock = sock
        self.noise = noise
        self.opener = Opener(noise)

    def send(self, obj):
        self.sock.sendall(seal(self.noise, obj))

    def send_data(self, data):
        self.sock.sendall(seal_data(self.noise, data))

    def recv(self):
        while True:
            obj = self.opener.feed(_recv_frame(self.sock))
            if obj is not None:
                return obj

    def close(self):
        self.sock.close()


class KeyChanged(ProtocolError):
    """The server shows another key than the one we trust for it."""

    def __init__(self, name, public):
        super().__init__(f"the server {name} shows another key ({fingerprint(public)}) than its "
                         "server file; someone may be posing as it")
        self.name, self.public = name, public


def open_server(host, port, private, timeout=5):
    """Open a connection and run the handshake up to the server's key:
    (sock, noise, name, public key). Nothing that names us is sent yet;
    finish() sends our key once we trust the server's. Raises OSError when
    the address can't be reached or the connection breaks, and
    ProtocolError when the other side isn't an inter-agent-comm server. A ws:// or wss:// URL as host goes
    through the WebSocket gateway (wss.py); port is then unused."""
    if wss.is_url(host):
        sock = wss.open_stream(host, timeout)
    else:
        sock = socket.create_connection((host, port), timeout=timeout)
    try:
        noise = _noise(True, private)
        sock.sendall(_frame(noise.write_message()))
        garbled = ProtocolError("not an inter-agent-comm server, or its answer was garbled on the way")
        try:
            frame = _read_reply_frame(sock)
        except Closed:
            raise garbled from None
        try:
            about = json.loads(noise.read_message(frame))
            name, version = about["name"], about["version"]
        except Exception:   # noise's own errors aren't ValueErrors; deep JSON is a RecursionError
            raise garbled from None
        if version != PROTOCOL:
            raise ProtocolError(f"the server speaks protocol {version!r}, this client {PROTOCOL}; "
                                "update inter-agent-comm (git pull), or ask the host to")
        if not valid_server_name(name):
            raise ProtocolError("the server sent a garbled name")
        public = bytes(noise.noise_protocol.handshake_state.rs.public_bytes)
        return sock, noise, name, public
    except BaseException:
        sock.close()
        raise


def finish(sock, noise, hello):
    """The handshake's last message, once we trust the server's key: our
    key and hello, encrypted so only the holder of that key can read them."""
    try:
        sock.sendall(_frame(noise.write_message(json.dumps(hello).encode())))
        return Client(sock, noise)
    except BaseException:
        sock.close()
        raise


def connect(host, port, private, server_public, hello, timeout=5):
    """open_server(), then finish() if the server shows the key we have for
    it. Raises OSError when the address can't be reached, KeyChanged when
    the server shows another key, and ProtocolError for anything else."""
    sock, noise, name, public = open_server(host, port, private, timeout)
    if public != bytes(server_public):
        sock.close()
        raise KeyChanged(name, public)
    return finish(sock, noise, hello)


# --- server side (asyncio) ---

async def read_frame(reader):
    (n,) = struct.unpack(">H", await reader.readexactly(2))
    return await reader.readexactly(n)


async def accept(reader, writer, private, name):
    """Server half of the handshake. Returns (noise, client public key, hello).
    Says nothing until the client's first message, a bare ephemeral key."""
    noise = _noise(False, private)
    first = await read_frame(reader)
    if len(first) != 32:
        raise ProtocolError("not a client")
    noise.read_message(first)
    writer.write(_frame(noise.write_message(json.dumps({"name": name, "version": PROTOCOL}).encode())))
    await writer.drain()
    state = noise.noise_protocol.handshake_state  # the library drops it when the handshake ends
    hello = json.loads(noise.read_message(await read_frame(reader)))
    return noise, bytes(state.rs.public_bytes), hello if isinstance(hello, dict) else {}
