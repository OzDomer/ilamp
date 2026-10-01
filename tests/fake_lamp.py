"""
fake_lamp.py - a pretend i_Lamp for testing without hardware.

It copies every behavior we observed in the real lamp:

  - a command before the hello + handshake  -> hangs up    (the 0.4s kick)
  - no heartbeat for a while                 -> hangs up    (the ~14s watchdog)
  - each heartbeat                           -> STATUS reply
  - a change of state                        -> state REPORT
  - a command that changes nothing           -> no reply    (mode 00 at the start)
  - an unknown mode                          -> ignored     (modes 02, 05, 08+)
  - read_state                               -> state answer

The timings are shrunk (watchdog 0.3s instead of ~14s) so tests run fast.
It implements the same small interface as bleak's BleakClient, so Session
can't tell the difference.
"""

import asyncio

from ilamp import protocol as p

# Copied from a real heartbeat reply (frame 5843 in the capture).
STATUS_PACKET = bytes.fromhex(
    "01fe0000410028000000000000000000001f001f0700000800000000000000000000000001036148"
)
VALID_MODES = {0x00, 0x01, 0x03, 0x04, 0x06, 0x07}


def state_message(op: int, cmd: int, state: dict) -> bytes:
    """Build a lamp reply carrying the 6-byte state payload (no padding, like the real lamp)."""
    payload = [state["power"], state["brightness"], *state["rgb"], state["mode"]]
    msg = bytes([p.START, 12, p.Group.LIGHT, op, cmd, *payload, p.END])
    return p.wrap(p.REPLY, msg)


class FakeLamp:
    def __init__(
        self,
        on_disconnect=None,
        watchdog: float = 0.3,
        answer_reads: bool = True,
        fail_writes: bool = False,
    ):
        self.on_disconnect = on_disconnect
        self.watchdog = watchdog
        self.answer_reads = answer_reads  # False: a lamp whose read answers never arrive
        self.fail_writes = fail_writes  # True: every write raises, like a dying link
        self.state = {"power": 1, "brightness": 255, "rgb": [255, 255, 255], "mode": 0}
        self.received: list[bytes] = []  # everything written to us, for assertions
        self.heartbeats = 0
        self._connected = False
        self._notify = None
        self._session_open = False
        self._hello_seen = False
        self._handshakes = 0
        self._watchdog_task = None

    # -- the BleakClient-shaped interface -------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self):
        self._connected = True
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def disconnect(self):
        # bleak fires disconnected_callback for EVERY disconnect, including
        # ones we asked for. Session's _closing flag is what tells them apart.
        self._drop(report=True)

    async def start_notify(self, uuid, callback):
        self._notify = callback

    async def stop_notify(self, uuid):
        self._notify = None

    async def write_gatt_char(self, uuid, data, response=False):
        if not self._connected:
            raise RuntimeError("Not connected")
        if self.fail_writes:
            raise RuntimeError("Write failed")
        data = bytes(data)
        self.received.append(data)
        self._handle(data)

    # -- lamp behavior ----------------------------------------------------------

    def external_change(self, **fields):
        """The lamp changes on its own (a button press) and reports it, like the real one."""
        self.state.update(fields)
        self._reply(state_message(p.Op.REPORT, 0x01, self.state))

    def vanish(self):
        """The lamp is unplugged or walks out of range: the link drops without warning."""
        self._drop(report=True)

    def _handle(self, data: bytes):
        if data == p.HELLO:
            self._hello_seen = True
            return
        ptype = p.packet_type(data)
        if ptype == p.HANDSHAKE:
            self._handshakes += 1
            self._session_open = self._hello_seen and self._handshakes >= 2
            return
        if ptype == p.HEARTBEAT:
            self.heartbeats += 1
            self._last_beat = asyncio.get_running_loop().time()
            self._reply(STATUS_PACKET)
            return
        if ptype == p.COMMAND:
            if not self._session_open:
                self._drop(report=True)  # the 0.4s kick
                return
            self._command(data[p.WRAPPER_SIZE :])

    def _command(self, msg: bytes):
        op, cmd, args = msg[3], msg[4], msg[5:]
        if op == p.Op.READ and cmd == p.Cmd.STATE:
            if self.answer_reads:
                self._reply(state_message(p.Op.READ, p.Cmd.STATE, self.state))
            return
        if op != p.Op.WRITE:
            return
        before = dict(self.state, rgb=list(self.state["rgb"]))
        if cmd == p.Cmd.POWER:
            self.state["power"] = args[0]
        elif cmd == p.Cmd.COLOR:
            self.state["brightness"] = args[0]
            self.state["rgb"] = list(args[1:4])
        elif cmd == p.Cmd.MODE and args[0] in VALID_MODES:
            self.state["mode"] = args[0]
        if self.state != before:  # the real lamp only reports changes
            self._reply(state_message(p.Op.REPORT, 0x01, self.state))

    def _reply(self, packet: bytes):
        if self._notify is not None:
            # Real notifications arrive a moment later, not mid-write.
            asyncio.get_running_loop().call_soon(self._notify, None, bytearray(packet))

    # -- connection drops ---------------------------------------------------------

    async def _watchdog_loop(self):
        loop = asyncio.get_running_loop()
        self._last_beat = loop.time()
        while self._connected:
            await asyncio.sleep(self.watchdog / 10)
            if loop.time() - self._last_beat > self.watchdog:
                self._drop(report=True)

    def _drop(self, report: bool):
        if not self._connected:
            return
        self._connected = False
        if self._watchdog_task is not None and self._watchdog_task is not asyncio.current_task():
            self._watchdog_task.cancel()
        if report and self.on_disconnect is not None:
            self.on_disconnect()


def factory_for(lamp_holder: list, **kwargs):
    """
    Build a client_factory for Session that creates a FakeLamp and also
    stores it in lamp_holder[0], so the test can inspect it afterwards.
    """

    async def factory(name, on_disconnect):
        lamp = FakeLamp(on_disconnect=on_disconnect, **kwargs)
        lamp_holder.append(lamp)
        return lamp

    return factory
