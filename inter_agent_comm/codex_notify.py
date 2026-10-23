# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Salih Marangoz <salih285@gmail.com>
"""Fixed notices via the official local app-server, or `codex queue`.

Steering adds input to an active turn; it doesn't cancel commands. Never
send chat content as input. An uncertain steer is not retried or queued,
since the server might already have accepted it.
"""

import json
import subprocess
import time

from . import proto

RPC_TIMEOUT = 10
STEERS_PER_HOUR = 6


class RpcError(Exception):
    """A definite rejection by app-server (safe to fall back to queue)."""


class Rpc:
    def __init__(self, ws):
        self.ws = ws
        self.number = 0

    def call(self, method, params):
        self.number += 1
        self.ws.send(json.dumps({"id": self.number, "method": method, "params": params}))
        deadline = time.monotonic() + RPC_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{method} response timed out")
            message = json.loads(self.ws.recv(timeout=remaining))
            if message.get("id") != self.number:
                continue  # Notifications, not a response to this request.
            if "error" in message:
                raise RpcError(str(message["error"]))
            return message["result"]

    def initialize(self):
        self.call("initialize", {
            "clientInfo": {"name": "inter-agent-comm", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        self.ws.send(json.dumps({"method": "initialized", "params": {}}))

    def active_turn(self, thread):
        info = self.call("thread/read", {"threadId": thread, "includeTurns": False})["thread"]
        if info["status"]["type"] != "active":
            return None
        turns = self.call("thread/turns/list", {
            "threadId": info["id"], "limit": 1,
            "sortDirection": "desc", "itemsView": "notLoaded",
        })["data"]
        if turns and turns[0]["status"] == "inProgress":
            return info["id"], turns[0]["id"]
        return None


def steer(request, notice, root, reply):
    from .codex_wait import save

    if request.get("delivery", "auto") == "queue" or not request.get("mentions"):
        return {"fallback": "queue requested"}
    if not reply.get("messages"):
        return {"fallback": "no targeted messages"}
    # All workers for this identity share run.lock, held by the caller.
    # The host's authenticated sender identity is set by the chat server.
    from_host = any(message.get("from") == proto.CONSOLE for message in reply["messages"])
    rate_file = root.parent / "steers.json"
    now = time.time()
    rates = {}
    try:
        if rate_file.exists():
            rates = json.loads(rate_file.read_text())
        recent = [stamp for stamp in rates.get(request["thread"], []) if now - stamp < 3600]
    except (OSError, ValueError, TypeError, AttributeError) as error:
        return {"fallback": "steer rate state unavailable", "steer_error": str(error)}
    if not from_host and len(recent) >= STEERS_PER_HOUR:
        return {"fallback": "hourly steer limit"}
    sent = False
    try:
        # This queries the existing daemon; it never starts or restarts one.
        version = subprocess.run([request["codex"], "app-server", "daemon", "version"],
                                 capture_output=True, text=True, timeout=RPC_TIMEOUT)
        daemon = json.loads(version.stdout)
        if version.returncode != 0 or daemon.get("status") != "running":
            return {"fallback": "no local daemon"}
        from websockets.sync.client import unix_connect
        with unix_connect(daemon["socketPath"], open_timeout=RPC_TIMEOUT, close_timeout=1) as ws:
            rpc = Rpc(ws)
            rpc.initialize()
            active = rpc.active_turn(request["thread"])
            for attempt in range(2):
                if active is None:
                    return {"fallback": "thread idle"}
                thread, turn = active
                if not from_host and attempt == 0:
                    # Reserve before sending: a timeout/crash may follow acceptance.
                    rates[request["thread"]] = recent + [now]
                    save(rate_file, rates)
                sent = True
                try:
                    accepted = rpc.call("turn/steer", {
                        "threadId": thread, "expectedTurnId": turn,
                        "input": [{"type": "text", "text": notice}],
                    })
                except RpcError as error:
                    sent = False  # Explicit rejection; nothing was delivered.
                    if attempt == 1:
                        return {"fallback": "steer rejected", "steer_error": str(error)}
                    active = rpc.active_turn(thread)  # The current turn may have changed.
                    continue
                if accepted.get("turnId") != turn:
                    raise ValueError("steer acknowledged an unexpected turn")
                return {"stage": "steered", "delivery": "steer", "turn": turn}
    except Exception as error:
        # A lost acknowledgment after send is ambiguous; avoid duplicate delivery.
        if sent:
            return {"stage": "delivery_uncertain", "delivery": "steer", "steer_error": str(error)}
        return {"fallback": "steer unavailable", "steer_error": str(error)}


def notify(request, notice, root, reply):
    result = steer(request, notice, root, reply)
    if result.get("stage"):
        return result
    queued = subprocess.run([request["codex"], "queue", "--thread", request["thread"],
                             "--message", notice], capture_output=True, text=True, timeout=60)
    log = root / "queue.log"
    log.touch(mode=0o600)
    log.write_text(queued.stdout + "\n" + queued.stderr)
    return {**result, "stage": "queued" if queued.returncode == 0 else "queue_failed",
            "delivery": "queue", "queue_exit": queued.returncode}
