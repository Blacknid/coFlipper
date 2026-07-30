"""The coFlipper agent: connects the Gemini model to the Flipper Zero device.

The user writes in natural language, the model decides which CFP commands are needed,
the agent executes them on the device and returns the real results to the model, on
whose basis it formulates the final answer.

Running:
    python agent.py           # with a Flipper connected over USB
    python agent.py --mock    # without a device, for development
"""

import argparse
import os
import sys
import time

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

from commands import CommandDispatcher, build_tool, device_commands, load_catalog
from reasoning import ANSWER, THOUGHT, TOOL, Trace

# Model fixat intentionat, nu un alias de tip "latest": aliasul urmareste mereu cea mai
# recenta generatie, iar aceasta vine cu alte limite de utilizare.
# Pe planul gratuit fiecare model are aproximativ 20 de cereri pe zi, numarate separat,
# deci schimbarea modelului prin variabila de mediu COFLIPPER_MODEL ofera o cota nouă.
MODEL = os.environ.get("COFLIPPER_MODEL", "gemini-3.5-flash")

# The API occasionally returns 503 when overloaded. Without a retry, such a transient
# error would interrupt the conversation in progress.
SEND_RETRIES = 3
RETRY_DELAY_S = 2.0

# Modelele recente raționeaza inainte de a raspunde si pot returna un rezumat al acestui
# raționament. Il cerem explicit: fara el, singurul lucru vizibil intre cererea
# utilizatorului si raspunsul final ar fi lista comenzilor executate, nu si motivul
# pentru care agentul a ales tocmai acele comenzi.
# COFLIPPER_THOUGHTS=0 dezactiveaza cererea, pentru modelele care nu o accepta.
INCLUDE_THOUGHTS = os.environ.get("COFLIPPER_THOUGHTS", "1") != "0"

SYSTEM_INSTRUCTION = """Ești asistentul proiectului coFlipper. Controlezi un dispozitiv
Flipper Zero conectat prin USB, folosind uneltele care ți-au fost puse la dispoziție.

Reguli pe care le respecți strict:
1. Orice informație despre starea dispozitivului sau despre semnalele din jur provine
   EXCLUSIV din rezultatul unei unelte apelate. Nu inventezi niciodată frecvențe,
   UID-uri, protocoale sau citiri hardware.
2. Dacă o unealtă răspunde cu eroare, spui deschis utilizatorului ce a eșuat și nu
   compensezi eroarea cu un răspuns plauzibil inventat. Erorile obișnuite sunt
   'not_implemented' (funcția nu există încă în firmware), 'dispozitiv neconectat'
   (Flipper Zero nu este legat prin USB — îi ceri utilizatorului să îl conecteze) și
   'aplicatia coFlipper CFP nu ruleaza pe dispozitiv' (îi ceri să o pornească din
   meniul Flipper-ului). Dispozitivul poate fi conectat sau deconectat oricând în
   timpul conversației, deci o comandă poate eșua deși una anterioară a reușit.
3. Poți explica noțiuni tehnice generale din cunoștințele tale, dar marchezi clar
   diferența dintre explicație generală și date măsurate de dispozitiv.
4. Raspunde in limba in care ai primit promptul si raționezi in aceeasi limba: pasii
   raționamentului tau sunt aratati utilizatorului, alaturi de comenzile executate.
5. Raspunzi in text simplu, fara marcaje Markdown - fara asteriscuri, diez sau accente
   grave. Raspunsul e afisat intr-o fereastra care nu interpreteaza astfel de marcaje,
   deci ele ar aparea ca semne de punctuatie fara rost.
6. Pentru comenzi cu infrarosu ('stinge televizorul', 'da volumul mai tare', 'schimba
   canalul') folosesti unealta agent_ir_control. Ii transmiti cererea utilizatorului
   asa cum a formulat-o, plus marca aparatului daca a mentionat-o. La cereri de
   continuare ('mai tare', 'inca un canal') transmiti explicit device_type, fiindca
   aparatul nu mai e numit, dar il stii din conversatie. Nu inventezi coduri IR si nu
   apelezi tu comenzile ir.* individuale: unealta le gestioneaza singura.
7. Cand pornesti un bruteforce IR, spui utilizatorului sa apese butonul din mijloc (OK)
   pe Flipper in momentul in care aparatul reactioneaza - asa se opreste emisia. Daca
   rezultatul are 'worked': false, inseamna ca s-au epuizat codurile fara confirmare:
   spui asta deschis si propui sa incerce precizand marca, in loc sa pretinzi reusita.
8. Daca utilizatorul spune ca nu a mers ('tot nu merge', 'nu s-a intamplat nimic'),
   apelezi din nou agent_ir_control cu search_online=true, ca sa se caute in baza de
   date online de telecomenzi reale (mii de modele), nu doar in tabelul intern. Ai
   nevoie de marca aparatului: daca utilizatorul nu a spus-o, o ceri intai. Il anunti
   ca durata e mai mare, fiindca se descarca de pe internet. Dupa cinci incercari
   nereusite pe acelasi aparat, cautarea online porneste automat.
9. In raspuns spui de unde au venit codurile: 'code_source': 'builtin' inseamna tabelul
   intern, 'irdb' inseamna baza de date online. Daca rezultatul contine 'next_step', il
   folosesti ca sa-i spui utilizatorului ce urmeaza.
"""

# Adaugat la instructiunea de sistem cand se lucreaza fara dispozitiv fizic. Fara el,
# modelul primeste raspunsuri verosimile de la simulator si anunta utilizatorul ca
# Flipper-ul e conectat si functioneaza - exact confuzia pe care proiectul o evita.
SIMULATED_NOTICE = """
ATENȚIE - MOD SIMULAT: niciun Flipper Zero fizic nu este conectat. Toate uneltele sunt
servite de un simulator, iar rezultatele lor sunt fictive. Ele conțin câmpul
'simulated': true. Nu afirmi niciodată că dispozitivul este conectat sau că o valoare a
fost măsurată. În fiecare răspuns care se referă la starea dispozitivului sau la
semnale, precizezi explicit că datele provin dintr-un simulator.
"""


def build_client_for_device():
    from cfp_client import pick_port
    from protocol import CFPClient

    return CFPClient(pick_port())


def build_chat(api_key, commands, simulated=False):
    """Sesiunea de conversatie, cu uneltele derivate din catalogul de comenzi.

    Returneaza si clientul, nu doar conversatia: apelantul trebuie sa pastreze o
    referinta la el, altfel colectorul de gunoaie il distruge si inchide conexiunea
    HTTP pe care se sprijina conversatia.
    """
    instruction = SYSTEM_INSTRUCTION
    if simulated:
        instruction += SIMULATED_NOTICE

    client = genai.Client(api_key=api_key)
    chat = client.chats.create(
        model=MODEL,
        config=types.GenerateContentConfig(
            system_instruction=instruction,
            tools=[build_tool(commands)],
            thinking_config=(
                types.ThinkingConfig(include_thoughts=True) if INCLUDE_THOUGHTS else None
            ),
        ),
    )
    return client, chat


def _response_parts(response):
    for candidate in response.candidates or []:
        content = getattr(candidate, "content", None)
        for part in (getattr(content, "parts", None) or []) if content else []:
            yield part


def thought_texts(response):
    """Rezumatele de raționament din raspuns, in ordinea in care le-a produs modelul.

    Un rezumat vine ca o bucata de text obisnuita, deosebita doar prin indicatorul
    'thought'. Modelele care nu ofera rezumate returneaza pur si simplu zero bucati de
    acest fel, iar lantul rezultat contine doar comenzile executate.
    """
    return [
        part.text for part in _response_parts(response) if getattr(part, "thought", False) and part.text
    ]


def answer_text(response):
    """Textul adresat utilizatorului, fara rezumatele de raționament.

    Nu folosim direct response.text: acela poate include si bucatile de raționament,
    care sunt notite interne ale modelului si au locul lor in lant, nu in raspuns.
    """
    chunks = [
        part.text
        for part in _response_parts(response)
        if part.text and not getattr(part, "thought", False)
    ]
    return "".join(chunks).strip() or (response.text or "").strip()


def send_with_retry(chat, message):
    for attempt in range(1, SEND_RETRIES + 1):
        try:
            return chat.send_message(message)
        except errors.ServerError as exc:
            if attempt == SEND_RETRIES:
                raise
            print(f"  [gemini] service unavailable ({exc.code}), retrying...")
            time.sleep(RETRY_DELAY_S * attempt)
        except errors.ClientError as exc:
            # Nu toate modelele accepta cererea de rezumate de raționament. Mesajul brut
            # al API-ului nu spune ce trebuie schimbat, asa ca il traducem.
            if exc.code == 400 and "thinking" in str(exc).lower():
                sys.exit(
                    f"Modelul {MODEL} nu accepta rezumate de raționament.\n"
                    "Porneste din nou cu COFLIPPER_THOUGHTS=0: lantul va arata comenzile "
                    "executate, dar fara explicatiile modelului."
                )
            if exc.code != 429:
                raise
            # A 429 with 'limit: 0' does not mean a quota we consumed ourselves, but a
            # model that is not available at all on the current plan: retrying is pointless.
            if "limit: 0" in str(exc):
                sys.exit(
                    f"The model {MODEL} is not available on this API key's plan.\n"
                    "Choose another one through the COFLIPPER_MODEL environment variable "
                    "(list_models.py shows what exists)."
                )
            if attempt == SEND_RETRIES:
                raise
            print("  [gemini] request limit reached, waiting...")
            time.sleep(RETRY_DELAY_S * attempt * 5)


def run_turn(chat, dispatcher, message, on_step=None):
    """Un tur de conversatie, construind lantul de raționament pe parcurs.

    Un tur nu este un singur schimb de mesaje: modelul poate cere o masuratoare, o
    interpreta, apoi decide ca are nevoie de alta inainte de a raspunde. Fiecare runda
    contribuie cu pasi la lant - mai intai raționamentul, apoi comenzile pe care acesta
    le-a motivat - iar la final raspunsul formulat pe baza lor.

    on_step(pas) e apelat pentru fiecare pas, imediat ce se produce, ca afisajul sa
    creasca in timp real si nu doar la sfarsitul turului.

    Returneaza (raspuns, lant).
    """
    trace = Trace(message)

    def record(step):
        if on_step:
            on_step(step)
        return step

    record(trace.first)
    response = send_with_retry(chat, message)

    while True:
        trace.next_round()
        for thought in thought_texts(response):
            record(trace.add_thought(thought))

        if not response.function_calls:
            break

        results = []
        for call in response.function_calls:
            args = dict(call.args or {})
            outcome = dispatcher.dispatch(call.name, call.args)
            record(trace.add_tool(call.name, args, outcome))
            results.append(
                types.Part.from_function_response(name=call.name, response=outcome)
            )
        response = send_with_retry(chat, results)

    reply = answer_text(response)
    record(trace.add_answer(reply))
    return reply, trace


def main():
    parser = argparse.ArgumentParser(description="The coFlipper agent (Gemini + Flipper Zero)")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="use a simulated Flipper, without a physical device",
    )
    args = parser.parse_args()

    load_dotenv()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY is not set. Put it in desktop/.env (see .env.example).")

    catalog = load_catalog()
    commands = device_commands(catalog)
    if not commands:
        sys.exit("No command available in commands.json.")

    if args.mock:
        from mock_flipper import MockCFPClient

        flipper = MockCFPClient()
        print("Simulated mode: no physical device is used.")
    else:
        flipper = build_client_for_device()

    dispatcher = CommandDispatcher(commands, flipper)
    # genai_client nu e folosit direct, dar referinta trebuie pastrata cat dureaza
    # conversatia (vezi build_chat).
    genai_client, chat = build_chat(api_key, commands, dispatcher.simulated)  # noqa: F841

    names = ", ".join(cmd["name"] for cmd in commands)
    print(f"Unelte disponibile modelului: {names}")
    print("Scrie o cerere in limbaj natural. Ctrl+C pentru a incheia.\n")

    def log_step(step):
        """Lantul de raționament, afisat pe masura ce se construieste."""
        if step.kind == THOUGHT:
            print(f"  [gand] {step.text}")
        elif step.kind == TOOL:
            print(f"  [flipper] {step.name} {step.arg_line()}".rstrip())
            print(f"  [flipper] -> {step.result_line()}")
        elif step.kind == ANSWER:
            print(f"  [agent] raspuns formulat dupa {step.at_s:.1f} s")

    try:
        while True:
            message = input("> ").strip()
            if not message:
                continue
            reply, _trace = run_turn(chat, dispatcher, message, log_step)
            print(reply)
    except KeyboardInterrupt:
        print("\nSession stopped.")
    finally:
        flipper.close()


if __name__ == "__main__":
    main()
