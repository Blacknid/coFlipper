"""Agentul coFlipper: leaga modelul Gemini de dispozitivul Flipper Zero.

Utilizatorul scrie in limbaj natural, modelul decide ce comenzi CFP sunt necesare,
agentul le executa pe dispozitiv si returneaza modelului rezultatele reale, pe baza
carora acesta formuleaza raspunsul final.

Rulare:
    python agent.py           # cu Flipper conectat prin USB
    python agent.py --mock    # fara dispozitiv, pentru dezvoltare
"""

import argparse
import os
import sys
import time

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

from commands import CommandDispatcher, build_tool, device_commands, load_catalog

# Model fixat intentionat, nu un alias de tip "latest": aliasul urmareste mereu cea mai
# recenta generatie, iar aceasta vine cu alte limite de utilizare. Concret, aliasul
# gemini-flash-latest trimitea catre gemini-3.6-flash, limitat la 20 de cereri pe zi pe
# planul gratuit - insuficient pentru dezvoltare si demonstratie.
# Poate fi schimbat fara modificarea codului, prin variabila de mediu COFLIPPER_MODEL.
MODEL = os.environ.get("COFLIPPER_MODEL", "gemini-3.5-flash")

# API-ul returneaza ocazional 503 cand este suprasolicitat. Fara reincercare,
# o astfel de eroare trecatoare ar intrerupe conversatia in curs.
SEND_RETRIES = 3
RETRY_DELAY_S = 2.0

SYSTEM_INSTRUCTION = """Ești asistentul proiectului coFlipper. Controlezi un dispozitiv
Flipper Zero conectat prin USB, folosind uneltele care ți-au fost puse la dispoziție.

Reguli pe care le respecți strict:
1. Orice informație despre starea dispozitivului sau despre semnalele din jur provine
   EXCLUSIV din rezultatul unei unelte apelate. Nu inventezi niciodată frecvențe,
   UID-uri, protocoale sau citiri hardware.
2. Dacă o unealtă răspunde cu eroare (de exemplu 'not_implemented'), spui deschis
   utilizatorului că funcția respectivă nu este încă implementată pe dispozitiv.
   Nu compensezi eroarea cu un răspuns plauzibil inventat.
3. Poți explica noțiuni tehnice generale din cunoștințele tale, dar marchezi clar
   diferența dintre explicație generală și date măsurate de dispozitiv.
4. Raspunde in limba in care ai primit promptul.
"""


def build_client_for_device():
    from cfp_client import pick_port
    from protocol import CFPClient

    return CFPClient(pick_port())


def send_with_retry(chat, message):
    for attempt in range(1, SEND_RETRIES + 1):
        try:
            return chat.send_message(message)
        except errors.ServerError as exc:
            if attempt == SEND_RETRIES:
                raise
            print(f"  [gemini] serviciu indisponibil ({exc.code}), reincerc...")
            time.sleep(RETRY_DELAY_S * attempt)
        except errors.ClientError as exc:
            if exc.code != 429:
                raise
            # Un 429 cu 'limit: 0' nu inseamna o cota consumata de noi, ci un model
            # care nu este deloc disponibil pe planul curent: reincercarea e inutila.
            if "limit: 0" in str(exc):
                sys.exit(
                    f"Modelul {MODEL} nu este disponibil pe planul acestei chei API.\n"
                    "Alege altul prin variabila de mediu COFLIPPER_MODEL "
                    "(list_models.py arata ce exista)."
                )
            if attempt == SEND_RETRIES:
                raise
            print("  [gemini] limita de cereri atinsa, astept...")
            time.sleep(RETRY_DELAY_S * attempt * 5)


def run_turn(chat, dispatcher, message):
    """Un tur de conversatie: poate include mai multe runde de apeluri de unelte."""
    response = send_with_retry(chat, message)

    while response.function_calls:
        results = []
        for call in response.function_calls:
            print(f"  [flipper] {call.name} {dict(call.args or {})}")
            outcome = dispatcher.dispatch(call.name, call.args)
            print(f"  [flipper] -> {outcome}")
            results.append(
                types.Part.from_function_response(name=call.name, response=outcome)
            )
        response = send_with_retry(chat, results)

    return response.text


def main():
    parser = argparse.ArgumentParser(description="Agentul coFlipper (Gemini + Flipper Zero)")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="foloseste un Flipper simulat, fara dispozitiv fizic",
    )
    args = parser.parse_args()

    load_dotenv()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY nu este setat. Pune-l in desktop/.env (vezi .env.example).")

    catalog = load_catalog()
    commands = device_commands(catalog)
    if not commands:
        sys.exit("Nicio comanda disponibila in commands.json.")

    if args.mock:
        from mock_flipper import MockCFPClient

        flipper = MockCFPClient()
        print("Mod simulat: niciun dispozitiv fizic nu este folosit.")
    else:
        flipper = build_client_for_device()

    dispatcher = CommandDispatcher(commands, flipper)
    genai_client = genai.Client(api_key=api_key)
    chat = genai_client.chats.create(
        model=MODEL,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            tools=[build_tool(commands)],
        ),
    )

    names = ", ".join(cmd["name"] for cmd in commands)
    print(f"Unelte disponibile modelului: {names}")
    print("Scrie o cerere in limbaj natural. Ctrl+C pentru a incheia.\n")

    try:
        while True:
            message = input("> ").strip()
            if not message:
                continue
            print(run_turn(chat, dispatcher, message))
    except KeyboardInterrupt:
        print("\nSesiunea a fost oprita.")
    finally:
        flipper.close()


if __name__ == "__main__":
    main()
