# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Salih Marangoz <salih285@gmail.com>
"""Official Unix WebSocket RPC, races and delivery guarantees; no live Codex."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from websockets.sync.server import unix_serve

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # the repo, for inter_agent_comm
from inter_agent_comm import codex_notify  # noqa: E402


class CodexNotifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex_notify_test_")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "wake-1"
        self.root.mkdir()
        self.socket = self.base / "server.sock"
        self.queue = self.base / "queued.json"
        binary = self.base / "codex"
        binary.write_text(f"#!{sys.executable}\n" + f"""import json, sys
from pathlib import Path
if sys.argv[1:]==['app-server','daemon','version']:
    print(json.dumps({{'status':'running', 'socketPath':{str(self.socket)!r}}}))
else:
    Path({str(self.queue)!r}).write_text(json.dumps(sys.argv[1:]))
""")
        binary.chmod(0o755)
        self.request = {"codex": str(binary), "thread": "own-thread", "mentions": True}
        self.reply = {"messages": [{"from": "agent-x", "text": "UNTRUSTED-CHAT-CONTENT"}]}
        self.notice = "Read the saved untrusted reply with chat wake-read /tmp/private-path"
        self.status = "active"
        self.turn = "turn-a"
        self.behavior = "accept"
        self.calls = []
        self.server = unix_serve(self.handler, str(self.socket))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=2)

    def handler(self, ws):
        for raw in ws:
            message = json.loads(raw)
            self.calls.append(message)
            method = message["method"]
            if "id" not in message:
                continue
            # Unrelated notifications must not be mistaken for an RPC response.
            ws.send(json.dumps({"method": "thread/status/changed", "params": {}}))
            if method == "initialize":
                result = {"platformOs": "linux"}
            elif method == "thread/read":
                result = {"thread": {"id": "own-thread", "status": {"type": self.status}}}
            elif method == "thread/turns/list":
                result = {"data": [{"id": self.turn, "status": "inProgress"}]}
            elif method == "turn/steer":
                if self.behavior == "lost_ack":
                    ws.close()
                    return
                if self.behavior in ("reject", "race_idle", "race_turn"):
                    if self.behavior == "race_idle":
                        self.status = "idle"
                    if self.behavior == "race_turn":
                        self.turn = "turn-b"
                        self.behavior = "accept"
                    ws.send(json.dumps({"id": message["id"], "error": {
                        "code": -32600, "message": "active turn mismatch"}}))
                    continue
                result = {"turnId": message["params"]["expectedTurnId"]}
            else:
                self.fail(f"Unexpected RPC method {method}")
            ws.send(json.dumps({"id": message["id"], "result": result}))

    def notify(self):
        return codex_notify.notify(self.request, self.notice, self.root, self.reply)

    def steers(self):
        return [x for x in self.calls if x["method"] == "turn/steer"]

    def test_active_turn_receives_only_fixed_notice_with_turn_precondition(self):
        out = self.notify()
        self.assertEqual(out["stage"], "steered")
        self.assertEqual(out["turn"], "turn-a")
        self.assertFalse(self.queue.exists())
        self.assertEqual(self.steers()[0]["params"], {
            "threadId": "own-thread", "expectedTurnId": "turn-a",
            "input": [{"type": "text", "text": self.notice}],
        })
        self.assertNotIn("UNTRUSTED-CHAT-CONTENT", json.dumps(self.calls))
        listing = next(x for x in self.calls if x["method"] == "thread/turns/list")
        self.assertEqual(listing["params"]["itemsView"], "notLoaded")
        self.assertEqual((self.base / "steers.json").stat().st_mode & 0o777, 0o600)

    def test_idle_thread_uses_official_queue(self):
        self.status = "idle"
        out = self.notify()
        self.assertEqual(out["stage"], "queued")
        self.assertEqual(out["fallback"], "thread idle")
        self.assertFalse(self.steers())
        self.assertEqual(json.loads(self.queue.read_text()), [
            "queue", "--thread", "own-thread", "--message", self.notice])

    def test_room_wait_and_explicit_queue_never_connect_to_daemon(self):
        for options in ({"mentions": False}, {"delivery": "queue"}):
            self.request.update(mentions=True, delivery="auto")
            self.request.update(options)
            self.assertEqual(self.notify()["stage"], "queued")
        self.assertFalse(self.calls)

    def test_turn_finishes_between_read_and_steer_falls_back_to_queue(self):
        self.behavior = "race_idle"
        out = self.notify()
        self.assertEqual(out["stage"], "queued")
        self.assertEqual(out["fallback"], "thread idle")
        self.assertEqual(len(self.steers()), 1)

    def test_new_active_turn_after_explicit_rejection_is_steered_once(self):
        self.behavior = "race_turn"
        out = self.notify()
        self.assertEqual(out["stage"], "steered")
        self.assertEqual(out["turn"], "turn-b")
        self.assertEqual([x["params"]["expectedTurnId"] for x in self.steers()], ["turn-a", "turn-b"])
        self.assertFalse(self.queue.exists())

    def test_definite_rejection_falls_back_without_looping(self):
        self.behavior = "reject"
        self.assertEqual(self.notify()["stage"], "queued")
        self.assertEqual(len(self.steers()), 2)

    def test_lost_ack_never_queues_a_possible_duplicate(self):
        self.behavior = "lost_ack"
        out = self.notify()
        self.assertEqual(out["stage"], "delivery_uncertain")
        self.assertFalse(self.queue.exists())
        self.assertEqual(len(self.steers()), 1)

    def test_unavailable_daemon_falls_back_to_queue(self):
        self.stop_server()
        self.assertEqual(self.notify()["stage"], "queued")
        self.assertFalse(self.calls)

    def test_hourly_cap_persists_and_host_bypasses_it(self):
        rates = self.base / "steers.json"
        rates.write_text(json.dumps({"own-thread": [time.time()] * codex_notify.STEERS_PER_HOUR}))
        self.assertEqual(self.notify()["fallback"], "hourly steer limit")
        self.assertFalse(self.calls)
        self.reply["messages"][0]["from"] = "host"
        self.assertEqual(self.notify()["stage"], "steered")
        self.reply["messages"][0]["from"] = "agent-x"
        rates.write_text(json.dumps({"own-thread": [time.time() - 3601]}))
        self.assertEqual(self.notify()["stage"], "steered")
        self.assertEqual(len(json.loads(rates.read_text())["own-thread"]), 1)

    def test_bad_rate_state_queues_instead_of_resetting_cap(self):
        (self.base / "steers.json").write_text("corrupt")
        self.assertEqual(self.notify()["stage"], "queued")
        self.assertFalse(self.calls)


def run_checks():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(CodexNotifyTests)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == "__main__":
    raise SystemExit(not run_checks())
