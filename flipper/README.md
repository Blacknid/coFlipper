# flipper/ — server CFP pentru Flipper Zero

Aplicație FAP care rulează pe Flipper Zero și înregistrează comanda CLI `cfp`, folosită de componenta desktop pentru comunicare prin Protocolul coFlipper (CFP). Protocolul este documentat în [/PROTOCOL.md](../PROTOCOL.md).

## Build și rulare

Necesită [ufbt](https://github.com/flipperdevices/flipperzero-ufbt) (Micro Flipper Build Tool):

    pip install ufbt
    cd flipper
    ufbt launch

`ufbt launch` compilează aplicația, o trimite pe Flipper conectat prin USB și o pornește. Pentru a doar compila, fără flash: `ufbt build`.

Dispozitivul folosit în dezvoltare rulează firmware Momentum, nu firmware-ul oficial. Deoarece o aplicație externă trebuie compilată cu SDK-ul corespunzător versiunii de firmware pe care va rula, `ufbt` a fost configurat să folosească indexul Momentum:

    ufbt update -c release --index-url=https://up.momentum-fw.dev/firmware/directory.json

Verificarea potrivirii se face comparând valorile `Target` și `API` raportate la finalul compilării cu cele raportate de dispozitiv la comanda `device_info` (în cazul nostru, target 7 și API 87.1).

## Testare

Cu aplicația pornită pe Flipper (ecranul arată „coFlipper - CFP"), dispozitivul răspunde la comenzi trimise pe portul serial CLI — același port folosit și de qFlipper. Din desktop:

    python ../desktop/cfp_client.py

## Stadiul verificării

Aplicația a fost compilată, instalată și testată pe un Flipper Zero fizic. Comenzile `ping` și `info` returnează date reale ale dispozitivului, iar comenzile încă neimplementate (`subghz.info`, `ir.info`, `nfc.info`) răspund, conform proiectării, cu `ERR not_implemented`. O comandă inexistentă produce `ERR unknown_command`.

## Două observații utile pentru dezvoltarea ulterioară

Ambele au fost descoperite în urma testării pe dispozitivul fizic și merită reținute, întrucât nu sunt evidente din documentație:

1. Interfața CLI a Flipper Zero interpretează caracterul `\r` drept confirmare a comenzii. O linie terminată doar cu `\n` este primită, dar nu este niciodată executată — comportament care se manifestă ca o expirare a timpului de așteptare, fără niciun mesaj de eroare.

2. O comandă înregistrată cu `CliCommandFlagDefault` este refuzată atât timp cât o aplicație este deschisă pe dispozitiv, cu mesajul `this command cannot be run while an application is open`. Întrucât în arhitectura noastră chiar aplicația care înregistrează comanda rămâne deschisă pe ecran, comanda trebuie declarată cu `CliCommandFlagParallelSafe`, altfel devine inutilizabilă.
