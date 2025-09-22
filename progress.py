# progress.py
import time
import random
from typing import Optional
import discord

from utils import human_mb, clamp, eph_send

NOTE_GLYPHS = ["𝆕", "𝅘𝅥", "𝅗𝅥", "𝅘𝅥𝅮", "♩", "♪", "♫", "♬"]

class ProgressReporter:
    """Ephemeral, live-updating progress line with a music-note bar."""
    def __init__(self, inter: discord.Interaction):
        self.inter = inter
        self.msg: Optional[discord.Message] = None
        self._last = 0.0

        self.total_tracks = 1
        self.current_index = 1
        self.current_title = ""

        self.percent = 0
        self.d_bytes = 0
        self.t_bytes = 0

        self.bar_width = 30
        self.cancelled = False

    async def start(self):
        if not self.msg:
            self.msg = await eph_send(self.inter, "Process starting…")

    def _bar(self) -> str:
        filled = clamp(int(round(self.percent / 100 * self.bar_width)), 0, self.bar_width)
        notes = "".join(random.choice(NOTE_GLYPHS) for _ in range(filled))
        rest = "-" * (self.bar_width - filled)
        return f"[{notes}{rest}]"

    async def update(self, force: bool = False):
        if not self.msg:
            return
        now = time.time()
        if not force and now - self._last < 0.45:
            return
        self._last = now

        title = (self.current_title or "")
        if len(title) > 70:
            title = title[:67] + "…"

        header = f"**[{self.current_index}/{self.total_tracks}] {title}**"
        size = f"{human_mb(self.d_bytes)}" + (f" / {human_mb(self.t_bytes)}" if self.t_bytes else "")
        content = f"{header}\n{self._bar()}  {self.percent:>3d}% ({size})"
        try:
            await self.msg.edit(content=content)
        except discord.HTTPException:
            pass

    async def replace(self, text: str):
        if not self.msg:
            self.msg = await eph_send(self.inter, text)
        else:
            try:
                await self.msg.edit(content=text)
            except discord.HTTPException:
                pass
