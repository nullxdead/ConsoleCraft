"""Optional local Discord Rich Presence; never blocks launcher or shares account data."""
from __future__ import annotations

import asyncio
import queue
import threading
import time


class DiscordPresence:
    def __init__(self, app_id: str):
        self.app_id = app_id
        self.messages = queue.Queue()
        self.thread = None

    def set(self, details: str, state: str = ""):
        if not self.app_id:
            return
        if self.thread is None:
            self.thread = threading.Thread(target=self._worker, daemon=True, name="discord-presence")
            self.thread.start()
        self.messages.put((details[:128], state[:128]))

    def close(self):
        if self.thread:
            self.messages.put(None)
            self.thread.join(timeout=1)
            self.thread = None

    def _worker(self):
        from pypresence import Presence
        loop = None
        rpc = None
        current = None
        started = int(time.time())
        try:
            while True:
                try:
                    current = self.messages.get(timeout=20)
                except queue.Empty:
                    pass
                if current is None:
                    break
                try:
                    if rpc is None:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        rpc = Presence(self.app_id, loop=loop, connection_timeout=2, response_timeout=2)
                        rpc.connect()
                    rpc.update(details=current[0], state=current[1] or None, start=started,
                               buttons=[{"label": "Discord", "url": "https://discord.gg/FtqBFt58w"}])
                except Exception:
                    if rpc is not None:
                        try:
                            rpc.close()
                        except Exception:
                            pass
                    rpc = None
                    if loop is not None and not loop.is_closed():
                        loop.close()
                    loop = None
        finally:
            if rpc is not None:
                try:
                    rpc.close()
                except Exception:
                    pass
            if loop is not None and not loop.is_closed():
                loop.close()

