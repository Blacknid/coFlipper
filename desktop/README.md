# desktop/ — agentul coFlipper

Componenta care rulează pe calculator: interpretează cererile utilizatorului cu ajutorul modelului Gemini și le traduce în comenzi CFP trimise către Flipper Zero. Protocolul este documentat în [/PROTOCOL.md](../PROTOCOL.md), catalogul de comenzi în [/commands.json](../commands.json).

## Instalare

    pip install -r requirements.txt

Apoi copiază `.env.example` în `.env` și completează cheia obținută de la [Google AI Studio](https://aistudio.google.com/apikey):

    GEMINI_API_KEY=cheia_ta

Fișierul `.env` este exclus din git și nu trebuie publicat.

### Alegerea modelului

Modelul este fixat explicit în `agent.py` și poate fi schimbat, fără modificarea codului, prin variabila de mediu `COFLIPPER_MODEL`. Am evitat intenționat aliasurile de tip `gemini-flash-latest`: acestea urmăresc mereu cea mai recentă generație, iar limitele de utilizare diferă substanțial de la o generație la alta.

Constatare practică din timpul dezvoltării, pe planul gratuit: `gemini-flash-latest` trimitea către `gemini-3.6-flash`, limitat la 20 de cereri pe zi, insuficient pentru dezvoltare. Modelele `gemini-2.0-flash` și `gemini-2.5-flash` nu sunt deloc disponibile pentru chei API noi — primele răspund cu `limit: 0`, celelalte cu eroare 404. `list_models.py` afișează modelele accesibile cheii configurate.

## Rulare

    python agent.py           # cu Flipper Zero conectat prin USB
    python agent.py --mock    # fara dispozitiv fizic, pentru dezvoltare

În modul `--mock`, comenzile nu ajung la un dispozitiv real: sunt servite de un Flipper simulat care răspunde exact ca firmware-ul (`ping` și `info` reușesc, restul returnează `not_implemented`). Modul este util pentru a lucra pe partea de agent atunci când dispozitivul nu este la îndemână.

## Fișiere

| Fișier | Rol |
|---|---|
| agent.py | agentul propriu-zis: bucla de conversație și orchestrarea apelurilor de unelte |
| commands.py | conversia catalogului commands.json în unelte Gemini și dispecerizarea apelurilor |
| protocol.py | implementarea clientului CFP peste portul serial |
| cfp_client.py | consolă pentru trimiterea manuală de comenzi CFP, fără model de limbaj |
| mock_flipper.py | Flipper simulat, cu aceeași interfață ca clientul real |
| test_gemini.py | verificare minimală a conexiunii la API-ul Gemini |
| list_models.py | listează modelele disponibile pentru cheia configurată |

## Stadiul verificării

Bucla completă model → unealtă → comandă CFP → răspuns → formulare finală a fost testată cu API-ul Gemini real, atât în modul simulat, cât și pe un Flipper Zero fizic (firmware Momentum `mntm-012`, port serial USB).

Scenarii verificate pe dispozitivul fizic:

- interogarea stării dispozitivului (`ping`, `info`), cu apelarea a două unelte într-un singur tur de conversație;
- măsurarea nivelului de semnal pe o frecvență indicată de utilizator, cu interpretarea valorii în limbaj natural;
- compararea a două frecvențe, agentul decizând singur să efectueze două măsurători succesive și să formuleze o concluzie;
- solicitarea unei frecvențe imposibile fizic (2.4 GHz), caz în care agentul a raportat eroarea returnată de dispozitiv și a explicat corect limitarea hardware, fără a inventa o măsurătoare.
