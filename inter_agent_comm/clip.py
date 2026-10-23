#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Salih Marangoz <salih285@gmail.com>
"""Puts stdin on the X11 clipboard and holds it until another program takes
the clipboard (or an hour passes). server.py runs this when no clipboard tool
(wl-copy, xclip, xsel) is installed; tkinter ships with Python."""

import sys
import time
import tkinter

text = sys.stdin.read()
root = tkinter.Tk()
root.withdraw()
root.clipboard_clear()
root.clipboard_append(text)
root.update()
end = time.time() + 3600
while time.time() < end:
    try:
        # Empty once another program owns the clipboard.
        if not root.tk.call("selection", "own", "-selection", "CLIPBOARD"):
            break
        root.update()
    except tkinter.TclError:
        break
    time.sleep(0.1)
