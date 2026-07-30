# coFlipper

Olimpiada de Inovare și Creație Digitală — InfoEducație 2026
Etapa națională, secțiunea OPEN

| | |
|---|---|
| Lider echipă | Lupu Iulian-Nicolae |
| Coechipier | Ciuca Andrei-Corneliu |
| Clasa | a X-a |
| Instituția | Liceul "Atanasie Marienescu" |

---

## Descriere generală

coFlipper este un proiect care își propune dezvoltarea unui harnașament agentic (*agentic harness*) destinat integrării cu dispozitivul Flipper Zero. Pornim de la observația că instrumentele existente pentru Flipper Zero rămân, în esență, utilitare de control manual: utilizatorul selectează o frecvență, inițiază o captură, interpretează singur rezultatul. coFlipper adaugă un nivel suplimentar de mediere între utilizator și dispozitiv — un agent capabil să înțeleagă o intenție exprimată în limbaj natural și să o traducă în operațiuni concrete asupra hardware-ului.

## Motivație

Interacțiunea curentă cu Flipper Zero presupune, în majoritatea cazurilor, un anumit nivel de cunoștințe tehnice prealabile: utilizatorul trebuie să știe ce frecvență să interogheze, cum se citește un semnal capturat sau ce înseamnă un anumit protocol NFC. Această barieră de intrare limitează accesibilitatea dispozitivului pentru cei care nu provin dintr-un background tehnic solid.

Ipoteza de la care pornim este că un strat agentic, capabil să proceseze cereri formulate liber și să le transforme în acțiuni asupra Flipper-ului, poate reduce semnificativ această barieră. În loc ca utilizatorul să învețe sintaxa și logica internă a dispozitivului, este suficient să descrie ce anume dorește să afle sau să facă, urmând ca agentul să identifice pașii necesari și să îi execute.

## Obiective

În cadrul probei OPEN, ne propunem explorarea și implementarea următoarelor direcții funcționale:

- Analiza frecvențelor radio — capacitatea agentului de a interoga o frecvență specifică și de a returna informații contextuale relevante despre aceasta (de exemplu, la ce tip de dispozitiv sau protocol pare să fie asociată, pe baza semnalului capturat).
- Interacțiune cu infraroșu — utilizarea modulului IR al Flipper Zero pentru identificarea și, eventual, replicarea semnalelor emise de telecomenzi sau alte dispozitive compatibile.
- Interacțiune cu NFC — un scenariu ilustrativ pentru această direcție ar fi acela în care utilizatorul indică agentului un dispozitiv mobil anume, iar agentul construiește, pe baza acestei cereri, o rutină care permite observarea entităților sau punctelor de acces cu care modulul NFC al telefonului respectiv intră în contact.

Aceste trei direcții nu sunt exhaustive, ci reprezintă un punct de plecare pentru validarea conceptului în intervalul de timp disponibil pentru probă. Pe măsură ce implementarea avansează, alte capabilități ale Flipper Zero (precum sub-GHz, RFID de joasă frecvență sau modulul GPIO) pot fi integrate în mod similar.

## Arhitectură propusă

Proiectul este organizat pe două componente principale, reflectate și în structura repository-ului:

- flipper/ — logica destinată să ruleze pe (sau în relație directă cu) dispozitivul Flipper Zero: comunicarea cu modulele sale radio, IR și NFC, precum și expunerea acestor capabilități către restul sistemului.
- desktop/ — componenta agentică propriu-zisă, responsabilă de interpretarea cererilor utilizatorului, decizia asupra pașilor necesari și orchestrarea comenzilor trimise către Flipper Zero. Include aplicația cu interfață grafică prin care utilizatorul interacționează efectiv cu sistemul.

Separarea celor două componente urmărește un principiu simplu: dispozitivul rămâne executantul operațiunilor de nivel jos, în timp ce agentul concentrează întreaga logică de interpretare și decizie, fiind singurul punct cu care utilizatorul interacționează direct, în limbaj natural.

Comunicarea dintre cele două componente se face prin portul serial USB al Flipper Zero, folosind un protocol text propriu, denumit CFP (coFlipper Protocol) și documentat integral în PROTOCOL.md.

Legătura dintre modelul de limbaj și dispozitiv este realizată printr-un catalog declarativ de comenzi, commands.json. Acesta reprezintă singura sursă de adevăr a sistemului: fiecare comandă descrisă acolo este convertită automat, la pornirea agentului, într-o unealtă apelabilă de model (*function calling*). Adăugarea unei funcționalități noi presupune, prin urmare, descrierea ei în catalog și implementarea ei în firmware, fără modificări în logica agentului.

Fluxul complet al unei cereri este următorul: utilizatorul formulează o intenție în limbaj natural; modelul decide care comenzi din catalog sunt necesare și le solicită; agentul le traduce în cadre CFP și le trimite pe portul serial; Flipper Zero le execută și răspunde; rezultatele reale sunt returnate modelului, care formulează pe baza lor răspunsul final.

Interacțiunea are loc într-o aplicație cu interfață grafică, organizată în două panouri: conversația și, permanent vizibilă alături de ea, lista comenzilor executate efectiv pe dispozitiv. Această a doua zonă are o funcție care depășește depanarea — permite utilizatorului să verifice că afirmațiile agentului se sprijină pe măsurători reale, nu pe formulări plauzibile.

O constrângere de proiectare pe care am considerat-o esențială este aceea că modelul nu are permisiunea de a formula afirmații despre starea hardware-ului în absența unui rezultat efectiv primit de la dispozitiv. Dacă o comandă eșuează sau nu este încă implementată, agentul comunică explicit acest lucru, în loc să producă un răspuns plauzibil, dar fabricat. Fără această restricție, un asistent conversațional aplicat unui domeniu tehnic ar putea genera date aparent credibile — frecvențe, identificatori de carduri, protocoale — care nu corespund niciunei măsurători reale.

## Filosofia de proiectare

Un aspect central al acestui proiect este delegarea deciziilor de „cum” către agent, păstrând la nivelul utilizatorului doar formularea intenției — „ce” anume își dorește. Această separare este inspirată din paradigma agenților capabili să opereze instrumente externe (*tool use*), în care limbajul natural devine interfața primară, iar traducerea în comenzi tehnice concrete este responsabilitatea stratului intermediar.

## Elemente de originalitate

Spre deosebire de aplicațiile companion existente pentru Flipper Zero, care expun funcționalitățile dispozitivului printr-o interfață grafică tradițională, coFlipper propune o interfață conversațională ca punct central de interacțiune. Utilizatorul nu navighează manual printr-un meniu de opțiuni, ci descrie rezultatul dorit, iar agentul este cel care alege și înlănțuie operațiile necesare pe dispozitiv.

Este important de precizat unde se află, concret, originalitatea acestui proiect și unde nu se află. Comenzile individuale expuse de Flipper Zero prin protocolul CFP — citirea unui semnal Sub-GHz, decodarea unui semnal infraroșu, citirea sau emularea unui tag NFC — nu sunt originale: ele reproduc funcționalități pe care dispozitivul le are deja, nativ, în aplicațiile sale din fabrică. Catalogul complet al acestor comenzi este documentat în commands.json, sub eticheta `"layer": "device"`.

Originalitatea proiectului se află într-un nivel superior acestora, marcat în același fișier sub eticheta `"layer": "agent"`: operațiile care combină una sau mai multe comenzi de nivel device cu un raționament realizat de modelul de limbaj, pentru a produce un răspuns interpretat, nu doar date brute. De exemplu, în loc să afișeze un cod de protocol Sub-GHz, agentul poate explica, în limbaj natural, ce tip de dispozitiv este probabil sursa semnalului; în loc să listeze UID-uri NFC, poate construi un rezumat al unei sesiuni de monitorizare. Acesta este stratul care nu are, după cunoștințele noastre, un echivalent direct în ecosistemul de aplicații existente pentru Flipper Zero.

## Stadiul curent și limitări

Proiectul a fost dezvoltat în cadrul probei OPEN a etapei naționale InfoEducație 2026, în intervalul de timp alocat acesteia. Ca urmare, implementarea reflectă un stadiu incipient, de prototip funcțional, concentrat pe validarea conceptului mai degrabă decât pe acoperirea exhaustivă a tuturor capabilităților Flipper Zero. Extinderea și consolidarea proiectului rămân direcții firești pentru o eventuală continuare ulterioară competiției.

## Declarație de originalitate

În conformitate cu regulamentul InfoEducație, componentele proiectului care nu aparțin în întregime autorilor (biblioteci externe, fragmente de cod preluate, resurse grafice etc.) sunt menționate explicit în fișierul separat de originalitate atașat lucrării.
