# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Salih Marangoz <salih285@gmail.com>
"""Bridge failure and concurrency regressions, without network or real Codex calls."""

import argparse
import sys
import datetime as dt
import io
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # the repo, for inter_agent_comm
from inter_agent_comm import codex_bridge  # noqa: E402


def options(path):
    return argparse.Namespace(name="test-agent", state=str(path), thread="test-thread",
                              per_hour=1, idle_after=1800, server=None, codex="fake-codex",
                              poll=1, renotify=900, notice=0)


def message(n=1, text="peer message"):
    return {"id": n, "ts": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "from": "other-agent", "kind": "msg", "text": text}


class BridgeRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="iac_bridge_regression_")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)

    def read(self, notice=0):
        args = options(self.path)
        args.notice = notice
        output = io.StringIO()
        with redirect_stdout(output):
            codex_bridge.read_inbox(args)
        return output.getvalue()

    @staticmethod
    def notice_number(run):
        notice = run.call_args.args[0][-1]
        return int(notice.split("--notice ", 1)[1].splitlines()[0])

    def test_manual_reads_and_stale_notice_do_not_queue_duplicates(self):
        relay = codex_bridge.Bridge(options(self.path))
        success = subprocess.CompletedProcess([], 0, "queued", "")

        def deliver(n):
            data = message(n, f"message {n}")
            data["from"] = "host"
            relay.step([data])

        with patch.object(codex_bridge.subprocess, "run", return_value=success) as run:
            deliver(1)
            first = self.notice_number(run)
            self.assertIn("message 1", self.read())
            self.assertEqual(relay.inbox.acked(), 0)
            deliver(2)
            self.assertEqual(run.call_count, 1, "manual read acknowledged a queued notice")
            self.assertIn("message 2", self.read(first))

            deliver(3)
            second = self.notice_number(run)
            self.assertGreater(second, first)
            self.assertEqual(run.call_count, 2)
            self.assertIn("message 3", self.read(first))
            deliver(4)
            self.assertEqual(run.call_count, 2, "an old notice acknowledged the new notice")
            self.assertEqual(relay.inbox.acked(), first)
            self.assertIn("message 4", self.read(second))

            deliver(5)
            self.assertEqual(run.call_count, 3, "a consumed notice prevented the next wake")

    def test_numbered_notice_is_acknowledged_after_manual_read_drains_inbox(self):
        relay = codex_bridge.Bridge(options(self.path))
        success = subprocess.CompletedProcess([], 0, "queued", "")
        with patch.object(codex_bridge.subprocess, "run", return_value=success) as run:
            relay.step([message(1)])
            number = self.notice_number(run)
            self.assertIn("peer message", self.read())
            self.assertEqual(relay.inbox.acked(), 0)
            self.assertEqual(self.read(number).strip(), "No new messages.")
            self.assertEqual(relay.inbox.acked(), number)
            data = message(2)
            data["from"] = "host"
            relay.step([data])
            self.assertEqual(run.call_count, 2)

    def test_queue_exceptions_preserve_messages_and_wake_budget(self):
        for i, error in enumerate((subprocess.TimeoutExpired("fake-codex", 60),
                                   FileNotFoundError("fake-codex"))):
            with self.subTest(error=type(error).__name__):
                relay = codex_bridge.Bridge(options(self.path / str(i)))
                success = subprocess.CompletedProcess([], 0, "queued", "")
                data = message()
                with patch.object(codex_bridge.subprocess, "run", side_effect=[error, success]) as run:
                    relay.step([data])
                    self.assertEqual(relay.inbox.messages(), [data])
                    self.assertEqual(relay.inbox.read_pos(), 0)
                    self.assertFalse(relay.agent_wakes)
                    relay.maybe_notify()
                    self.assertEqual(run.call_count, 2)
                    self.assertEqual(relay.notified, 1)

    def test_concurrent_append_and_read_keeps_utf8_json_complete(self):
        inbox = codex_bridge.Inbox(self.path)
        batch = [message(n, "ğ" * 20000) for n in range(120)]
        done, errors = threading.Event(), []

        def append():
            try:
                inbox.append(batch)
            except Exception as e:
                errors.append(type(e).__name__)
            finally:
                done.set()

        writer = threading.Thread(target=append, daemon=True)
        writer.start()
        while not done.is_set():
            try:
                inbox.messages()
            except Exception as e:
                errors.append(type(e).__name__)
                break
        writer.join(timeout=5)
        self.assertFalse(writer.is_alive(), "append did not finish")
        self.assertFalse(errors, errors)
        self.assertEqual(inbox.messages(), batch)

    def test_second_listener_is_rejected_and_lock_released_after_exit(self):
        relay = codex_bridge.Bridge(options(self.path))
        entered, stop = threading.Event(), threading.Event()
        errors = []

        class Finished(Exception):
            pass

        def listen():
            entered.set()
            stop.wait(timeout=5)
            raise Finished()

        def run():
            try:
                relay.run()
            except Finished:
                pass
            except BaseException as e:
                errors.append(type(e).__name__)

        with patch.object(relay, "listen", side_effect=listen):
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=2), "first bridge did not start")
                other = codex_bridge.Bridge(options(self.path))
                with patch.object(other, "listen", side_effect=AssertionError("second bridge listened")):
                    with self.assertRaisesRegex(SystemExit, "already running"):
                        other.run()
            finally:
                stop.set()
                worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors, errors)
        restarted = codex_bridge.Bridge(options(self.path))
        with patch.object(restarted, "listen", side_effect=Finished):
            with self.assertRaises(Finished):
                restarted.run()


def run_checks():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(BridgeRegressionTests)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == "__main__":
    raise SystemExit(not run_checks())
