# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Salih Marangoz <salih285@gmail.com>
"""Starts one of the commands: `python path/to/inter_agent_comm COMMAND ...`,
which is what the launchers (chat, server, codex-bridge) run, so the code
works from a clone or a read-only copy with no install; or
`python -m inter_agent_comm COMMAND ...`."""

import importlib
import sys
from pathlib import Path

if not __package__:   # run by path: import the package from the folder above it
    sys.path[0] = str(Path(__file__).resolve().parent.parent)

# codex-wait: the detached worker of `chat wait --wake-codex`.
COMMANDS = {"chat": "chat", "server": "server", "codex-bridge": "codex_bridge", "codex-wait": "codex_wait"}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(f"usage: inter_agent_comm {{{','.join(COMMANDS)}}} [args...]")
    name = sys.argv.pop(1)
    sys.argv[0] = name    # argparse names itself after it
    importlib.import_module(f"inter_agent_comm.{COMMANDS[name]}").main()


if __name__ == "__main__":
    main()
