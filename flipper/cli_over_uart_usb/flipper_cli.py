"""Bridge pyserial catre CLI-ul Flipper Zero (USB-C / CDC ACM sau UART).

Foloseste-l ca modul:

    from flipper_cli import FlipperCLI

    with FlipperCLI() as f:
        print(f.command("info device"))

sau direct din terminal:

    python flipper_cli.py                 # shell interactiv, port auto-detectat
    python flipper_cli.py --port COM12    # port explicit
    python flipper_cli.py -c "info device" -c "storage list /ext"
"""

import argparse
import re
import sys
import time

import serial
import serial.tools.list_ports

# Flipper Zero se prezinta ca STM32 Virtual COM Port.
FLIPPER_VID = 0x0483
FLIPPER_PID = 0x5740

# Promptul CLI-ului: ">: " la inceput de linie, fara newline dupa el.
PROMPT = b">: "

# Secvente ANSI de culoare pe care Flipper le trimite in raspunsuri.
ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]")


class FlipperError(Exception):
  """Eroare de comunicare cu Flipper."""


def find_flipper_port():
  """Returneaza numele portului serial al Flipper-ului, sau None."""
  for port in serial.tools.list_ports.comports():
    if port.vid == FLIPPER_VID and port.pid == FLIPPER_PID:
      return port.device
  # Fallback: unele build-uri raporteaza numele in descriere.
  for port in serial.tools.list_ports.comports():
    haystack = f"{port.description} {port.manufacturer or ''}".lower()
    if "flipper" in haystack:
      return port.device
  return None


def list_flipper_ports():
  """Returneaza [(device, description)] pentru toate porturile seriale."""
  return [(p.device, p.description) for p in serial.tools.list_ports.comports()]


class FlipperCLI:
  """Conexiune persistenta la CLI-ul Flipper Zero.

  Args:
    port: numele portului (ex. "COM12", "/dev/ttyACM0"). None = auto-detect.
    baudrate: irelevant pe USB CDC (e virtual), contine pentru UART real.
    timeout: secunde de asteptare implicite pentru un raspuns complet.
  """

  def __init__(self, port=None, baudrate=230400, timeout=5.0):
    self.port = port or find_flipper_port()
    if not self.port:
      raise FlipperError(
          "Nu am gasit niciun Flipper Zero conectat. Verifica cablul USB-C"
          " (sa fie de date, nu doar de incarcare) si inchide qFlipper /"
          " lab.flipper.net, care tin portul ocupat.\nPorturi disponibile: "
          + ", ".join(d for d, _ in list_flipper_ports())
      )
    self.baudrate = baudrate
    self.timeout = timeout
    self.ser = None

  # -- ciclu de viata -------------------------------------------------------

  def open(self):
    try:
      self.ser = serial.Serial(self.port, self.baudrate, timeout=0.1)
    except serial.SerialException as exc:
      raise FlipperError(
          f"Nu pot deschide {self.port}: {exc}. Portul e probabil folosit de"
          " alt program (qFlipper, un alt terminal)."
      ) from exc

    time.sleep(0.4)  # lasa CDC-ul sa se stabilizeze
    self.ser.reset_input_buffer()
    # Un \r gol face Flipper-ul sa reafiseze promptul, deci stim ca e viu
    # si consumam banner-ul de start.
    self.ser.write(b"\r")
    self._read_until_prompt(timeout=3.0)
    return self

  def close(self):
    if self.ser and self.ser.is_open:
      self.ser.close()
    self.ser = None

  def __enter__(self):
    return self.open()

  def __exit__(self, exc_type, exc, tb):
    self.close()

  # -- I/O ------------------------------------------------------------------

  def _read_until_prompt(self, timeout):
    """Citeste pana la promptul '>: ' sau pana expira timeout-ul.

    Promptul poate fi inconjurat de secvente ANSI (mai ales dupa un mesaj de
    eroare colorat), deci cautam promptul in fluxul deja curatat de ANSI, nu
    in octetii bruti.
    """
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
      chunk = self.ser.read(1024)
      if chunk:
        buf += chunk
        stripped = ANSI_RE.sub(b"", bytes(buf))
        if stripped.rstrip(b" ").endswith(PROMPT.rstrip(b" ")):
          end = stripped.rfind(PROMPT.rstrip(b" "))
          return stripped[:end]
      else:
        time.sleep(0.01)
    raise FlipperError(
        f"Timeout dupa {timeout}s fara prompt. Raspuns partial:\n"
        + repr(self._clean(bytes(buf)))
    )

  @staticmethod
  def _clean(raw):
    """Scoate secventele ANSI si normalizeaza CRLF."""
    return ANSI_RE.sub(b"", raw).decode("utf-8", errors="replace").replace(
        "\r\n", "\n"
    ).replace("\r", "\n")

  def command(self, cmd, timeout=None):
    """Trimite o comanda si returneaza iesirea ei, fara ecou si fara prompt."""
    if not self.ser or not self.ser.is_open:
      raise FlipperError("Conexiunea nu e deschisa. Apeleaza open() intai.")

    self.ser.reset_input_buffer()
    self.ser.write(cmd.encode("utf-8") + b"\r")
    self.ser.flush()

    raw = self._read_until_prompt(timeout or self.timeout)
    text = self._clean(raw)

    # Flipper face ecou la comanda trimisa; scoatem prima linie daca e ecoul.
    lines = text.split("\n")
    if lines and lines[0].strip() == cmd.strip():
      lines = lines[1:]
    return "\n".join(lines).strip()

  def raw_write(self, data):
    """Trimite octeti bruti (util pentru Ctrl-C = b'\\x03' sau input binar)."""
    self.ser.write(data)
    self.ser.flush()

  def interrupt(self):
    """Trimite Ctrl-C pentru a opri o comanda care ruleaza la nesfarsit."""
    self.raw_write(b"\x03")
    try:
      self._read_until_prompt(timeout=2.0)
    except FlipperError:
      pass

  # -- helpers uzuale -------------------------------------------------------

  def info(self):
    return self.command("info device")

  def storage_list(self, path="/ext"):
    return self.command(f"storage list {path}")

  def storage_read(self, path, timeout=15.0):
    return self.command(f"storage read {path}", timeout=timeout)

  def vibro(self, on=True):
    return self.command(f"vibro {int(bool(on))}")

  def led(self, channel="b", value=255):
    """channel: r | g | b | bl (backlight); value: 0-255."""
    return self.command(f"led {channel} {value}")


# -- shell interactiv --------------------------------------------------------


def interactive(flipper):
  print(f"Conectat la Flipper pe {flipper.port}.")
  print("Comenzi: orice comanda CLI Flipper. 'help' listeaza tot. 'exit' iese.")
  print("-" * 60)
  while True:
    try:
      cmd = input("flipper> ").strip()
    except (EOFError, KeyboardInterrupt):
      print()
      break
    if not cmd:
      continue
    if cmd.lower() in ("exit", "quit"):
      break
    try:
      out = flipper.command(cmd)
    except FlipperError as exc:
      print(f"[eroare] {exc}", file=sys.stderr)
      flipper.interrupt()
      continue
    if out:
      print(out)
    print("-" * 60)


def main(argv=None):
  parser = argparse.ArgumentParser(description="Bridge CLI Flipper Zero.")
  parser.add_argument("--port", help="port serial (implicit: auto-detect)")
  parser.add_argument("--baud", type=int, default=230400)
  parser.add_argument(
      "--timeout", type=float, default=5.0, help="timeout per comanda (s)"
  )
  parser.add_argument(
      "-c",
      "--command",
      action="append",
      default=[],
      help="ruleaza o comanda si iese; se poate repeta",
  )
  parser.add_argument(
      "--list-ports", action="store_true", help="listeaza porturile si iese"
  )
  args = parser.parse_args(argv)

  if args.list_ports:
    for device, desc in list_flipper_ports():
      print(f"{device}\t{desc}")
    return 0

  try:
    with FlipperCLI(args.port, args.baud, args.timeout) as flipper:
      if args.command:
        for cmd in args.command:
          print(f"$ {cmd}")
          print(flipper.command(cmd))
      else:
        interactive(flipper)
  except FlipperError as exc:
    print(f"[eroare] {exc}", file=sys.stderr)
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
