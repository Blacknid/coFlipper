# flipper/ — server CFP pentru Flipper Zero

Aplicație FAP care rulează pe Flipper Zero și înregistrează comanda CLI `cfp`, folosită de componenta desktop pentru comunicare prin Protocolul coFlipper (CFP). Protocolul este documentat în [/PROTOCOL.md](../PROTOCOL.md).

## Build și rulare

Necesită [ufbt](https://github.com/flipperdevices/flipperzero-ufbt) (Micro Flipper Build Tool):

    pip install ufbt
    cd flipper
    ufbt launch

`ufbt launch` compilează aplicația, o trimite pe Flipper conectat prin USB și o pornește. Pentru a doar compila, fără flash: `ufbt build` — fișierul `.fap` rezultat apare în `dist/`.

## Testare

Cu aplicația pornită pe Flipper (ecranul arată „coFlipper - CFP"), dispozitivul răspunde la comenzi trimise pe portul serial CLI — același port folosit și de qFlipper. Din desktop:

    python ../desktop/cfp_client.py

## Notă onestă despre stadiul acestui cod

Codul din `cfp_app.c` a fost scris fără acces la un Flipper Zero fizic și fără toolchain-ul `ufbt` instalat în acest mediu de lucru, deci nu a fost încă validat printr-o compilare reală. Structura urmează API-ul public documentat al firmware-ului (`cli_add_command`, `ViewPort`/`Gui` pentru input), dar sunt posibile mici ajustări de nume de funcții/headere față de versiunea exactă de firmware/SDK folosită. Primul pas la reluarea lucrului pe partea de Flipper ar trebui să fie exact `ufbt build`, ca să prindem din prima orice discrepanță.
