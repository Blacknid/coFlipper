"""Flipper simulat, cu aceeasi interfata ca CFPClient.

Serveste la dezvoltarea si testarea agentului fara dispozitivul fizic conectat.
Raspunde exact ca firmware-ul real: doar 'ping' si 'info' reusesc, restul
comenzilor din firmware raspund ERR not_implemented.
"""

from protocol import CFPError

IMPLEMENTED = {
    "ping": ["pong"],
    "info": ["Flipper", "Zero", "(simulat)"],
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
