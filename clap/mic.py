"""
mic.py - the microphone, as a stream of 20 ms blocks. Device I/O only.

sounddevice (PortAudio) delivers audio from its own thread, by callback.
Nothing in asyncio may be touched from there, so the callback hands each
block to the event loop with call_soon_threadsafe and the loop puts it in
a queue. The detector then reads the queue like any other async source.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Self

import numpy as np
import sounddevice as sd


def list_devices() -> list[tuple[int, str]]:
    """(index, name) of every device that can record."""
    return [(i, d["name"]) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]


def find_device(wanted: str | None) -> int | None:
    """
    None -> the system default input. A number -> that device index.
    Anything else -> the first input whose name contains it (case-insensitive).
    """
    if wanted is None:
        return None
    if wanted.isdigit():
        return int(wanted)
    for index, name in list_devices():
        if wanted.lower() in name.lower():
            return index
    raise LookupError(f"No input device matching {wanted!r}. Try --list-devices.")


class Microphone:
    """
    async with Microphone(device) as mic:
        async for block in mic.blocks():
            ...

    Blocks are float32 arrays of `block_size` samples, mono, -1..1.
    """

    def __init__(self, device: int | None = None, sample_rate: int = 16_000, block_ms: int = 20):
        self.device = device
        self.sample_rate = sample_rate
        self.block_size = sample_rate * block_ms // 1000
        self._queue: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self._stream: sd.InputStream | None = None
        self.dropped = 0  # blocks the loop was too slow to take (should stay 0)

    async def __aenter__(self) -> Self:
        loop = asyncio.get_running_loop()

        def callback(indata: np.ndarray, frames: int, _time, status: sd.CallbackFlags) -> None:
            # PortAudio's thread. Copy (indata is reused) and hand over.
            if status.input_overflow:
                self.dropped += 1
            loop.call_soon_threadsafe(self._queue.put_nowait, indata[:, 0].copy())

        self._stream = sd.InputStream(
            device=self.device,
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            blocksize=self.block_size,
            callback=callback,
        )
        self._stream.start()
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    async def blocks(self) -> AsyncIterator[np.ndarray]:
        while True:
            yield await self._queue.get()
