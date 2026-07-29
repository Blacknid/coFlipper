# Protocolul coFlipper (CFP)

CFP este protocolul text propriu prin care componenta desktop (agentul) comunică cu Flipper Zero, peste portul serial USB — același canal folosit în mod normal de CLI-ul Flipper. Este gândit să fie simplu de citit, ușor de scris de mână direct în CLI (pentru depanare) și ușor de implementat atât pe un microcontroler cu resurse limitate, cât și într-un client Python.

## Format cerere (desktop -> Flipper)

    cfp <id> <comanda> [argument ...]

- `cfp` — numele comenzii CLI înregistrate de aplicația coFlipper pe Flipper.
- `<id>` — întreg pozitiv ales de client, folosit pentru a asocia răspunsul cu cererea corespunzătoare.
- `<comanda>` — identificator de forma `modul.actiune` (ex: `ping`, `subghz.info`, `ir.info`, `nfc.info`).
- `[argument ...]` — parametri separați prin spațiu, specifici comenzii. Limitare în v1: argumentele nu pot conține spații.

## Format răspuns (Flipper -> desktop)

    CFP/1 <id> OK [date ...]
    CFP/1 <id> ERR <mesaj>

`<id>` este identificatorul din cererea corespunzătoare. `OK` este urmat de datele cerute (tot separate prin spațiu); `ERR` este urmat de un cod care descrie eroarea (`unknown_command`, `not_implemented`, `bad_frame` etc.).

Orice altă linie primită pe port (bannerul CLI, prompt-ul `>: `, log-uri) nu începe cu `CFP/1` și este ignorată de client.

## Exemplu de schimb

    > cfp 1 ping
    < CFP/1 1 OK pong

    > cfp 2 subghz.info
    < CFP/1 2 ERR not_implemented

## Comenzi definite momentan

| Comandă      | Argumente | Descriere                                        | Stadiu      |
|--------------|-----------|---------------------------------------------------|-------------|
| ping         | —         | Verifică dacă serverul CFP răspunde               | implementat |
| info         | —         | Numele/modelul dispozitivului                     | implementat |
| subghz.info  | —         | Informații despre frecvența Sub-GHz curentă       | stub (TODO) |
| ir.info      | —         | Informații despre ultimul semnal IR recepționat   | stub (TODO) |
| nfc.info     | —         | Informații despre ultimul tag NFC citit           | stub (TODO) |

Comenzile marcate „stub" răspund în prezent cu `ERR not_implemented` și urmează să fie legate de modulele hardware reale (radio, IR, NFC) ale Flipper Zero, pe măsură ce implementarea avansează.
