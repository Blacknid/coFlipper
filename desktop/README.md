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

## Fișiere

| Fișier | Rol |
|---|---|
| gui.py | aplicația cu interfață grafică (Tkinter) |
| agent.py | nucleul agentului: bucla de conversație și orchestrarea apelurilor de unelte |
| reasoning.py | lanțul de raționament: pașii unui tur, în ordinea în care s-au produs |
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

Construirea lanțului de raționament a fost verificată cu modelul înlocuit printr-un set fix de răspunsuri, ceea ce permite verificarea repetată fără a consuma cota zilnică de cereri: ordinea pașilor pe mai multe runde, faptul că fiecare pas ajunge imediat la afișaj, separarea rezumatelor de raționament de textul răspunsului, marcarea rezultatelor simulate, un tur în care comanda eșuează și cazul unui model care nu produce rezumate de raționament.

Faptul că modelul real întoarce efectiv rezumate de raționament atunci când folosește și unelte a fost confirmat separat, cu API-ul Gemini: la cererea de a compara nivelul de semnal de pe două frecvențe, agentul a raționat, a efectuat cele două măsurători și a formulat concluzia, iar lanțul a cuprins toți pașii în ordine. Această verificare nu poate fi înlocuită de cea cu răspunsuri fixe, fiindcă exact aici se afla incertitudinea: dacă modelul acceptă cererea de rezumate simultan cu apelarea uneltelor.

Simulatorul reproduce și `subghz.rssi`, cu aceleași benzi de frecvență pe care le acceptă emițătorul-receptor CC1101 al dispozitivului. Fără această restricție, agentul ar fi fost dezvoltat împotriva unui dispozitiv mai permisiv decât cel real, iar o frecvență respinsă de hardware ar fi trecut neobservată în timpul dezvoltării.
