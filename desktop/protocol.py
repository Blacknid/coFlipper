"""Client Python pentru Protocolul coFlipper (CFP), vezi /PROTOCOL.md."""

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
    # CLI-ul Flipper trateaza \r drept Enter; \n singur nu submite comanda.
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
    # Opusul lui MockCFPClient.simulated: datele vin de la hardware real.
    simulated = False

    def __init__(self, port, baudrate=DEFAULT_BAUDRATE, timeout=DEFAULT_TIMEOUT):
        self._serial = serial.Serial(port, baudrate=baudrate, timeout=timeout)
        self._ids = itertools.count(1)
        # CLI-ul Flipper afiseaza un banner/prompt la conectare; un Enter gol
        # il aduce intr-o stare stabila, iar bufferul e golit inainte de prima cerere.
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
        while True:
            line = self._serial.readline()
            if not line:
                raise CFPError(f"timeout: niciun raspuns la '{cmd}'")
            reply = decode_frame(line)
            if reply is None or reply["id"] != request_id:
                continue
            if reply["status"] == "ERR":
                raise CFPError(" ".join(reply["data"]) or "eroare necunoscuta")
            return reply["data"]
