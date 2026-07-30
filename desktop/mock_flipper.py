"""A simulated Flipper, with the same interface as CFPClient.

Used for developing and testing the agent without the physical device connected.
Responds exactly like the real firmware: only 'ping' and 'info' succeed, the rest
of the firmware commands respond ERR not_implemented.

The IR bruteforce commands are simulated with real state (a queue, a counter), because
the desktop side polls them in a loop: a stub that always answered the same thing would
leave that loop spinning until its timeout.
"""

from protocol import CFPError

IMPLEMENTED = {
    "ping": ["pong"],
    "info": ["Flipper", "Zero", "(simulated)"],
}

STUBS = ("subghz.info", "ir.info", "nfc.info")


class MockCFPClient:
    # Marcaj citit de CommandDispatcher si transmis modelului la fiecare rezultat, ca
    # sa nu poata prezenta date simulate drept masuratori reale.
    simulated = True

    def __init__(self, stop_after=None):
        self.calls = []
        # IR bruteforce state, mirroring CfpIrState in the firmware.
        self._ir_queue = []
        self._ir_sent = 0
        self._ir_running = False
        self._ir_stopped = False
        # Which code the simulated user "reacts" to by pressing OK. None means no code
        # works and the run goes to exhaustion.
        self._stop_after = stop_after

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def _ir_request(self, cmd, args):
        if cmd == "ir.reset":
            self._ir_queue = []
            self._ir_sent = 0
            self._ir_running = False
            self._ir_stopped = False
            return ["cleared"]

        if cmd == "ir.queue":
            if len(args) < 3:
                raise CFPError("missing_code")
            self._ir_queue.append(tuple(args[:3]))
            return [str(len(self._ir_queue))]

        if cmd == "ir.bruteforce":
            if not self._ir_queue:
                raise CFPError("empty_queue")
            self._ir_running = True
            self._ir_stopped = False
            self._ir_sent = 0
            return ["started", str(len(self._ir_queue))]

        if cmd == "ir.status":
            # Each poll advances the run by one code, the way the device does between
            # two transmissions.
            if self._ir_running:
                if self._stop_after is not None and self._ir_sent >= self._stop_after:
                    self._ir_running = False
                    self._ir_stopped = True
                elif self._ir_sent < len(self._ir_queue):
                    self._ir_sent += 1
                else:
                    self._ir_running = False

            if self._ir_running:
                state = "running"
            elif self._ir_stopped:
                state = "stopped"
            else:
                state = "idle"
            return [state, str(self._ir_sent), str(len(self._ir_queue))]

        return None

    def request(self, cmd, *args):
        self.calls.append((cmd, args))
        if cmd in IMPLEMENTED:
            return IMPLEMENTED[cmd]
        if cmd.startswith("ir.") and cmd in ("ir.reset", "ir.queue", "ir.bruteforce", "ir.status"):
            return self._ir_request(cmd, args)
        if cmd in STUBS:
            raise CFPError("not_implemented")
        raise CFPError("unknown_command")
