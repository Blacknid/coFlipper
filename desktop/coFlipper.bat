@echo off
rem Lansator pentru dublu-clic. Consola ramane deschisa intentionat: daca aplicatia
rem nu poate porni deloc (de exemplu o dependenta lipsa), mesajul de eroare apare aici.
cd /d "%~dp0"
python gui.py %*
if errorlevel 1 pause
