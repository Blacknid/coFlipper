"""Python client for the coFlipper Protocol (CFP), see /PROTOCOL.md."""

import itertools
import time

import serial

PROTOCOL_VERSION = "CFP/1"
DEFAULT_BAUDRATE = 230400
DEFAULT_TIMEOUT = 2.0


class CFPError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def encode_frame(request_id, cmd, args):
    # The Flipper CLI treats \r as Enter; \n alone does not submit the command.
    tokens = ["cfp", str(request_id), cmd, *args]
    return (" ".join(tokens) + "\r\n").encode("ascii")


def decode_frame(line):
    text = line.decode("ascii", errors="ignore").strip()
    if not text.startswith(PROTOCOL_VERSION + " "):
        return None
    tokens = text.split(" ")
    if len(tokens) < 3:
        return None
    _, raw_id, status, *data = tokens
    if status not in ("OK", "ERR"):
        return None
    try:
        frame_id = int(raw_id)
    except ValueError:
        return None
    return {"id": frame_id, "status": status, "data": data}


class CFPClient:
    # The opposite of MockCFPClient.simulated: the data comes from real hardware.
    simulated = False

    def __init__(self, port, baudrate=DEFAULT_BAUDRATE, timeout=DEFAULT_TIMEOUT):
        self._serial = serial.Serial(port, baudrate=baudrate, timeout=timeout)
        self._ids = itertools.count(1)
        # The Flipper CLI prints a banner/prompt on connect; an empty Enter brings it
        # into a stable state, and the buffer is flushed before the first request.
        self._serial.write(b"\r\n")
        time.sleep(0.3)
        self._serial.reset_input_buffer()

    def close(self):
        self._serial.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def request(self, cmd, *args):
        request_id = next(self._ids)
        self._serial.write(encode_frame(request_id, cmd, args))
        # Most commands answer within the default timeout, but subghz.read deliberately blocks
        # on the radio for its whole listening window (timeout_ms, the second argument) before
        # answering - a single long read is how the watcher waits for a signal without leaking
        # memory in a tight loop (see the firmware's CFP_READ_MAX_MS note). So the client has to
        # wait out that window plus a margin, or it would give up on a read that is working
        # exactly as intended. We enforce it as a wall-clock deadline across the readline loop.
        deadline = time.monotonic() + self._deadline_for(cmd, args)
        while True:
            line = self._serial.readline()
            if not line:
                if time.monotonic() >= deadline:
                    raise CFPError(f"timeout: no response to '{cmd}'")
                continue
            reply = decode_frame(line)
            if reply is None or reply["id"] != request_id:
                continue
            if reply["status"] == "ERR":
                raise CFPError(" ".join(reply["data"]) or "unknown error")
            return reply["data"]

    def _deadline_for(self, cmd, args):
        """Seconds to wait for this command's reply. The Sub-GHz read commands block on the
        radio for their whole listening window (a timeout_ms argument, defaulting to 3 s on
        the firmware) before answering, so we wait that window plus a 2 s margin; everything
        else uses the default timeout. The window is a different positional argument per
        command: subghz.read is 'freq [timeout_ms]', subghz.read_raw is 'freq name [timeout_ms]'."""
        window_arg = {"subghz.read": 1, "subghz.read_raw": 2}.get(cmd)
        if window_arg is not None:
            window_ms = 3000
            if len(args) > window_arg:
                try:
                    window_ms = int(args[window_arg])
                except (TypeError, ValueError):
                    pass
            return window_ms / 1000.0 + 2.0
        return DEFAULT_TIMEOUT
