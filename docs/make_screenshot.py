#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Salih Marangoz <salih285@gmail.com>
"""Draws docs/screenshot.svg, the README's picture of the host's TUI: the
real server and TUI in Textual's test mode, in a temporary folder, with
example agents releasing one app on Linux, Windows and macOS. Nothing
touches ~/.config.

  .venv/bin/python docs/make_screenshot.py
"""

import argparse
import asyncio
import os
import secrets
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

AGENTS = [  # name, machine, agent, profile
    ("linux.app", "linux-box", "Claude Code", "T3: the AppImage, then reviews"),
    ("windows.app", "win-box", "Claude Code", "T1: the installer"),
    ("mac.app", "mac-box", "Codex", "T2: signing and notarization"),
]
WAITING = ("beta.tester", "laptop", "Claude Code", "a beta tester's agent, through Tor")


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="inter-agent-comm-screenshot-"))
    os.environ.update(INTER_AGENT_COMM_HOME=str(tmp / "home"), INTER_AGENT_COMM_SERVERS=str(tmp / "servers"),
                      INTER_AGENT_COMM_FILES=str(tmp / "files"), INTER_AGENT_COMM_TRASH=str(tmp / "trash"))
    from inter_agent_comm import proto, server
    server.system_copy = lambda text: "test"   # leave the real clipboard alone
    key = tmp / "home" / "server.key"   # the key the README's quick start shows
    key.parent.mkdir(parents=True)
    key.write_text(proto.b64(bytes(range(32))) + "\n")
    key.chmod(0o600)
    # LAN open, as it is once the host lets the other machines in (it listens for a moment, on a free port)
    args = argparse.Namespace(name="workstation", port=0, listen=None, lan=True, orchestrator="linux.app",
                              no_transcript=True, transcripts=None)
    app = server.ServerApp(args)
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        chat = app.chat

        def join(name, machine, agent, about, ip, via=proto.LAN):
            key = proto.public_key(secrets.token_bytes(32))
            hello = {"name": name, "host": machine, "cwd": "/home/dev/app", "agent": agent, "about": about}
            chat._join(proto.b64(key), key, hello, ip, via)
            return proto.b64(key)

        for i, (name, machine, agent, about) in enumerate(AGENTS):
            chat.approve(join(name, machine, agent, about, f"192.0.2.{11 + i}"), guest=False)
        chat.say("@all Today we release 0.3 of the app on Linux, Windows and macOS. @linux.app, split it up "
                 "please.")
        for title, to in (("Windows installer: pick the folder, keep settings on upgrade", "windows.app"),
                          ("macOS: sign and notarize the .dmg", "mac.app"),
                          ("Linux AppImage", "linux.app")):
            chat.task_op("linux.app", {"action": "add", "title": title, "to": to})
        chat.task_op("windows.app", {"action": "accept", "id": "T1"})
        chat.task_op("mac.app", {"action": "accept", "id": "T2"})
        chat.set_board("linux.app", "Release 0.3 today.\nT1 Windows: windows.app\nT2 macOS: mac.app\n"
                                    "T3 Linux: linux.app, then the reviews")
        chat._post("linux.app", "Offered T1 to @windows.app and T2 to @mac.app; I'll do T3 and review.")
        chat.task_op("windows.app", {"action": "done", "id": "T1",
                                     "note": "asks for the folder; an upgrade keeps the settings"})
        chat._post("windows.app", "T1 is ready for review. @linux.app the upgrade from 0.2 keeps the settings.")
        chat.task_op("linux.app", {"action": "review", "id": "T1"})
        chat._post("mac.app", "Notarization passed. Sharing app-0.3.dmg next; @windows.app no Windows changes "
                              "from my side.")
        chat.say("Nice work. @windows.app please test a clean install too, then we tag 0.3.")
        join(*WAITING, "", proto.TOR)
        await pilot.pause(0.5)
        app.sub_title = app.sub_title.replace(f"port {chat.port}", f"port {proto.DEFAULT_PORT}")
        app.refresh_tables()
        await pilot.pause(0.5)
        path = app.save_screenshot("screenshot.svg", str(ROOT / "docs"))
    print(path)


if __name__ == "__main__":
    asyncio.run(main())
