# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Salih Marangoz <salih285@gmail.com>
"""One-shot wake regressions: real detached workers, fake chat and Codex."""

import argparse
import contextlib
import io
import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from websockets.sync.server import unix_serve

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # the repo, for inter_agent_comm
from inter_agent_comm import codex_wait  # noqa: E402


class CodexWaitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex_wait_test_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pids = []
        self.addCleanup(self.stop_workers)
        client = self.root / "fake_chat.py"
        client.write_text("""import json, os, sys, time
from pathlib import Path
Path(os.environ['FAKE_REQUEST']).write_text(json.dumps({
    'argv': sys.argv[1:], 'session': os.environ.get('INTER_AGENT_COMM_SESSION')}))
if os.environ.get('FAKE_FAIL_ONCE'):
    first = Path(os.environ['FAKE_FAIL_ONCE'])
    if not first.exists():
        first.touch()
        print('Lost the connection', file=sys.stderr)
        sys.exit(1)
gate = Path(os.environ['FAKE_GATE'])
deadline = time.monotonic() + min(10, float(sys.argv[sys.argv.index('--timeout') + 1]))
while not gate.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
print(os.environ['FAKE_REPLY'] if gate.exists() else json.dumps({'type':'messages', 'timeout':True}))
sys.exit(int(os.environ.get('FAKE_WAIT_EXIT', '0')))
""")
        binary = self.root / "codex"
        binary.write_text(f"#!{sys.executable}\n" + """import json, os, sys
from pathlib import Path
if sys.argv[1:] == ['app-server','daemon','version']:
    if os.environ.get('FAKE_RPC_SOCKET'):
        print(json.dumps({'status':'running', 'socketPath':os.environ['FAKE_RPC_SOCKET']}))
        sys.exit(0)
    sys.exit(1)
Path(os.environ['FAKE_QUEUE']).write_text(json.dumps(sys.argv[1:]))
sys.exit(int(os.environ.get('FAKE_QUEUE_EXIT', '0')))
""")
        binary.chmod(0o755)
        self.args = argparse.Namespace(name="test-agent", wake_codex="test-thread", timeout=2,
                                       mentions=True, server="test-server", address="127.0.0.1:42",
                                       agent="Codex")
        self.reply = {"type": "messages", "messages": [
            {"id": 7, "ts": "2026-10-10 12:00:00", "from": "peer", "kind": "msg",
             "text": "@test-agent untrusted $(touch /tmp/nope) SECRET-PEER-MESSAGE"}]}
        env = {"PATH": str(self.root) + os.pathsep + os.environ.get("PATH", ""),
               "FAKE_REQUEST": str(self.root / "request"), "FAKE_GATE": str(self.root / "gate"),
               "FAKE_QUEUE": str(self.root / "queue"), "FAKE_REPLY": json.dumps(self.reply)}
        env_patch = patch.dict(os.environ, env)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        client_patch = patch.object(codex_wait, "CLIENT", [sys.executable, str(client)])
        client_patch.start()
        self.addCleanup(client_patch.stop)

    def stop_workers(self):
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass

    def arm(self):
        result = codex_wait.arm(self.args, ("test-session", os.getpid()), state=self.root / "state")
        self.pids.append(result["pid"])
        return result, Path(result["path"])

    def finished(self, path):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = json.loads((path / "status.json").read_text())
            if "finished_at" in status:
                return status
            time.sleep(0.01)
        self.fail(f"worker did not finish: {path}")

    def release(self):
        (self.root / "gate").touch()

    def test_detached_wait_preserves_reply_and_queues_only_fixed_notice(self):
        armed, path = self.arm()
        self.assertEqual(os.getsid(armed["pid"]), armed["pid"])
        self.assertFalse((self.root / "queue").exists(), "arm waited for the message")
        self.release()
        self.assertEqual(self.finished(path)["stage"], "queued")
        self.assertEqual(json.loads((path / "reply.json").read_text()), self.reply)
        argv = json.loads((self.root / "queue").read_text())
        self.assertEqual(argv[:4], ["queue", "--thread", "test-thread", "--message"])
        self.assertIn("wake-read", argv[4])
        self.assertIn(str(path), argv[4])
        self.assertNotIn("SECRET-PEER-MESSAGE", argv[4])
        request = json.loads((self.root / "request").read_text())
        self.assertIn("--mentions", request["argv"])
        self.assertIn("test-server", request["argv"])
        self.assertIn("127.0.0.1:42", request["argv"])
        self.assertEqual(request["session"], f"test-session:{os.getpid()}")
        for file in path.iterdir():
            self.assertEqual(file.stat().st_mode & 0o777, 0o600, file.name)
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_second_wait_refused_until_worker_exits_then_rearm_works(self):
        armed, path = self.arm()
        with self.assertRaisesRegex(ValueError, "already running"):
            self.arm()
        self.release()
        self.finished(path)
        os.waitpid(armed["pid"], 0)
        self.pids.remove(armed["pid"])
        _, second = self.arm()
        self.assertNotEqual(path, second)
        self.assertEqual(self.finished(second)["stage"], "queued")

    def test_detached_worker_steers_saved_reply_into_active_turn(self):
        sock = self.root / "rpc.sock"
        calls = []
        def handler(ws):
            for raw in ws:
                message = json.loads(raw)
                calls.append(message)
                if "id" not in message:
                    continue
                method = message["method"]
                if method == "thread/read":
                    result = {"thread": {"id": "test-thread", "status": {"type": "active"}}}
                elif method == "thread/turns/list":
                    result = {"data": [{"id": "active-turn", "status": "inProgress"}]}
                elif method == "turn/steer":
                    result = {"turnId": "active-turn"}
                else:
                    result = {}
                ws.send(json.dumps({"id": message["id"], "result": result}))
        with unix_serve(handler, str(sock)) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                os.environ["FAKE_RPC_SOCKET"] = str(sock)
                _, path = self.arm()
                self.release()
                status = self.finished(path)
            finally:
                server.shutdown()
                thread.join(timeout=2)
        self.assertEqual(status["stage"], "steered")
        self.assertEqual(status["turn"], "active-turn")
        self.assertFalse((self.root / "queue").exists())
        self.assertEqual(json.loads((path / "reply.json").read_text()), self.reply)
        params = next(x["params"] for x in calls if x["method"] == "turn/steer")
        self.assertEqual(params["expectedTurnId"], "active-turn")
        self.assertIn(str(path), params["input"][0]["text"])
        self.assertNotIn("SECRET-PEER-MESSAGE", params["input"][0]["text"])

    def test_timeout_does_not_queue_a_wake(self):
        os.environ["FAKE_REPLY"] = json.dumps({"type": "messages", "messages": [], "timeout": True})
        _, path = self.arm()
        self.release()
        self.assertEqual(self.finished(path)["stage"], "no_messages")
        self.assertFalse((self.root / "queue").exists())

    def test_wait_error_does_not_queue_a_wake(self):
        os.environ.update(FAKE_REPLY=json.dumps({"type": "error", "code": "removed", "message": "removed"}),
                          FAKE_WAIT_EXIT="1")
        _, path = self.arm()
        self.release()
        self.assertEqual(self.finished(path)["stage"], "wait_failed")
        self.assertFalse((self.root / "queue").exists())

    def test_queue_failure_preserves_consumed_message_for_manual_read(self):
        os.environ["FAKE_QUEUE_EXIT"] = "1"
        _, path = self.arm()
        self.release()
        self.assertEqual(self.finished(path)["stage"], "queue_failed")
        self.assertEqual(json.loads((path / "reply.json").read_text()), self.reply)

    def test_dropped_connection_retries_and_eventually_delivers(self):
        self.args.timeout = 7
        os.environ["FAKE_FAIL_ONCE"] = str(self.root / "first_attempt")
        _, path = self.arm()
        self.release()
        status = self.finished(path)
        self.assertEqual(status["stage"], "queued")
        self.assertEqual(status["retries"], 1)
        self.assertEqual(json.loads((path / "reply.json").read_text()), self.reply)

    def test_default_wait_is_bounded_and_releases_its_lock(self):
        self.args.timeout = 0
        with patch.object(codex_wait, "MAX_WAIT", 0.05):
            armed, path = self.arm()
        self.assertEqual(self.finished(path)["stage"], "no_messages")
        self.assertFalse((self.root / "queue").exists())
        os.waitpid(armed["pid"], 0)
        self.pids.remove(armed["pid"])
        self.release()
        _, second = self.arm()
        self.assertEqual(self.finished(second)["stage"], "queued")

    def test_missing_codex_does_not_start_a_wait(self):
        with patch.object(codex_wait.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "not on PATH"):
                self.arm()
        self.assertFalse((self.root / "request").exists())

    def test_cli_defaults_to_own_thread_and_does_not_connect_in_parent(self):
        from inter_agent_comm import chat
        with patch.object(sys, "argv", ["chat", "-n", "test-agent", "wait", "--wake-codex"]), \
                patch.dict(os.environ, {"CODEX_THREAD_ID": "own-thread"}), \
                patch.object(chat, "resolve_name", return_value="test-agent"), \
                patch.object(chat, "session", return_value=None), \
                patch.object(chat, "connect", side_effect=AssertionError("parent connected")), \
                patch.object(codex_wait, "arm", return_value={"pid": 1, "path": "/tmp/fake"}) as arm, \
                contextlib.redirect_stdout(io.StringIO()):
            chat.main()
        self.assertEqual(arm.call_args.args[0].wake_codex, "own-thread")

    def test_missing_thread_rejected_before_claiming_an_identity(self):
        from inter_agent_comm import chat
        with patch.object(sys, "argv", ["chat", "wait", "--wake-codex"]), \
                patch.dict(os.environ, {"CODEX_THREAD_ID": ""}), \
                patch.object(chat, "resolve_name", side_effect=AssertionError("claimed identity")), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            chat.main()
        self.assertEqual(error.exception.code, 2)

    def test_unbounded_or_negative_timeout_rejected_before_starting(self):
        from inter_agent_comm import chat
        for value in ("-1", "nan", "inf"):
            with self.subTest(timeout=value), \
                    patch.object(sys, "argv", ["chat", "wait", "--wake-codex", "test-thread", "--timeout", value]), \
                    patch.object(chat, "resolve_name", side_effect=AssertionError("claimed identity")), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                chat.main()
            self.assertEqual(error.exception.code, 2)


def run_checks():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(CodexWaitTests)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == "__main__":
    raise SystemExit(not run_checks())
