import sys

import serial.tools.list_ports

from protocol import CFPClient, CFPError, DEFAULT_BAUDRATE


def pick_port():
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        sys.exit("Niciun port serial gasit. Conecteaza Flipper Zero prin USB.")

    flipper_ports = [p for p in ports if "flipper" in (p.description or "").lower()]
    candidates = flipper_ports or ports

    if len(candidates) == 1:
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
