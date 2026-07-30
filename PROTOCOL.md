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

    > cfp 2 subghz.rssi 433920000
    < CFP/1 2 OK 433919830 -75.0

    > cfp 3 subghz.rssi 999999999
    < CFP/1 3 ERR invalid_frequency

## Comenzi implementate

| Comandă     | Argumente | Descriere                                                 |
|-------------|-----------|-----------------------------------------------------------|
| ping        | —         | Verifică dacă serverul CFP răspunde                        |
| info        | —         | Numele/modelul dispozitivului                              |
| subghz.rssi | frecvență | Nivelul de semnal (dBm) măsurat pe frecvența dată          |
| exit        | —         | Închide aplicația CFP de pe dispozitiv (uz intern)         |

Catalogul complet, incluzând comenzile aflate încă în stadiul de proiectare, se află în commands.json.

O observație privind răspunsul comenzii `subghz.rssi`: prima valoare returnată nu este frecvența cerută, ci frecvența pe care sintetizatorul radio a reușit efectiv să o genereze. Diferența, de ordinul a câtorva sute de herți, provine din rezoluția finită a circuitului CC1101 și este raportată explicit pentru ca utilizatorul să știe pe ce s-a măsurat în realitate.

## Coduri de eroare

| Cod                | Semnificație                                                      |
|--------------------|-------------------------------------------------------------------|
| bad_frame          | Cadrul nu conține cel puțin un identificator și o comandă          |
| unknown_command    | Comanda nu este recunoscută de dispozitiv                          |
| missing_frequency  | Comanda necesită o frecvență, dar nu a primit niciun argument      |
| invalid_frequency  | Frecvența nu se află în domeniile suportate de modulul radio       |
