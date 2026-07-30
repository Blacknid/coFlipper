# desktop/ — the coFlipper agent

The component that runs on the computer: it interprets user requests with the help of the Gemini model and translates them into CFP commands sent to the Flipper Zero. The protocol is documented in [/PROTOCOL.md](../PROTOCOL.md), the command catalog in [/commands.json](../commands.json).

## Installation

    pip install -r requirements.txt

Then copy `.env.example` to `.env` and fill in the key obtained from [Google AI Studio](https://aistudio.google.com/apikey):

    GEMINI_API_KEY=your_key

The `.env` file is excluded from git and must not be published.

### Choosing the model

The model is set explicitly in `agent.py` and can be changed, without modifying the code, through the `COFLIPPER_MODEL` environment variable. We deliberately avoided aliases of the `gemini-flash-latest` kind: these always track the most recent generation, and usage limits differ substantially from one generation to the next.

Practical findings from during development, on the free plan:

- the limit is approximately **20 requests per day for each model** among the recent generations (verified on `gemini-3.6-flash` and `gemini-3.5-flash`). A single exchange of messages can consume two or three requests, since every round of tool calls needs an additional request, so the limit is reached quickly;
- the quota is counted separately for each model, so switching to another model through `COFLIPPER_MODEL` provides a fresh quota;
- the `gemini-2.0-flash` and `gemini-2.0-flash-lite` models are not available on the free plan (they respond with `limit: 0`), and `gemini-2.5-flash` and `gemini-2.5-flash-lite` are no longer accessible to new keys (404 error). `list_models.py` shows the models accessible to the configured key.

Consequence for development: work on the interface is done in `--mock` mode wherever possible, and requests to the model are reserved for the checks that actually need them. For a public demonstration it is worth checking the remaining quota ahead of time.

Changing the model for the current session, when one model's quota has been exhausted:

    set COFLIPPER_MODEL=gemini-3.5-flash-lite
    python gui.py

## Running

The graphical application, the usual way to use the project:

    python gui.py             # with a Flipper Zero connected over USB
    python gui.py --mock      # without a physical device, for development

The same functionality is available in the console as well, useful when debugging:

    python agent.py
    python agent.py --mock

In `--mock` mode, commands do not reach a real device: they are served by a simulated Flipper that responds exactly like the firmware (`ping` and `info` succeed, the rest return `not_implemented`). This mode is useful for working on the agent side when the device is not at hand.

### Signalling simulated mode

Simulated mode raises an honesty problem that we only discovered while using the application: the simulator returns plausible responses, and the model, having no way to know they come from a simulator, would inform the user that the device is connected and working normally. The statement was false, even though no element of the code was, formally, wrong.

The solution has three components that complement one another:

- every tool result produced in simulated mode contains the `simulated` field, which the model receives directly;
- the system instruction receives an additional section that explicitly forbids stating that a device is connected, and requires signalling the provenance of the data in every response;
- the interface uses yellow instead of green for the connection status, shows a warning at startup, and marks every result with "(simulated result)".

Without the first measure, the restriction in the instruction would have remained a mere recommendation, one the model had no way to apply: nothing in the data it received indicated that it was inside a simulation.

## Lanțul de raționament

Între cererea utilizatorului și răspunsul final se află o succesiune de decizii: ce unealtă merită apelată, ce a răspuns dispozitivul, ce concluzie se poate trage din acel răspuns, dacă mai este nevoie de o altă măsurătoare. Aplicația păstrează această succesiune și o afișează pas cu pas, în ordinea în care s-a produs. Un tur poate conține mai multe runde de dialog cu modelul, iar fiecare rundă contribuie cu pași la lanț.

Un lanț tipic, exact în forma în care apare în panoul din dreapta:

    CERERE: Verifica daca dispozitivul raspunde si masoara nivelul pe 433.92 MHz

    1. raționament
       Cererea are doua parti. Verific mai intai daca dispozitivul raspunde,
       fiindca o masuratoare pe un dispozitiv mut nu ar avea sens.

    2. ping
       (fara argumente)
       OK pong

    3. raționament
       Raspunde. Trec la masuratoare, pe frecventa cerută.

    4. subghz_rssi
       frequency=433920000
       OK 433919809 -93.9

    5. raționament
       Nivelul e scazut, deci nu emite nimic puternic in apropiere.

    6. răspuns formulat (2.4 s)

Pașii de tip raționament nu sunt reconstituiți de noi din comenzile executate: ei sunt rezumatele propriului raționament, produse de model și cerute explicit prin `thinking_config`. Modelele care nu oferă astfel de rezumate rămân perfect utilizabile — lanțul conține în acel caz doar cererea, comenzile executate și răspunsul. Cererea poate fi dezactivată cu `COFLIPPER_THOUGHTS=0`.

Numărul de runde nu este fix și nici previzibil: la aceeași cerere, modelul a apelat într-o rulare ambele măsurători în aceeași rundă, iar în alta le-a împărțit în două runde succesive. Lanțul reflectă ce s-a întâmplat efectiv, nu un șablon prestabilit.

O limitare pe care nu am reușit să o eliminăm complet: limba rezumatelor de raționament. Instrucțiunea de sistem cere modelului să raționeze în limba în care a primit cererea, iar în practică primul rezumat respectă de obicei cerința, dar cele ulterioare revin frecvent la engleză. Rezumatele nu sunt scrise direct de model, ci de un mecanism intern care îi condensează raționamentul, și acesta nu poate fi controlat din instrucțiune. Am preferat să lăsăm rezumatele așa cum sosesc, în loc să le traducem: o traducere ar consuma cereri suplimentare din cota zilnică și, mai important, ar interpune încă un pas între raționamentul real al modelului și ceea ce vede utilizatorul — exact ceea ce lanțul încearcă să evite.

Marcajele Markdown au fost o problemă practică înrudită. Modelul răspunde implicit cu asteriscuri, accente grave și titluri, pe care fereastra le afișează literal, ca semne de punctuație fără rost. Soluția are două părți: instrucțiunea de sistem cere text simplu, iar afișajul curăță marcajele rămase, întrucât modelul respectă cerința doar în cea mai mare parte.

O consecință a acestei separări merită menționată: rezumatele de raționament sunt notițe interne ale modelului și nu au ce să caute în răspunsul adresat utilizatorului. Din acest motiv textul răspunsului este reconstruit din fragmentele care nu sunt marcate ca raționament, în loc să fie preluat direct din câmpul `text` al răspunsului, care le-ar include și pe ele.

Motivul pentru care lanțul este afișat permanent, și nu ascuns într-un jurnal de depanare, este același care a stat la baza restricției de a nu inventa date: un agent care formulează fraze în limbaj natural sună la fel de convingător și când a măsurat ceva, și când doar presupune. Lanțul arată concret pe ce se sprijină fiecare afirmație — iar când o comandă eșuează, se vede exact ce a eșuat și în ce moment. Răspunsul final încetează astfel să fie un verdict și devine încheierea unui drum pe care utilizatorul îl poate parcurge și verifica.

## Interfața grafică

Fereastra este împărțită în două panouri: în stânga conversația propriu-zisă, în dreapta lanțul de raționament descris mai sus. Bara de sus arată starea conexiunii (verde pentru conectat, roșu pentru eroare), portul serial folosit și modelul de limbaj activ.

Cât timp agentul lucrează, bara de stare urmărește lanțul: arată dacă modelul raționează în acel moment sau execută o anumită comandă pe dispozitiv, nu doar faptul că este ocupat.

## Bringing up the connection

Both the agent and the manual console go through `connect()` in `cfp_client.py`, which handles the two steps a CFP session needs:

1. Finding the serial port, by USB VID/PID rather than by description (on Windows the Flipper shows up as a nondescript "USB Serial Device").
2. Launching the coFlipper application on the device, if it is not already open.

The second step matters more than it looks: the `cfp` command exists only while our application is running, because it is that application which registers the command into the Flipper's CLI. With it closed, every request fails with `could not find command cfp`. To launch it, the client briefly speaks the Flipper's *native* CLI (`loader info` to see what is open, `loader open` to start our application), then hands the port over to the CFP session proper.

If the application is already open, it is detected and left alone. Use `--no-launch` to skip this step entirely and assume the application is running.

Two device-side details worth knowing: `loader open` needs the full `.fap` path, since it resolves plain names only for built-in applications; and `loader close` does not work on our application, which exits only on a Back event — the `cfp <id> exit` command is what closes it remotely.

### Manual console

    python cfp_client.py                      # interactive, auto-detected port
    python cfp_client.py --port COM12         # explicit port
    python cfp_client.py -c "ping" -c "info"  # run commands and exit
    python cfp_client.py --list-ports         # list serial ports and exit
    python cfp_client.py --no-launch          # assume the application is already open

It can also be used as a module, which is how `agent.py` obtains its client:

    from cfp_client import connect

    with connect() as flipper:
        print(flipper.request("ping"))

## Files

| File | Role |
|---|---|
| gui.py | aplicația cu interfață grafică (Tkinter) |
| agent.py | nucleul agentului: bucla de conversație și orchestrarea apelurilor de unelte |
| reasoning.py | lanțul de raționament: pașii unui tur, în ordinea în care s-au produs |
| commands.py | conversia catalogului commands.json în unelte Gemini și dispecerizarea apelurilor |
| device.py | conexiunea cu dispozitivul folosită de interfață, pe un fir de execuție separat |
| protocol.py | implementarea clientului CFP peste portul serial |
| cfp_client.py | stabilirea conexiunii (detecția portului + lansarea aplicației) și consolă pentru trimiterea manuală de comenzi CFP, fără model de limbaj |
| mock_flipper.py | Flipper simulat, cu aceeași interfață ca clientul real |
| test_gemini.py | verificare minimală a conexiunii la API-ul Gemini |
| list_models.py | listează modelele disponibile pentru cheia configurată |

## Verification status

The full loop model → tool → CFP command → response → final phrasing has been tested against the real Gemini API, both in simulated mode and on a physical Flipper Zero (Momentum firmware `mntm-012`, USB serial port).

Scenarios verified on the physical device:

- querying device state (`ping`, `info`), with two tools called within a single conversation turn;
- measuring the signal level on a frequency indicated by the user, with the value interpreted in natural language;
- comparing two frequencies, with the agent deciding on its own to perform two successive measurements and formulate a conclusion;
- requesting a physically impossible frequency (2.4 GHz), in which case the agent reported the error returned by the device and correctly explained the hardware limitation, without inventing a measurement.

Interfața grafică a fost verificată separat, cu dispozitivul simulat: conectare, activarea corectă a controalelor, trimiterea unei cereri, afișarea comenzilor executate și a răspunsului final, precum și tratarea erorilor fără blocarea ferestrei.

Construirea lanțului de raționament a fost verificată cu modelul înlocuit printr-un set fix de răspunsuri, ceea ce permite verificarea repetată fără a consuma cota zilnică de cereri: ordinea pașilor pe mai multe runde, faptul că fiecare pas ajunge imediat la afișaj, separarea rezumatelor de raționament de textul răspunsului, marcarea rezultatelor simulate, un tur în care comanda eșuează și cazul unui model care nu produce rezumate de raționament.

Faptul că modelul real întoarce efectiv rezumate de raționament atunci când folosește și unelte a fost confirmat separat, cu API-ul Gemini: la cererea de a compara nivelul de semnal de pe două frecvențe, agentul a raționat, a efectuat cele două măsurători și a formulat concluzia, iar lanțul a cuprins toți pașii în ordine. Această verificare nu poate fi înlocuită de cea cu răspunsuri fixe, fiindcă exact aici se afla incertitudinea: dacă modelul acceptă cererea de rezumate simultan cu apelarea uneltelor.

Simulatorul reproduce și `subghz.rssi`, cu aceleași benzi de frecvență pe care le acceptă emițătorul-receptor CC1101 al dispozitivului. Fără această restricție, agentul ar fi fost dezvoltat împotriva unui dispozitiv mai permisiv decât cel real, iar o frecvență respinsă de hardware ar fi trecut neobservată în timpul dezvoltării.
