# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Salih Marangoz <salih285@gmail.com>
"""Independent file-transfer checks: isolated localhost server, keys and spool.

Run directly with .venv/bin/python tests/test_files.py or via tests/run.py.
No production server, cache, configuration or downloads are touched.
"""

import asyncio
import contextlib
import errno
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # the repo, for inter_agent_comm
from inter_agent_comm import chat  # noqa: E402
from inter_agent_comm import proto  # noqa: E402
from inter_agent_comm.server import ChatServer


class LocalChat:
    def __init__(self, root):
        self.root = root
        self.keys = {}
        self.servers = root / "servers"
        self.servers.mkdir()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.start()
        for name in ("sender", "receiver", "other-agent"):
            self.register(name)

    def call(self, function, *args):
        async def run():
            return await function if asyncio.iscoroutine(function) else function(*args)
        return asyncio.run_coroutine_threadsafe(run(), self.loop).result(timeout=5)

    def start(self):
        async def create():
            server = ChatServer("filetest", 0, self.root / "server-home", self.root / "works",
                                "127.0.0.1", files=self.root / "spool", trash=self.root / "trash")
            await server.start()
            return server
        self.server = self.call(create())
        record = {"name": "filetest", "port": self.server.port, "addresses": ["127.0.0.1"],
                  "public_key": proto.b64(self.server.public)}
        (self.servers / "filetest.json").write_text(json.dumps(record))

    def stop(self):
        self.call(self.server.stop())

    def close(self):
        self.stop()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=3)
        self.loop.close()

    def client(self, name):
        private = self.keys[name]
        return proto.connect("127.0.0.1", self.server.port, private, self.server.public,
                             {"name": name}, timeout=2)

    def register(self, name, key_label=None):
        key_path = self.root / (key_label or name) / "agents" / f"{name}.key"
        self.keys[name] = proto.load_or_create_key(key_path)
        reply = self.exchange(name, {"op": "join"})
        if reply.get("type") == "error":
            raise AssertionError(reply)
        key = proto.b64(proto.public_key(self.keys[name]))
        self.call(self.server.approve, key)

    def exchange(self, name, request):
        with contextlib.closing(self.client(name)) as client:
            client.send(request)
            return client.recv()

    def cli(self, name, *args):
        home = self.root / name
        env = dict(os.environ, INTER_AGENT_COMM_HOME=str(home), INTER_AGENT_COMM_SERVERS=str(self.servers),
                   INTER_AGENT_COMM_FILES=str(self.root / "unused-client-spool"))
        return subprocess.run([*proto.command("chat"), "-n", name, "--json", *args],
                              capture_output=True, text=True, timeout=15, env=env)


class FileTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="iac_files_test_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.h = LocalChat(self.root)
        self.addCleanup(self.h.close)

    def offer(self, data, name="payload.bin", digest=None):
        with contextlib.closing(self.h.client("sender")) as client:
            client.send({"op": "offer", "to": "receiver", "name": name, "size": len(data),
                         "sha256": digest or hashlib.sha256(data).hexdigest()})
            ready = client.recv()
            self.assertEqual(ready.get("type"), "ready", ready)
            for offset in range(0, len(data), proto.CHUNK):
                client.send_data(data[offset:offset + proto.CHUNK])
            return client.recv()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.02)
        self.fail("server did not finish cleanup")

    def test_binary_cli_roundtrip_and_recipient_restriction(self):
        marker = b"FILE_CONTENT_MUST_NOT_APPEAR_IN_TRANSCRIPT"
        data = marker + bytes(range(256)) * 8192
        source = self.root / "source.bin"
        source.write_bytes(data)
        sent = self.h.cli("sender", "send-file", str(source), "--to", "receiver")
        self.assertEqual(sent.returncode, 0, sent.stderr)
        fid = json.loads(sent.stdout)["id"]
        denied = self.h.exchange("other-agent", {"op": "receive", "id": fid})
        self.assertEqual(denied.get("type"), "error", denied)
        folder = self.root / "downloads"
        received = self.h.cli("receiver", "receive", fid, "--dir", str(folder))
        self.assertEqual(received.returncode, 0, received.stderr)
        saved = folder / source.name
        self.assertEqual(saved.stat().st_size, len(data))
        self.assertEqual(chat.file_digest(saved), hashlib.sha256(data).hexdigest())
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        stored = self.root / "spool" / fid / "data"
        self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        transcript = "".join(p.read_text() for p in (self.root / "works").rglob("transcript.md"))
        self.assertNotIn(marker.decode(), transcript)
        self.assertIn("source.bin", transcript)

    def test_empty_file_roundtrip(self):
        reply = self.offer(b"", "empty.bin")
        self.assertEqual(reply.get("type"), "ok", reply)
        folder = self.root / "downloads"
        received = self.h.cli("receiver", "receive", reply["id"], "--dir", str(folder))
        self.assertEqual(received.returncode, 0, received.stderr)
        self.assertEqual((folder / "empty.bin").stat().st_size, 0)

    def test_bad_hash_keeps_no_offer_or_partial_payload(self):
        reply = self.offer(b"corrupted payload", digest="0" * 64)
        self.assertEqual(reply.get("type"), "error", reply)
        self.assertEqual(reply.get("code"), "bad_hash")
        self.assertEqual(self.h.call(self.h.server.files.used), 0)
        self.assertEqual(list((self.root / "spool").iterdir()), [])

    def test_filename_traversal_and_existing_destination(self):
        data = b"downloaded contents"
        reply = self.offer(data, "../../escape.txt")
        self.assertEqual(reply.get("type"), "ok", reply)
        folder = self.root / "downloads"
        folder.mkdir()
        original = folder / "escape.txt"
        original.write_text("keep this file")
        received = self.h.cli("receiver", "receive", reply["id"], "--dir", str(folder))
        self.assertEqual(received.returncode, 0, received.stderr)
        self.assertEqual(original.read_text(), "keep this file")
        self.assertFalse((self.root / "escape.txt").exists())
        matching = [p for p in folder.iterdir() if p.is_file() and p.read_bytes() == data]
        self.assertEqual(len(matching), 1)

    def test_sender_and_recipient_permissions_are_bound_to_keys(self):
        reply = self.offer(b"old sender's file")
        fid = reply["id"]
        self.h.call(self.h.server.kick, "sender")
        self.h.register("sender", "replacement-sender")
        self.assertEqual(self.h.exchange("sender", {"op": "cancel", "id": fid})["type"], "error")
        self.assertFalse(self.h.exchange("sender", {"op": "files"})["files"])
        self.h.call(self.h.server.kick, "receiver")
        self.h.register("receiver", "replacement-receiver")
        self.assertEqual(self.h.exchange("receiver", {"op": "receive", "id": fid})["type"], "error")
        self.assertFalse(self.h.exchange("receiver", {"op": "files"})["files"])

    def test_size_limits_and_invalid_sizes_rejected_before_upload(self):
        for size in (proto.MAX_FILE + 1, -1, True, "100"):
            with self.subTest(size=size):
                reply = self.h.exchange("sender", {"op": "offer", "to": "receiver", "name": "large.bin",
                                                    "size": size, "sha256": "0" * 64})
                self.assertEqual(reply.get("type"), "error", reply)
        huge = self.root / "too-large.bin"
        with huge.open("wb") as out:
            out.truncate(proto.MAX_FILE + 1)  # sparse; no gigabyte is allocated or hashed
        denied = self.h.cli("sender", "send-file", str(huge), "--to", "receiver")
        self.assertNotEqual(denied.returncode, 0)

    def test_concurrent_uploads_reserve_quota_and_partial_upload_cleans_up(self):
        self.h.call(setattr, self.h.server.files, "total", 150)
        with contextlib.closing(self.h.client("sender")) as first:
            offer = {"op": "offer", "to": "receiver", "name": "one.bin", "size": 100, "sha256": "0" * 64}
            first.send(offer)
            ready = first.recv()
            self.assertEqual(ready.get("type"), "ready", ready)
            second = self.h.exchange("other-agent", dict(offer, name="two.bin"))
            self.assertEqual(second.get("type"), "error", second)
            self.assertEqual(second.get("code"), "full")
            first.send_data(b"half")
        self.wait_for(lambda: not (self.root / "spool" / ready["id"]).exists())
        self.assertEqual(self.h.call(self.h.server.files.used), 0)

    def test_chat_replies_while_upload_waits_for_more_data(self):
        with contextlib.closing(self.h.client("sender")) as client:
            client.send({"op": "offer", "to": "receiver", "name": "pending.bin", "size": 100,
                         "sha256": "0" * 64})
            self.assertEqual(client.recv().get("type"), "ready")
            client.send_data(b"partial")
            reply = self.h.exchange("other-agent", {"op": "send", "text": "chat still responds"})
            self.assertEqual(reply.get("type"), "ok", reply)

    def test_empty_data_frame_cannot_keep_upload_open(self):
        rejected = False
        with contextlib.closing(self.h.client("sender")) as client:
            client.send({"op": "offer", "to": "receiver", "name": "pending.bin", "size": 1,
                         "sha256": hashlib.sha256(b"x").hexdigest()})
            self.assertEqual(client.recv().get("type"), "ready")
            client.send_data(b"")
            client.sock.settimeout(.5)
            try:
                reply = client.recv()
                rejected = isinstance(reply, dict) and reply.get("type") == "error"
            except (ConnectionError, proto.ProtocolError):
                rejected = True
            except socket.timeout:
                pass
        self.assertTrue(rejected, "empty file data kept the upload waiting")

    def test_offer_survives_restart_and_expires(self):
        reply = self.offer(b"persisted across restart")
        fid = reply["id"]
        self.h.stop()
        self.h.start()
        folder = self.root / "downloads"
        received = self.h.cli("receiver", "receive", fid, "--dir", str(folder))
        self.assertEqual(received.returncode, 0, received.stderr)
        self.h.call(setattr, self.h.server.files.files[fid], "created", time.time() - proto.FILE_TTL - 1)
        denied = self.h.exchange("receiver", {"op": "receive", "id": fid})
        self.assertEqual(denied.get("type"), "error", denied)
        self.assertFalse((self.root / "spool" / fid).exists())

    def test_sender_can_cancel_offer(self):
        fid = self.offer(b"cancel me")["id"]
        self.assertEqual(self.h.exchange("other-agent", {"op": "cancel", "id": fid})["type"], "error")
        self.assertEqual(self.h.exchange("sender", {"op": "cancel", "id": fid})["type"], "ok")
        self.assertEqual(self.h.exchange("receiver", {"op": "receive", "id": fid})["type"], "error")
        self.assertFalse((self.root / "spool" / fid).exists())


class DownloadRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="iac_download_test_")
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    @staticmethod
    def client(data, during_data=None, claimed_data=None):
        claimed = data if claimed_data is None else claimed_data
        class FakeClient:
            def __init__(self):
                self.items = iter([{"type": "file", "id": "abc123", "name": "race.bin", "size": len(claimed),
                                    "sha256": hashlib.sha256(claimed).hexdigest()}, data, {"type": "end"}])

            def send(self, request):
                pass

            def recv(self):
                item = next(self.items)
                if isinstance(item, bytes) and during_data:
                    during_data()
                return item
        return FakeClient()

    def test_destination_created_during_download_is_not_overwritten(self):
        target = self.folder / "race.bin"
        client = self.client(b"received bytes", lambda: target.write_text("created by another process"))
        try:
            chat.download(client, {"op": "receive", "id": "abc123"}, self.folder)
        except FileExistsError:
            pass
        self.assertEqual(target.read_text(), "created by another process")

    def test_temp_creation_failure_does_not_delete_another_download(self):
        target = self.folder / "race.bin"
        part = self.folder / "race.bin.part"
        part.write_text("another download owns this")
        choose_path = chat.free_path
        first = True

        def contested_path(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                return target  # name became occupied between checking and O_EXCL
            return choose_path(*args, **kwargs)

        with patch.object(chat, "free_path", side_effect=contested_path):
            try:
                chat.download(self.client(b"new contents"), {"op": "receive", "id": "abc123"}, self.folder)
            except FileExistsError:
                pass
        self.assertTrue(part.exists(), "another download's temp file was removed")
        self.assertEqual(part.read_text(), "another download owns this")

    def test_unsupported_hardlinks_do_not_allow_racy_rename(self):
        race_targets = []
        rename = Path.rename
        replace = os.replace

        def concurrent_create(part, target):
            target = Path(target)
            target.write_text("created while download was finishing")
            race_targets.append(target)
            return rename(part, target)

        def concurrent_replace(part, target):
            target = Path(target)
            target.write_text("created while download was finishing")
            race_targets.append(target)
            return replace(part, target)

        with patch.object(chat.os, "link", side_effect=OSError(errno.EOPNOTSUPP, "hard links unsupported")):
            with patch.object(Path, "rename", autospec=True, side_effect=concurrent_create):
                with patch.object(chat.os, "replace", side_effect=concurrent_replace):
                    try:
                        chat.download(self.client(b"received bytes"), {"op": "receive", "id": "abc123"}, self.folder)
                    except OSError:
                        pass  # refusing this filesystem is safe too
        for target in race_targets:
            self.assertEqual(target.read_text(), "created while download was finishing")
        self.assertFalse(list(self.folder.glob("*.part")))

    def test_dangling_symlink_is_an_occupied_filename(self):
        for suffix in ("", ".part"):
            with self.subTest(suffix=suffix):
                occupied = self.folder / ("race.bin" + suffix)
                occupied.symlink_to(self.folder / "does-not-exist")
                try:
                    self.assertNotEqual(chat.free_path(self.folder, "race.bin"), self.folder / "race.bin")
                finally:
                    occupied.unlink()

    def test_receiver_rejects_checksum_mismatch_and_short_data(self):
        for actual, claimed in ((b"evil", b"good"), (b"x", b"long")):
            with self.subTest(actual=actual, claimed=claimed):
                reply, saved = chat.download(self.client(actual, claimed_data=claimed),
                                             {"op": "receive", "id": "abc123"}, self.folder)
                self.assertEqual(reply["type"], "error")
                self.assertIsNone(saved)
                self.assertFalse(list(self.folder.iterdir()))


def run_checks():
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
                               for cls in (FileTransferTests, DownloadRaceTests))
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == "__main__":
    raise SystemExit(not run_checks())
