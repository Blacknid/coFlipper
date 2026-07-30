"""Lantul de raționament: urma pas cu pas a unui tur de conversatie.

Intre cererea utilizatorului si raspunsul final se afla o succesiune de decizii: ce
unealta merita apelata, ce a raspuns dispozitivul, ce concluzie se poate trage din
acel raspuns, daca mai e nevoie de o masuratoare. Modulul pastreaza succesiunea in
ordinea in care s-a produs, ca sa poata fi arata utilizatorului si verificata pas cu pas.

Rostul nu este depanarea, ci verificabilitatea. Un agent care formuleaza fraze in
limbaj natural sună la fel de convingator si cand a masurat ceva, si cand doar
presupune; lantul arata pe ce se sprijina, concret, fiecare afirmatie din raspuns.

Modulul nu contine niciun cod de randare: consola si fereastra grafica afiseaza acelasi
lant in feluri diferite, iar formatarea comuna a unui pas se afla in metodele lui Step.
"""

import time
from dataclasses import dataclass, field

# Felurile de pasi din care e alcatuit un lant.
REQUEST = "request"  # cererea utilizatorului, pasul care deschide lantul
THOUGHT = "thought"  # rezumatul raționamentului, primit de la model
TOOL = "tool"  # o comanda executata efectiv pe dispozitiv, cu rezultatul ei
ANSWER = "answer"  # raspunsul final, formulat pe baza pasilor anteriori


@dataclass
class Step:
    kind: str
    text: str = ""
    name: str = ""
    args: dict = field(default_factory=dict)
    outcome: dict = field(default_factory=dict)
    # Runda de dialog cu modelul in care s-a produs pasul. Un tur poate avea mai multe:
    # modelul cere o masuratoare, primeste rezultatul, apoi decide daca mai are nevoie de alta.
    round: int = 0
    # Secunde scurse de la inceputul turului, ca sa se vada unde s-a consumat timpul.
    at_s: float = 0.0

    @property
    def ok(self):
        return self.outcome.get("status") == "ok"

    @property
    def simulated(self):
        return bool(self.outcome.get("simulated"))

    def arg_line(self):
        return " ".join(f"{key}={value}" for key, value in self.args.items())

    def result_line(self):
        """Rezultatul comenzii intr-o singura linie, in aceeasi forma pentru orice afisaj."""
        if self.kind != TOOL:
            return ""
        if self.ok:
            # Comenzile de dispozitiv intorc 'data'; cele de agent (ex. bruteforce-ul IR)
            # intorc un rezumat propriu, deci afisam ce exista.
            if "data" in self.outcome:
                summary = " ".join(self.outcome.get("data") or [])
            else:
                summary = self.outcome.get("message") or self.outcome.get("outcome") or "gata"
            return f"OK {summary}".strip()
        return f"ERR {self.outcome.get('error', 'eroare necunoscuta')}"


class Trace:
    """Lantul unui singur tur: cerere, raționamente, comenzi executate, raspuns."""

    def __init__(self, request):
        self._start = time.monotonic()
        self.round = 0
        self.steps = []
        self._add(Step(kind=REQUEST, text=request))

    def _add(self, step):
        step.round = self.round
        step.at_s = time.monotonic() - self._start
        self.steps.append(step)
        return step

    def next_round(self):
        self.round += 1

    def add_thought(self, text):
        return self._add(Step(kind=THOUGHT, text=plain_text(text)))

    def add_tool(self, name, args, outcome):
        return self._add(Step(kind=TOOL, name=name, args=dict(args or {}), outcome=outcome))

    def add_answer(self, text):
        return self._add(Step(kind=ANSWER, text=text))

    @property
    def first(self):
        return self.steps[0]

    @property
    def evidence(self):
        """Pasii care au produs date reale: dovezile pe care se sprijina raspunsul final."""
        return [step for step in self.steps if step.kind == TOOL and step.ok]


def plain_text(text):
    """Text simplu din text cu marcaje Markdown.

    Modelul primeste instructiunea sa raspunda fara marcaje, iar rezumatele de
    raționament nici nu trec prin acea instructiune. Ferestrele nu interpreteaza
    Markdown, deci un asterisc ramas se vede exact ca un asterisc.
    """
    cleaned = text.replace("**", "").replace("__", "").replace("`", "")
    lines = []
    for line in cleaned.splitlines():
        body = line.lstrip()
        indent = line[: len(line) - len(body)]
        if body.startswith(("* ", "- ")):
            body = "• " + body[2:].lstrip()
        elif body.startswith("#"):
            body = body.lstrip("#").lstrip()
        lines.append(indent + body)
    return "\n".join(lines).strip()
