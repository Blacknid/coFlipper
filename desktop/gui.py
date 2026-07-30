"""Interfata grafica a agentului coFlipper.

Aceeasi functionalitate ca agent.py, dar intr-o fereastra: conversatia in stanga,
lantul de raționament in dreapta - gandurile modelului si comenzile trimise efectiv
catre Flipper Zero, in ordinea in care s-au produs. Astfel raspunsul final nu apare ca
un verdict, ci ca incheierea unui drum pe care utilizatorul il poate urmari.

Aspectul urmeaza limbajul vizual al aplicatiei oficiale qFlipper: fundal foarte
intunecat, portocaliul Flipper ca singura culoare de accent, panouri plate cu contur
subtil si un card de dispozitiv in partea de sus.

Rulare:
    python gui.py           # cu Flipper conectat prin USB
    python gui.py --mock    # fara dispozitiv, pentru dezvoltare
"""

import argparse
import ctypes
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from dotenv import load_dotenv

from agent import MODEL, build_chat, run_turn
from commands import CommandDispatcher, device_commands, load_catalog
from reasoning import ANSWER, REQUEST, THOUGHT, TOOL, plain_text

# Paleta qFlipper: fundal aproape negru, un singur accent portocaliu (#FF8200 este
# portocaliul din identitatea Flipper Zero), restul in tonuri de gri.
BG = "#141518"
BG_CARD = "#1D1E22"
BG_PANEL = "#1A1B1F"
BORDER = "#2C2E34"
FG = "#E8E9ED"
FG_DIM = "#8A8C94"
FG_FAINT = "#5C5E66"
ORANGE = "#FF8200"
ORANGE_DARK = "#D96E00"
OK_GREEN = "#5FBF7F"
ERR_RED = "#E06C6C"
WARN_YELLOW = "#E0B84C"

# Cat de des se verifica prezenta dispozitivului. Verificarea inseamna doar citirea
# listei de porturi seriale, deci un interval scurt nu incarca sistemul.
DEVICE_POLL_S = 1.5

# Numarul pasului ocupa primul rand; restul textului se aliniaza sub el.
STEP_INDENT = "   "


def _indent(text):
    """Aliniaza un rezumat de raționament, care poate avea mai multe paragrafe."""
    return "\n".join(STEP_INDENT + line if line else "" for line in text.splitlines())


class CoFlipperWindow:
    def __init__(self, root, mock=False):
        self.root = root
        self.mock = mock
        self.events = queue.Queue()
        self.dispatcher = None
        self.chat = None
        self.genai_client = None
        self.flipper = None
        self.busy = False
        self.closing = False
        # Starea afisata cand agentul nu lucreaza, actualizata de firul de supraveghere.
        self.ready_status = "se conecteaza..."
        self.ready_color = FG_DIM
        # Numerotarea pasilor reporneste la fiecare cerere: lantul se citeste pe turul curent.
        self.step_no = 0
        self.chain_used = False

        root.title("coFlipper")
        root.minsize(760, 460)
        root.configure(bg=BG)

        self._build_fonts()
        self._build_styles()
        self._build_layout()
        # Geometria se fixeaza dupa construirea widgeturilor: altfel fereastra se
        # redimensioneaza singura ca sa incapa continutul si depaseste marginea ecranului.
        self._center_window(1020, 660)

        root.after(100, self._drain_events)
        # Conectarea deschide portul serial si contacteaza API-ul, deci se face pe un
        # fir separat: altfel fereastra ar aparea blocata pana la finalizarea ei.
        threading.Thread(target=self._connect, daemon=True).start()

    # ---------------------------------------------------------------- interfata

    def _center_window(self, width, height):
        """Asaza fereastra in centru, fara sa depaseasca ecranul.

        Dimensiunea dorita se reduce daca ecranul e mai mic: altfel, pe ecrane cu
        scalare mare, bara de scriere ar ajunge sub marginea de jos si ar fi inutilizabila.
        """
        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(width, screen_w - 80)
        height = min(height, screen_h - 140)
        x = (screen_w - width) // 2
        y = (screen_h - height) // 2
        self.root.geometry(f"{width}x{height}+{x}+{max(y, 20)}")

    def _build_fonts(self):
        self.font_ui = tkfont.Font(family="Segoe UI", size=10)
        self.font_small = tkfont.Font(family="Segoe UI", size=9)
        self.font_body = tkfont.Font(family="Segoe UI", size=11)
        self.font_bold = tkfont.Font(family="Segoe UI", size=11, weight="bold")
        self.font_device = tkfont.Font(family="Segoe UI Semibold", size=15)
        self.font_brand = tkfont.Font(family="Segoe UI", size=13, weight="bold")
        # Cascadia Code vine cu Windows Terminal si arata mai bine decat Consolas
        # in panoul de comenzi; daca lipseste, Tk cade automat pe un font monospatiat.
        self.font_mono = tkfont.Font(family="Cascadia Code", size=9)
        self.font_label = tkfont.Font(family="Segoe UI", size=8, weight="bold")
        # Gandurile modelului sunt proza, nu comenzi: font proportional si inclinat, ca
        # sa se distinga la prima vedere de liniile monospatiate ale dispozitivului.
        self.font_thought = tkfont.Font(family="Segoe UI", size=9, slant="italic")
        self.font_step = tkfont.Font(family="Segoe UI", size=8, weight="bold")

    def _build_styles(self):
        """Bara de derulare implicita e cea a sistemului si strica tema intunecata."""
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "coFlipper.Vertical.TScrollbar",
            background=BORDER,
            troughcolor=BG_PANEL,
            bordercolor=BG_PANEL,
            arrowcolor=FG_DIM,
            darkcolor=BORDER,
            lightcolor=BORDER,
            relief="flat",
            width=10,
        )
        style.map(
            "coFlipper.Vertical.TScrollbar",
            background=[("active", FG_FAINT), ("pressed", ORANGE)],
        )

    def _build_layout(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        self._build_top_bar()
        self._build_device_card()
        self._build_panels()
        self._build_input_bar()

    def _build_top_bar(self):
        bar = tk.Frame(self.root, bg=BG, padx=20, pady=14)
        bar.grid(row=0, column=0, sticky="ew")

        tk.Label(bar, text="co", bg=BG, fg=FG, font=self.font_brand).pack(side="left")
        tk.Label(bar, text="Flipper", bg=BG, fg=ORANGE, font=self.font_brand).pack(side="left")

        tk.Label(bar, text=MODEL, bg=BG, fg=FG_FAINT, font=self.font_small).pack(side="right")

    def _build_device_card(self):
        card = tk.Frame(
            self.root,
            bg=BG_CARD,
            highlightthickness=1,
            highlightbackground=BORDER,
            padx=18,
            pady=14,
        )
        card.grid(row=1, column=0, sticky="ew", padx=20)
        card.columnconfigure(1, weight=1)

        left = tk.Frame(card, bg=BG_CARD)
        left.grid(row=0, column=0, sticky="w")

        tk.Label(
            left, text="Flipper Zero", bg=BG_CARD, fg=FG, font=self.font_device
        ).pack(anchor="w")

        state_row = tk.Frame(left, bg=BG_CARD)
        state_row.pack(anchor="w", pady=(4, 0))
        self.status_dot = tk.Label(state_row, text="●", bg=BG_CARD, fg=FG_FAINT, font=self.font_ui)
        self.status_dot.pack(side="left", padx=(0, 6))
        self.status_label = tk.Label(
            state_row, text="se conecteaza...", bg=BG_CARD, fg=FG_DIM, font=self.font_ui
        )
        self.status_label.pack(side="left")

        right = tk.Frame(card, bg=BG_CARD)
        right.grid(row=0, column=1, sticky="e")
        tk.Label(right, text="UNELTE DISPONIBILE", bg=BG_CARD, fg=FG_FAINT, font=self.font_label).pack(
            anchor="e"
        )
        self.tools_label = tk.Label(
            right, text="—", bg=BG_CARD, fg=FG_DIM, font=self.font_small, justify="right"
        )
        self.tools_label.pack(anchor="e", pady=(3, 0))

    def _build_panels(self):
        panels = tk.Frame(self.root, bg=BG)
        panels.grid(row=2, column=0, sticky="nsew", padx=20, pady=(16, 0))
        panels.rowconfigure(1, weight=1)
        panels.columnconfigure(0, weight=3)
        panels.columnconfigure(1, weight=2)

        self._panel_label(panels, "CONVERSAȚIE", column=0)
        self._panel_label(panels, "LANȚ DE RAȚIONAMENT", column=1, padx=(14, 0))

        self.transcript = self._make_text(panels, self.font_body, "word", column=0)
        self.transcript.tag_configure("speaker", foreground=ORANGE, font=self.font_bold)
        self.transcript.tag_configure("agent", foreground=FG, spacing1=2, spacing3=8)
        self.transcript.tag_configure("system", foreground=FG_DIM, font=self.font_ui)
        self.transcript.tag_configure("error", foreground=ERR_RED, font=self.font_ui)
        self.transcript.tag_configure("warning", foreground=WARN_YELLOW, font=self.font_bold)

        # Incadrarea cuvintelor e necesara aici: pe langa comenzi, panoul afiseaza si
        # gandurile modelului, care sunt fraze intregi.
        self.chain = self._make_text(panels, self.font_mono, "word", column=1, padx=(14, 0))
        self.chain.tag_configure("head", foreground=ORANGE, font=self.font_step, spacing3=6)
        self.chain.tag_configure("num", foreground=FG_FAINT, font=self.font_step)
        self.chain.tag_configure("label", foreground=FG_DIM, font=self.font_step)
        self.chain.tag_configure("thought", foreground=FG_DIM, font=self.font_thought, spacing1=1)
        self.chain.tag_configure("call", foreground=FG)
        self.chain.tag_configure("ok", foreground=OK_GREEN)
        self.chain.tag_configure("err", foreground=ERR_RED)
        self.chain.tag_configure("dim", foreground=FG_FAINT)
        self.chain.tag_configure("warn", foreground=WARN_YELLOW)

    def _panel_label(self, parent, text, column, padx=0):
        tk.Label(
            parent, text=text, bg=BG, fg=FG_FAINT, font=self.font_label, anchor="w"
        ).grid(row=0, column=column, sticky="ew", padx=padx, pady=(0, 7))

    def _make_text(self, parent, font, wrap, column, padx=0):
        """Zona de text cu bara de derulare, asezata in grila panoului. Returneaza textul."""
        frame = tk.Frame(
            parent, bg=BG_PANEL, highlightthickness=1, highlightbackground=BORDER
        )
        frame.grid(row=1, column=column, sticky="nsew", padx=padx)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        text = tk.Text(
            frame,
            bg=BG_PANEL,
            fg=FG,
            font=font,
            wrap=wrap,
            relief="flat",
            padx=16,
            pady=14,
            insertbackground=ORANGE,
            selectbackground=ORANGE_DARK,
            state="disabled",
            highlightthickness=0,
            width=1,
            height=1,
            spacing3=2,
        )
        text.grid(row=0, column=0, sticky="nsew")

        scroll = ttk.Scrollbar(
            frame, orient="vertical", command=text.yview, style="coFlipper.Vertical.TScrollbar"
        )
        scroll.grid(row=0, column=1, sticky="ns", padx=(0, 3), pady=3)
        text.configure(yscrollcommand=scroll.set)
        return text

    def _build_input_bar(self):
        bar = tk.Frame(self.root, bg=BG, padx=20, pady=18)
        bar.grid(row=3, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)

        wrapper = tk.Frame(bar, bg=BG_PANEL, highlightthickness=1, highlightbackground=BORDER)
        wrapper.grid(row=0, column=0, sticky="ew", padx=(0, 12))

        self.entry = tk.Entry(
            wrapper,
            bg=BG_PANEL,
            fg=FG,
            font=self.font_body,
            relief="flat",
            insertbackground=ORANGE,
            selectbackground=ORANGE_DARK,
            highlightthickness=0,
            disabledbackground=BG_PANEL,
            disabledforeground=FG_FAINT,
        )
        self.entry.pack(fill="x", padx=14, pady=11)
        self.entry.bind("<Return>", lambda _event: self._on_send())

        self.send_button = tk.Button(
            bar,
            text="TRIMITE",
            command=self._on_send,
            bg=ORANGE,
            fg="#141518",
            font=self.font_bold,
            relief="flat",
            padx=26,
            pady=10,
            activebackground=ORANGE_DARK,
            activeforeground="#141518",
            cursor="hand2",
            disabledforeground="#6A6A72",
            borderwidth=0,
            highlightthickness=0,
        )
        self.send_button.grid(row=0, column=1)
        self.send_button.bind("<Enter>", lambda _e: self._hover_send(True))
        self.send_button.bind("<Leave>", lambda _e: self._hover_send(False))

        self._set_input_enabled(False)

    def _hover_send(self, hovering):
        if str(self.send_button["state"]) == "disabled":
            return
        self.send_button.configure(bg=ORANGE_DARK if hovering else ORANGE)

    # ------------------------------------------------------------- randare text

    def _append(self, widget, text, tag=None):
        widget.configure(state="normal")
        widget.insert("end", text, tag)
        widget.configure(state="disabled")
        widget.see("end")

    def _set_status(self, text, color=FG_DIM):
        self.status_label.configure(text=text, fg=color)
        self.status_dot.configure(fg=color)

    def _set_input_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        self.entry.configure(state=state)
        self.send_button.configure(state=state, bg=ORANGE if enabled else BG_CARD)
        if enabled:
            self.entry.focus_set()

    # ------------------------------------------------------------------ evenimente

    def _drain_events(self):
        """Singurul loc din care se modifica interfata.

        Tkinter nu poate fi apelat din alt fir de executie, deci firele de lucru doar
        pun evenimente in coada, iar bucla principala le consuma periodic.
        """
        try:
            while True:
                kind, payload = self.events.get_nowait()
                handler = getattr(self, f"_on_event_{kind}")
                handler(payload)
        except queue.Empty:
            pass
        self.root.after(80, self._drain_events)

    def _emit(self, kind, payload):
        self.events.put((kind, payload))

    def _on_event_ready(self, payload):
        simulated = payload["simulated"]
        # Galben, nu verde: modul simulat nu trebuie sa arate ca o conexiune reala.
        # In modul real starea ramane neutra pana cand firul de supraveghere o stabileste.
        self.ready_status = payload["status"]
        self.ready_color = WARN_YELLOW if simulated else FG_DIM
        self._set_status(self.ready_status, self.ready_color)
        self.tools_label.configure(text="  ".join(payload["tools"]))

        if simulated:
            self._append(
                self.transcript,
                "MOD SIMULAT - niciun Flipper Zero fizic nu este conectat.\n"
                "Rezultatele comenzilor sunt fictive, produse de un simulator.\n\n",
                "warning",
            )
        self._set_input_enabled(True)

    def _on_event_fatal(self, payload):
        self._set_status(payload["status"], ERR_RED)
        self._append(self.transcript, payload["message"] + "\n\n", "error")

    def _on_event_user(self, payload):
        self._append(self.transcript, "Tu\n", "speaker")
        self._append(self.transcript, payload + "\n\n", "agent")

    def _on_event_agent(self, payload):
        self._append(self.transcript, "Agent\n", "speaker")
        # Instructiunea de sistem cere text simplu, dar modelul mai strecoara marcaje
        # Markdown; aici sunt cele care ar aparea ca semne fara rost in fereastra.
        self._append(self.transcript, plain_text(payload) + "\n\n", "agent")
        self._set_status(self.ready_status, self.ready_color)
        self._set_input_enabled(True)
        self.busy = False

    def _on_event_error(self, payload):
        self._append(self.transcript, payload + "\n\n", "error")
        self._set_status(self.ready_status, self.ready_color)
        self._set_input_enabled(True)
        self.busy = False

    def _on_event_thinking(self, payload):
        self._set_status(payload, ORANGE)

    def _on_event_device(self, state):
        from device import CONNECTED, DISCONNECTED

        if state == CONNECTED:
            self.ready_status, self.ready_color = f"conectat pe {self.flipper.port}", OK_GREEN
            self._append(self.transcript, "Flipper Zero conectat.\n\n", "system")
        elif state == DISCONNECTED:
            self.ready_status, self.ready_color = "neconectat", ERR_RED
            self._append(self.transcript, "Flipper Zero deconectat.\n\n", "system")
        else:
            self.ready_status, self.ready_color = "aplicatia CFP nu ruleaza", WARN_YELLOW
            self._append(
                self.transcript,
                "Flipper Zero este conectat, dar aplicatia coFlipper CFP nu ruleaza pe el.\n"
                "Porneste-o din meniul dispozitivului (Apps > Tools > coFlipper CFP Server).\n\n",
                "warning",
            )

        # Starea nu se suprascrie cat timp un tur e in desfasurare: acolo bara arata
        # ca agentul lucreaza, iar starea corecta se aplica la finalul turului.
        if not self.busy:
            self._set_status(self.ready_status, self.ready_color)

    def _on_event_step(self, step):
        """Un pas din lantul de raționament, adaugat in panoul din dreapta.

        Bara de stare urmareste acelasi lant: cat timp agentul lucreaza, ea arata la ce
        anume lucreaza in acel moment, nu doar faptul ca e ocupat.
        """
        if step.kind == REQUEST:
            self.step_no = 0
            if self.chain_used:
                self._append(self.chain, "\n")
            self.chain_used = True
            self._append(self.chain, f"CERERE: {step.text}\n\n", "head")
            return

        self.step_no += 1
        self._append(self.chain, f"{self.step_no}. ", "num")

        if step.kind == THOUGHT:
            self._append(self.chain, "raționament\n", "label")
            self._append(self.chain, _indent(step.text) + "\n\n", "thought")
            self._set_status("raționează...", ORANGE)
        elif step.kind == TOOL:
            self._append(self.chain, f"{step.name}\n", "call")
            self._append(self.chain, f"{STEP_INDENT}{step.arg_line() or '(fara argumente)'}\n", "dim")
            self._append(self.chain, f"{STEP_INDENT}{step.result_line()}\n", "ok" if step.ok else "err")
            if step.simulated:
                self._append(self.chain, f"{STEP_INDENT}(rezultat simulat)\n", "warn")
            self._append(self.chain, "\n")
            self._set_status(f"execută {step.name}...", ORANGE)
        elif step.kind == ANSWER:
            self._append(self.chain, f"răspuns formulat ({step.at_s:.1f} s)\n", "label")

    def _on_event_ir_progress(self, payload):
        sent, total = payload
        self._set_status(f"bruteforce IR: {sent}/{total} coduri trimise", WARN_YELLOW)

    # ------------------------------------------------------------- fire de lucru

    def _on_ir_progress(self, sent, total):
        """Apelat din firul de lucru, in timpul unui bruteforce IR.

        Nu atinge direct widget-urile: Tkinter nu suporta apeluri din alt fir, deci
        progresul trece prin aceeasi coada de evenimente ca restul mesajelor.
        """
        self._emit("ir_progress", (sent, total))

    def _connect(self):
        load_dotenv()
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            self._emit(
                "fatal",
                {
                    "status": "cheie API lipsa",
                    "message": "GEMINI_API_KEY nu este setat. Completeaza-l in desktop/.env.",
                },
            )
            return

        commands = device_commands(load_catalog())
        if not commands:
            self._emit(
                "fatal",
                {"status": "catalog gol", "message": "Nicio comanda disponibila in commands.json."},
            )
            return

        try:
            if self.mock:
                from mock_flipper import MockCFPClient

                self.flipper = MockCFPClient()
                status = "dispozitiv simulat"
            else:
                from device import LiveDevice

                self.flipper = LiveDevice()
                status = "se caută dispozitivul..."

            # Bruteforce-ul IR dureaza secunde bune; fara acest apel fereastra ar parea
            # blocata cat timp Flipper-ul emite codurile.
            self.dispatcher = CommandDispatcher(
                commands, self.flipper, on_progress=self._on_ir_progress
            )
            # Clientul se pastreaza ca atribut, nu ca variabila locala: vezi build_chat.
            self.genai_client, self.chat = build_chat(
                api_key, commands, self.dispatcher.simulated
            )
        except Exception as exc:
            self._emit("fatal", {"status": "eroare la pornire", "message": str(exc)})
            return

        self._emit(
            "ready",
            {
                "status": status,
                "tools": [c["name"] for c in commands],
                "simulated": self.dispatcher.simulated,
            },
        )

        if not self.mock:
            threading.Thread(target=self._monitor_device, daemon=True).start()

    def _monitor_device(self):
        """Urmareste conectarea si deconectarea dispozitivului cat timp aplicatia ruleaza."""
        while not self.closing:
            state = self.flipper.poll()
            if state:
                self._emit("device", state)
            time.sleep(DEVICE_POLL_S)

    def _on_send(self):
        if self.busy:
            return
        message = self.entry.get().strip()
        if not message:
            return

        self.entry.delete(0, "end")
        self.busy = True
        self._set_input_enabled(False)
        self._emit("user", message)
        self._emit("thinking", "agentul lucreaza...")
        threading.Thread(target=self._worker, args=(message,), daemon=True).start()

    def _worker(self, message):
        try:
            reply, _trace = run_turn(
                self.chat,
                self.dispatcher,
                message,
                lambda step: self._emit("step", step),
            )
            self._emit("agent", reply or "(raspuns gol)")
        except SystemExit as exc:
            # send_with_retry iese cu sys.exit cand modelul nu e disponibil pe planul curent.
            self._emit("error", str(exc))
        except Exception as exc:
            self._emit("error", f"{type(exc).__name__}: {exc}")

    def close(self):
        self.closing = True  # opreste firul de supraveghere a dispozitivului
        if self.flipper:
            try:
                self.flipper.close()
            except Exception:
                pass


def enable_dpi_awareness():
    """Fara asta, Windows scaleaza fereastra ca pe o imagine si textul apare neclar."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Interfata grafica a agentului coFlipper")
    parser.add_argument(
        "--mock", action="store_true", help="foloseste un Flipper simulat, fara dispozitiv fizic"
    )
    args = parser.parse_args()

    enable_dpi_awareness()
    root = tk.Tk()
    window = CoFlipperWindow(root, mock=args.mock)

    def on_close():
        window.close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
