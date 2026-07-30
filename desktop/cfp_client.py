import sys

import serial.tools.list_ports

from protocol import CFPClient, CFPError, DEFAULT_BAUDRATE


# Flipper Zero se prezinta ca dispozitiv CDC pe baza STM32: VID 0x0483, PID 0x5740.
# Descrierea portului nu contine mereu "Flipper" (pe Windows apare drept
# "USB Serial Device"), deci identificarea dupa VID/PID e mai sigura.
FLIPPER_VID = 0x0483
FLIPPER_PID = 0x5740


def looks_like_flipper(port):
    if port.vid == FLIPPER_VID and port.pid == FLIPPER_PID:
        return True
    return "flipper" in (port.description or "").lower()


def find_flipper_ports():
    """Porturile care par a fi un Flipper Zero, cele mai probabile primele.

    Varianta neinteractiva a lui pick_port(), folosita de interfata grafica: aceasta
    nu poate pune intrebari la stdin, deci are nevoie de lista bruta de candidati.
    """
    ports = list(serial.tools.list_ports.comports())
    return [p.device for p in ports if looks_like_flipper(p)]


def pick_port():
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        sys.exit("Niciun port serial gasit. Conecteaza Flipper Zero prin USB.")

    candidates = [p for p in ports if looks_like_flipper(p)] or ports

    if len(candidates) == 1:
        print(f"Flipper detectat pe {candidates[0].device} ({candidates[0].description}).")
        return candidates[0].device

    print("Porturi disponibile:")
    for i, p in enumerate(candidates):
        print(f"  [{i}] {p.device} - {p.description}")
    choice = input("Alege portul (index): ").strip()
    return candidates[int(choice)].device


def main():
    port = pick_port()
    print(f"Conectare la {port} @ {DEFAULT_BAUDRATE} baud...")

    with CFPClient(port) as client:
        print("Conectat. Scrie o comanda (ex: ping), Ctrl+C pentru iesire.")
        try:
            while True:
                line = input("> ").strip()
                if not line:
                    continue
                cmd, *args = line.split(" ")
                try:
                    data = client.request(cmd, *args)
                    print("OK", " ".join(data))
                except CFPError as exc:
                    print("ERR", exc.message)
        except KeyboardInterrupt:
            print("\nOprit.")


if __name__ == "__main__":
    main()
