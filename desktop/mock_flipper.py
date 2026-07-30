"""A simulated Flipper, with the same interface as CFPClient.

Used for developing and testing the agent without the physical device connected.
Responds exactly like the real firmware: only 'ping' and 'info' succeed, the rest
of the firmware commands respond ERR not_implemented.
"""

from protocol import CFPError

IMPLEMENTED = {
    "ping": ["pong"],
    "info": ["Flipper", "Zero", "(simulated)"],
}

STUBS = ("subghz.info", "ir.info", "nfc.info")


class MockCFPClient:
    def __init__(self):
        self.calls = []

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def request(self, cmd, *args):
        self.calls.append((cmd, args))
        if cmd in IMPLEMENTED:
            return IMPLEMENTED[cmd]
        if cmd in STUBS:
            raise CFPError("not_implemented")
        raise CFPError("unknown_command")
