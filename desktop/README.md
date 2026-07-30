# desktop/ — agentul coFlipper

Componenta care rulează pe calculator: interpretează cererile utilizatorului cu ajutorul modelului Gemini și le traduce în comenzi CFP trimise către Flipper Zero. Protocolul este documentat în [/PROTOCOL.md](../PROTOCOL.md), catalogul de comenzi în [/commands.json](../commands.json).

## Instalare

    pip install -r requirements.txt

Apoi copiază `.env.example` în `.env` și completează cheia obținută de la [Google AI Studio](https://aistudio.google.com/apikey):

    GEMINI_API_KEY=cheia_ta

Fișierul `.env` este exclus din git și nu trebuie publicat.

### Alegerea modelului

Modelul este fixat explicit în `agent.py` și poate fi schimbat, fără modificarea codului, prin variabila de mediu `COFLIPPER_MODEL`. Am evitat intenționat aliasurile de tip `gemini-flash-latest`: acestea urmăresc mereu cea mai recentă generație, iar limitele de utilizare diferă substanțial de la o generație la alta.

Constatări practice din timpul dezvoltării, pe planul gratuit:

- limita este de aproximativ **20 de cereri pe zi pentru fiecare model** dintre generațiile recente (verificat pe `gemini-3.6-flash` și `gemini-3.5-flash`). Un singur schimb de mesaje poate consuma două sau trei cereri, întrucât fiecare rundă de apeluri de unelte necesită o cerere suplimentară, deci limita se atinge repede;
- cota se numără separat pentru fiecare model, așa că trecerea la un alt model prin `COFLIPPER_MODEL` oferă o cotă nouă;
- modelele `gemini-2.0-flash` și `gemini-2.0-flash-lite` nu sunt disponibile pe planul gratuit (răspund cu `limit: 0`), iar `gemini-2.5-flash` și `gemini-2.5-flash-lite` nu mai sunt accesibile cheilor noi (eroare 404). `list_models.py` afișează modelele accesibile cheii configurate.

Consecință pentru dezvoltare: lucrul asupra interfeței se face în modul `--mock` acolo unde este posibil, iar cererile către model se rezervă pentru verificările care au nevoie de ele. Pentru o demonstrație publică merită verificată cota rămasă din timp.

Schimbarea modelului pentru sesiunea curentă, atunci când cota unuia s-a epuizat:

    set COFLIPPER_MODEL=gemini-3.5-flash-lite
    python gui.py

## Rulare

Aplicația cu interfață grafică, modul obișnuit de utilizare:

    python gui.py             # cu Flipper Zero conectat prin USB
    python gui.py --mock      # fara dispozitiv fizic, pentru dezvoltare

Aceeași funcționalitate este disponibilă și în consolă, utilă la depanare:

    python agent.py
    python agent.py --mock

În modul `--mock`, comenzile nu ajung la un dispozitiv real: sunt servite de un Flipper simulat care răspunde exact ca firmware-ul (`ping` și `info` reușesc, restul returnează `not_implemented`). Modul este util pentru a lucra pe partea de agent atunci când dispozitivul nu este la îndemână.

### Semnalarea modului simulat

Modul simulat ridică o problemă de onestitate pe care am descoperit-o abia folosind aplicația: simulatorul întoarce răspunsuri verosimile, iar modelul, neavând cum să știe că vin de la un simulator, informa utilizatorul că dispozitivul este conectat și funcționează normal. Afirmația era falsă, deși niciun element din cod nu era, formal, greșit.

Soluția are trei componente care se completează reciproc:

- fiecare rezultat de unealtă produs în modul simulat conține câmpul `simulated`, pe care modelul îl primește direct;
- instrucțiunea de sistem primește o secțiune suplimentară care interzice explicit afirmația că un dispozitiv este conectat și cere semnalarea provenienței datelor în fiecare răspuns;
- interfața folosește galben în loc de verde pentru starea conexiunii, afișează un avertisment la pornire și marchează fiecare rezultat cu „(rezultat simulat)".

Fără prima măsură, restricția din instrucțiune ar fi rămas o simplă recomandare, pe care modelul nu avea cum să o aplice: nimic din datele primite nu îi indica faptul că se află într-o simulare.

## Interfața grafică

Fereastra este împărțită în două panouri. În stânga se află conversația propriu-zisă, în dreapta lista comenzilor trimise efectiv către Flipper Zero, cu argumentele și răspunsurile lor.

Această a doua zonă nu este un simplu jurnal de depanare, ci o decizie de proiectare: un agent care formulează răspunsuri în limbaj natural riscă să pară că știe lucruri pe care nu le-a măsurat. Afișând permanent comenzile executate pe dispozitiv, utilizatorul poate verifica dacă afirmațiile agentului au în spate date reale — și, în cazul unei erori returnate de hardware, vede exact ce a eșuat.

Bara de sus arată starea conexiunii (verde pentru conectat, roșu pentru eroare), portul serial folosit și modelul de limbaj activ.

## Fișiere

| Fișier | Rol |
|---|---|
| gui.py | aplicația cu interfață grafică (Tkinter) |
| agent.py | nucleul agentului: bucla de conversație și orchestrarea apelurilor de unelte |
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

Interfața grafică a fost verificată separat, cu dispozitivul simulat: conectare, activarea corectă a controalelor, trimiterea unei cereri, afișarea comenzilor executate și a răspunsului final, precum și tratarea erorilor fără blocarea ferestrei.
