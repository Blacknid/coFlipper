"""Connection to the CFP server on the Flipper Zero, plus a manual console.

Bringing up a CFP session takes two steps, both handled here by `connect()`:

1. The `cfp` command only exists while the coFlipper application is open on the
   device - it is the application that registers it into the Flipper's CLI. With
   the application closed, every request fails with `could not find command cfp`.
   So we first talk the Flipper's *native* CLI (`loader open`) to launch it.
2. Once the application is running, the session switches to CFP proper
   (`protocol.py`), which is the only thing the agent uses.

Use it as a module:

    from cfp_client import connect

    with connect() as flipper:
        print(flipper.request("ping"))

or directly from the terminal, as a manual console without a language model:

    python cfp_client.py                 # auto-detected port, launches the app
    python cfp_client.py --port COM12    # explicit port
    python cfp_client.py --no-launch     # assume the app is already open
"""

import argparse
import re
import sys
import time

import serial
import serial.tools.list_ports

from protocol import CFPClient, CFPError, DEFAULT_BAUDRATE

# The Flipper Zero presents itself as an STM32-based CDC device: VID 0x0483, PID 0x5740.
# The port description does not always contain "Flipper" (on Windows it shows up as
# "USB Serial Device"), so identifying by VID/PID is more reliable.
FLIPPER_VID = 0x0483
FLIPPER_PID = 0x5740

# The application, as named in flipper/application.fam, and the path ufbt installs it to.
# Launching has to go through the full .fap path: `loader open` resolves names only for
# built-in applications, and answers 'not found' for an external one like ours.
APP_NAME = "coFlipper CFP Server"
APP_PATH = "/ext/apps/Tools/cfp_app.fap"

# Native CLI prompt: ">: " at the start of a line, with no newline after it.
PROMPT = b">: "

# ANSI color sequences the Flipper sends in its responses.
ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]")

# How long the application needs, after `loader open`, before it has registered the
# `cfp` command and starts answering. Measured at well under a second on Momentum;
# the margin is there because a cold start after a reboot is slower.
APP_START_DELAY_S = 1.5


class FlipperError(Exception):
    """Error communicating with the Flipper over its native CLI."""


def looks_like_flipper(port):
    if port.vid == FLIPPER_VID and port.pid == FLIPPER_PID:
        return True
    return "flipper" in (port.description or "").lower()


def list_ports():
    """Returns [(device, description)] for all serial ports."""
    return [(p.device, p.description) for p in serial.tools.list_ports.comports()]


def pick_port():
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        sys.exit("No serial port found. Connect the Flipper Zero over USB.")

    candidates = [p for p in ports if looks_like_flipper(p)] or ports

    if len(candidates) == 1:
        print(f"Flipper detected on {candidates[0].device} ({candidates[0].description}).")
        return candidates[0].device

    print("Available ports:")
    for i, p in enumerate(candidates):
        print(f"  [{i}] {p.device} - {p.description}")
    choice = input("Choose the port (index): ").strip()
    return candidates[int(choice)].device


class FlipperCLI:
    """A connection to the Flipper Zero's own CLI (the shell qFlipper also talks to).

    Used here only to bring the CFP application up; once it is running, everything
    goes through CFPClient instead. Kept minimal on purpose - it needs to read
    prompt-delimited text output, where CFP reads one framed line per request.
    """

    def __init__(self, port=None, baudrate=DEFAULT_BAUDRATE, timeout=5.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial = None

    def open(self):
        try:
            self.serial = serial.Serial(self.port, self.baudrate, timeout=0.1)
        except serial.SerialException as exc:
            raise FlipperError(
                f"Cannot open {self.port}: {exc}. The port is probably in use by"
                " another program (qFlipper, another terminal)."
            ) from exc

        time.sleep(0.4)  # let the CDC settle
        self.serial.reset_input_buffer()
        # An empty \r makes the Flipper print the prompt again, so we know it is
        # alive and we consume the startup banner.
        self.serial.write(b"\r")
        self._read_until_prompt(timeout=3.0)
        return self

    def close(self):
        if self.serial and self.serial.is_open:
            self.serial.close()
        self.serial = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc_info):
        self.close()

    def _read_until_prompt(self, timeout):
        """Reads until the '>: ' prompt, or until the timeout expires.

        The prompt can be surrounded by ANSI sequences (especially after a colored
        error message), so we look for the prompt in the stream already stripped of
        ANSI, not in the raw bytes.
        """
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self.serial.read(1024)
            if chunk:
                buf += chunk
                stripped = ANSI_RE.sub(b"", bytes(buf))
                if stripped.rstrip(b" ").endswith(PROMPT.rstrip(b" ")):
                    end = stripped.rfind(PROMPT.rstrip(b" "))
                    return stripped[:end]
            else:
                time.sleep(0.01)
        raise FlipperError(
            f"Timeout after {timeout}s with no prompt. Partial response:\n"
            + repr(_clean(bytes(buf)))
        )

    def command(self, cmd, timeout=None):
        """Sends a command and returns its output, without the echo and the prompt."""
        if not self.serial or not self.serial.is_open:
            raise FlipperError("The connection is not open. Call open() first.")

        self.serial.reset_input_buffer()
        # The Flipper CLI treats \r as Enter; \n alone does not submit the command.
        self.serial.write(cmd.encode("utf-8") + b"\r")
        self.serial.flush()

        text = _clean(self._read_until_prompt(timeout or self.timeout))

        # The Flipper echoes the command sent; we drop the first line if it is the echo.
        lines = text.split("\n")
        if lines and lines[0].strip() == cmd.strip():
            lines = lines[1:]
        return "\n".join(lines).strip()

    # -- application lifecycle ------------------------------------------------

    def app_is_running(self):
        """Whether our CFP application is the one currently open on the device."""
        return APP_NAME in self.command("loader info")

    def launch_app(self):
        """Opens the CFP application, unless it is already open.

        Returns True if it had to be launched, False if it was already running.
        """
        if self.app_is_running():
            return False

        # On success `loader open` prints nothing at all; anything it does print is
        # the failure reason, so it is worth surfacing rather than swallowing.
        reply = self.command(f"loader open {APP_PATH}", timeout=10.0)
        if reply:
            raise FlipperError(f"Could not launch the application: {reply}")

        time.sleep(APP_START_DELAY_S)
        return True


def _clean(raw):
    """Strips ANSI sequences and normalizes CRLF."""
    return (
        ANSI_RE.sub(b"", raw)
        .decode("utf-8", errors="replace")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )


def connect(port=None, launch=True):
    """Opens a ready-to-use CFP session, launching the application if needed.

    Returns a CFPClient. The serial port is handed over to it, so the native CLI
    connection used for launching is closed first: the two cannot share the port.
    """
    port = port or pick_port()

    if launch:
        with FlipperCLI(port) as cli:
            if cli.launch_app():
                print(f'Application "{APP_NAME}" launched on the device.')
            else:
                print(f'Application "{APP_NAME}" was already running.')

    return CFPClient(port)


def main():
    parser = argparse.ArgumentParser(
        description="Manual console for the CFP server on the Flipper Zero."
    )
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    parser.add_argument(
        "--no-launch",
        action="store_true",
        help="do not launch the application; assume it is already open",
    )
    parser.add_argument(
        "-c",
        "--command",
        action="append",
        default=[],
        help="run one CFP command and exit; can be repeated",
    )
    parser.add_argument(
        "--list-ports", action="store_true", help="list the serial ports and exit"
    )
    args = parser.parse_args()

    if args.list_ports:
        for device, desc in list_ports():
            print(f"{device}\t{desc}")
        return 0

    try:
        client = connect(args.port, launch=not args.no_launch)
    except FlipperError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    print(f"Connected @ {DEFAULT_BAUDRATE} baud.")

    def run(line):
        cmd, *cmd_args = line.split(" ")
        try:
            print("OK", " ".join(client.request(cmd, *cmd_args)))
        except CFPError as exc:
            print("ERR", exc.message)

    with client:
        if args.command:
            for line in args.command:
                print(f"$ {line}")
                run(line)
            return 0

        print("Write a command (e.g. ping), Ctrl+C to exit.")
        try:
            while True:
                line = input("> ").strip()
                if line:
                    run(line)
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
