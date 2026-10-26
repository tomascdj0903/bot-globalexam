#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
═══════════════════════════════════════════════════════════════════════
  LAUNCHER — Bot GlobalExam (interface V2)
═══════════════════════════════════════════════════════════════════════

Fenêtre d'accueil pour lancer le bot sans terminal.

L'utilisateur choisit :
  1. la langue d'apprentissage,
  2. la durée de travail (30 min, 1 h, 1 h 30, 2 h, durée perso ou
     Illimité). À la fin du temps, le bot TERMINE l'exercice en cours,
     puis ferme Chrome et l'application,
  3. la clé API Gemini (clé fournie, ou sa propre clé),
puis clique « Démarrer ».

Le launcher ouvre Chrome (port de debug + profil DÉDIÉ, pour garder la
session GlobalExam), attend qu'il soit prêt, puis démarre le bot en
pause : l'utilisateur se connecte, ouvre sa leçon et clique
« Commencer ». Le journal, le temps restant, le nombre d'exercices et le
coût IA s'affichent en direct.

Ce fichier N'EST PAS le bot : il importe `bot.py` et lui injecte la clé
et la langue choisies.

Uniquement la bibliothèque standard (tkinter) : rien de plus à embarquer
dans l'exe.
═══════════════════════════════════════════════════════════════════════
"""

import os
import sys
import json
import time
import queue
import socket
import shutil
import threading
import subprocess
from collections import deque
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox

# =====================================================================
#  CONSTANTES
# =====================================================================
DEBUG_PORT = 9222
# Page ouverte au démarrage : l'utilisateur se connecte avec SES identifiants.
START_URL = "https://www.global-exam.com/"

# Profil Chrome DÉDIÉ (persistant + accessible en écriture, par utilisateur).
PROFILE_DIR = Path.home() / ".globalexam_bot_profile"
# Mémorise les choix (clé / langue / durée) d'un lancement à l'autre.
CONFIG_FILE = Path.home() / ".globalexam_bot_config.json"

LANGUES = [
    ("en", "Anglais"),
    ("es", "Espagnol"),
    ("fr", "Français"),
    ("de", "Allemand"),
    ("pt", "Portugais"),
    ("it", "Italien"),
]

# Logo (symbole infini) intégré en PNG base64 : aucun fichier à fournir.
LOGO_HEADER_B64 = (
    "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"
    "SUVORK5CYII="
)
LOGO_ICON_B64 = (
    "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"
    "n///TIwLbXKAOd0AAAAASUVORK5CYII="
)


def _logo_image(b64: str, w: int, h: int):
    """PhotoImage du logo à la taille voulue (PIL si dispo, sinon Tk)."""
    import base64 as _b64
    import io as _io
    raw = _b64.b64decode("".join(b64))
    try:
        from PIL import Image, ImageTk
        img = Image.open(_io.BytesIO(raw)).convert("RGBA")
        img = img.resize((max(1, w), max(1, h)), Image.LANCZOS)
        return ImageTk.PhotoImage(img)
    except Exception:  # noqa: BLE001
        ph = tk.PhotoImage(data=_b64.b64encode(raw))
        f = max(1, round(ph.width() / max(1, w)))
        return ph.subsample(f, f) if f > 1 else ph


def _write_logo_ico() -> str | None:
    """Écrit le logo en .ico (plusieurs tailles) dans le dossier temp."""
    try:
        import base64 as _b64
        import io as _io
        import tempfile
        from PIL import Image
        path = os.path.join(tempfile.gettempdir(), "botglobalexam_logo.ico")
        img = Image.open(_io.BytesIO(_b64.b64decode("".join(LOGO_ICON_B64))))
        img.convert("RGBA").resize((256, 256), Image.LANCZOS).save(
            path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                         (128, 128), (256, 256)])
        return path
    except Exception:  # noqa: BLE001
        return None


def _force_win_icon(root, ico: str) -> None:
    """Impose l'icône (barre de titre + barre des tâches) via Win32."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                      wintypes.UINT, ctypes.c_int,
                                      ctypes.c_int, wintypes.UINT]
        user32.GetParent.restype = wintypes.HWND
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
        WM_SETICON, ICON_SMALL, ICON_BIG = 0x80, 0, 1
        big = user32.LoadImageW(None, ico, IMAGE_ICON, 256, 256,
                                LR_LOADFROMFILE)
        small = user32.LoadImageW(None, ico, IMAGE_ICON, 32, 32,
                                  LR_LOADFROMFILE)
        if big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
        if small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
        root._win_icons = (big, small)   # garder les handles en vie
    except Exception:  # noqa: BLE001
        pass


# ── MISE À JOUR AUTOMATIQUE (GitHub) ──────────────────────────────────
# Au lancement, le launcher compare la version du bot embarqué dans l'exe
# avec version.txt du dépôt GitHub. Si GitHub est plus récent, il
# télécharge bot.py dans le dossier « maj » (à côté de l'exe) et l'utilise
# à la place : PLUS BESOIN DE RECONSTRUIRE l'exe pour une correction du
# bot. (Une modif du launcher lui-même demande toujours Construction.bat.)
UPDATE_REPO = "tomascdj0903/bot-globalexam"
UPDATE_RAW = f"https://raw.githubusercontent.com/{UPDATE_REPO}/main/"


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
        if base.name.lower() == "dist":
            base = base.parent
        return base
    return Path(__file__).resolve().parent


def _ver(v) -> tuple:
    import re as _re
    return tuple(int(x) for x in _re.findall(r"\d+", str(v or "0"))) or (0,)


def _file_version(path: Path) -> str | None:
    import re as _re
    try:
        m = _re.search(r'^BOT_VERSION\s*=\s*"([^"]+)"',
                       path.read_text(encoding="utf-8"), _re.M)
        return m.group(1) if m else None
    except OSError:
        return None


def _import_bot_file(path: Path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("bot", str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bot"] = mod
    spec.loader.exec_module(mod)
    return mod


def load_bot_module(log):
    """Charge le bot le plus récent : GitHub > copie « maj » > exe."""
    import urllib.request
    import bot as bundled                      # version embarquée
    best_ver = getattr(bundled, "BOT_VERSION", "0")
    upd = _app_dir() / "maj" / "bot.py"
    remote = None
    try:
        remote = urllib.request.urlopen(
            UPDATE_RAW + "version.txt", timeout=6).read().decode().strip()
    except Exception as e:  # noqa: BLE001
        log(f"ℹ️ Vérification des mises à jour impossible ({str(e)[:50]}) "
            f"— version locale utilisée.")
    if remote and _ver(remote) > _ver(best_ver) and _ver(remote) > _ver(
            _file_version(upd)):
        try:
            src = urllib.request.urlopen(
                UPDATE_RAW + "bot.py", timeout=30).read().decode("utf-8")
            compile(src, "bot.py", "exec")         # fichier valide ?
            if f'BOT_VERSION = "{remote}"' not in src:
                raise ValueError("version incohérente")
            upd.parent.mkdir(parents=True, exist_ok=True)
            # Octets bruts, fins de ligne « \n » : write_text sous Windows
            # transformait « \r\n » en « \r\r\n » → fichier illisible.
            upd.write_bytes(src.replace("\r\n", "\n").encode("utf-8"))
            log(f"⬇️ Mise à jour du bot téléchargée : v{remote}")
        except Exception as e:  # noqa: BLE001
            log(f"⚠️ Mise à jour non téléchargée : {str(e)[:60]}")
    cached = _file_version(upd)
    if cached and _ver(cached) > _ver(best_ver):
        try:
            mod = _import_bot_file(upd)
            log(f"🔄 Bot à jour : v{cached} (exe : v{best_ver})")
            return mod
        except Exception as e:  # noqa: BLE001
            sys.modules["bot"] = bundled
            try:
                upd.unlink()            # sera re-téléchargée au prochain lancement
            except OSError:
                pass
            log(f"⚠️ Mise à jour illisible ({str(e)[:60]}) → version de "
                f"l'exe v{best_ver}")
    else:
        log(f"✅ Bot v{best_ver}")
    return bundled


# Durées proposées (minutes). 0 = illimité.
DURATIONS = [(30, "30 min"), (60, "1 h"), (90, "1 h 30"),
             (120, "2 h"), (0, "Illimité")]

# Palette (thème sombre, accent violet)
C = {
    "bg": "#0E1016",
    "card": "#171A23",
    "card2": "#212534",
    "hover": "#2A2F42",
    "press": "#1A1E2B",
    "border": "#2A3040",
    "log": "#0A0C11",
    "text": "#E9ECF3",
    "muted": "#8D94A6",
    "faint": "#5B6274",
    "accent": "#7C5CFF",
    "accent_hi": "#957BFF",
    "accent_lo": "#5F45D6",
    "ok": "#34C88F",
    "warn": "#F2A93B",
    "err": "#F0596B",
    "info": "#62B0FF",
}

# Journal « essentiel » : lignes de détail technique masquées par défaut.
LOG_DETAIL_MARKERS = (
    "📊 Tokens", "🔎 Recherche", "💭", "📨", "🕵️", "🔬", "👁️", "🪶",
    "Draggable-item trouvé", "🎯 Cible", "📋 A =", "📋 B =", "🔁 Passe",
    "📋 FINAL", "Stock (", "Badges numérotés", "Zones drop", "Zones vides",
    "Images exercice", "Drag classique via", "Clic accumulateur via",
    "CDP", "Label cliqué", "Radio cliqué", "Checkbox", "Retry scan",
    "Aperçu :", "Solveur Python", "🌐 Langue :",
)


# =====================================================================
#  UTILITAIRES (hors GUI)
# =====================================================================
def find_chrome() -> str | None:
    """Retourne le chemin de l'exécutable Chrome, ou None si introuvable."""
    for name in ("chrome", "google-chrome", "chrome.exe"):
        p = shutil.which(name)
        if p:
            return p
    candidates = []
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            candidates.append(
                os.path.join(base, "Google", "Chrome",
                             "Application", "chrome.exe"))
    candidates += [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        str(Path.home() /
            "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def port_is_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(host: str, port: int, total: float = 25.0) -> bool:
    deadline = time.time() + total
    while time.time() < deadline:
        if port_is_open(host, port):
            return True
        time.sleep(0.4)
    return False


def load_config() -> dict:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def project_dir() -> Path:
    """Dossier du projet (à côté de l'exe ; si l'exe est dans dist/, on
    remonte d'un cran)."""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
        if base.name.lower() == "dist":
            base = base.parent
    else:
        base = Path(__file__).resolve().parent
    return base


LOG_DIR_NAME = "journaux"
LOG_KEEP = 30  # nombre de journaux de session conservés


def fmt_hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_duration_label(minutes: int) -> str:
    if minutes <= 0:
        return "Illimité"
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} h {m:02d}"
    if h:
        return f"{h} h"
    return f"{m} min"


def enable_windows_dpi_awareness() -> None:
    """Texte net sur les écrans haute résolution (Windows)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def use_dark_title_bar(root: tk.Tk) -> None:
    """Barre de titre sombre sous Windows 10/11 (sans effet ailleurs)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (Win11/Win10)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value),
                    ctypes.sizeof(value)) == 0:
                break
    except Exception:  # noqa: BLE001
        pass


# =====================================================================
#  REDIRECTION stdout → file d'attente (pour afficher les print du bot)
# =====================================================================
class QueueWriter:
    """Remplace sys.stdout : chaque écriture part dans une queue
    thread-safe, vidée périodiquement par le thread de l'interface."""

    def __init__(self, q: "queue.Queue[str]"):
        self.q = q

    def write(self, text: str) -> int:
        if text:
            self.q.put(text)
        return len(text)

    def flush(self) -> None:
        pass


# =====================================================================
#  WIDGETS MAISON (tkinter pur, coins arrondis dessinés sur Canvas)
# =====================================================================
def round_rect(cv: tk.Canvas, x1, y1, x2, y2, r, **kw):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
           x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
           x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


class RoundButton(tk.Canvas):
    """Bouton plat à coins arrondis, avec survol / clic / désactivé."""

    KINDS = {
        "primary": (C["accent"], C["accent_hi"], C["accent_lo"], "#FFFFFF"),
        "ghost": (C["card2"], C["hover"], C["press"], C["text"]),
        "chip": (C["card2"], C["hover"], C["press"], C["muted"]),
        "chip_on": (C["accent"], C["accent_hi"], C["accent_lo"], "#FFFFFF"),
        "ok": ("#1F3A31", "#25473B", "#1A3129", C["ok"]),
        "warn": ("#3B3020", "#4A3C27", "#30271A", C["warn"]),
    }

    def __init__(self, parent, text: str, command=None, kind: str = "primary",
                 height: int = 40, font=None, radius: int = 10, width: int = 0):
        super().__init__(parent, height=height, width=width or 10,
                         bg=parent["bg"], highlightthickness=0, bd=0,
                         cursor="hand2")
        self.text = text
        self.command = command
        self.kind = kind
        self.radius = radius
        self.font = font
        self.enabled = True
        self._hover = False
        self._press = False
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _on_enter(self, _e):
        self._hover = True
        self._draw()

    def _on_leave(self, _e):
        self._hover = False
        self._press = False
        self._draw()

    def _on_press(self, _e):
        if self.enabled:
            self._press = True
            self._draw()

    def _on_release(self, _e):
        fire = self._press and self._hover and self.enabled
        self._press = False
        self._draw()
        if fire and self.command:
            self.command()

    def set(self, text=None, kind=None, enabled=None, command=None):
        if text is not None:
            self.text = text
        if kind is not None:
            self.kind = kind
        if enabled is not None:
            self.enabled = enabled
            self.configure(cursor="hand2" if enabled else "arrow")
        if command is not None:
            self.command = command
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4:
            return
        base, hover, press, fg = self.KINDS.get(self.kind,
                                                self.KINDS["primary"])
        if not self.enabled:
            fill, fg = C["card2"], C["faint"]
        elif self._press:
            fill = press
        elif self._hover:
            fill = hover
        else:
            fill = base
        round_rect(self, 0, 0, w - 1, h - 1, self.radius,
                   fill=fill, outline=fill)
        self.create_text(w / 2, h / 2, text=self.text, fill=fg,
                         font=self.font)


class StatusPill(tk.Canvas):
    """Pastille d'état : point coloré + texte, dans une gélule."""

    def __init__(self, parent, font, height: int):
        super().__init__(parent, height=height, width=120, bg=parent["bg"],
                         highlightthickness=0, bd=0)
        self.font = font
        self.h = height
        self.set("Prêt", C["muted"])

    def set(self, text: str, color: str):
        f = tkfont.Font(font=self.font)
        pad = int(self.h * 0.55)
        dot = int(self.h * 0.28)
        w = f.measure(text) + pad * 2 + dot + int(self.h * 0.3)
        self.configure(width=w)
        self.delete("all")
        round_rect(self, 0, 0, w - 1, self.h - 1, self.h / 2,
                   fill=C["card2"], outline=C["border"])
        cy = self.h / 2
        self.create_oval(pad, cy - dot / 2, pad + dot, cy + dot / 2,
                         fill=color, outline=color)
        self.create_text(pad + dot + int(self.h * 0.3), cy, text=text,
                         anchor="w", fill=C["text"], font=self.font)


class ProgressBar(tk.Canvas):
    def __init__(self, parent, height: int):
        super().__init__(parent, height=height, bg=parent["bg"],
                         highlightthickness=0, bd=0)
        self.frac = None
        self.color = C["accent"]
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, frac, color=None):
        self.frac = frac
        if color:
            self.color = color
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            return
        round_rect(self, 0, 0, w - 1, h - 1, h / 2,
                   fill=C["card2"], outline=C["card2"])
        if self.frac is not None and self.frac > 0:
            fw = max(h, int((w - 1) * min(1.0, self.frac)))
            round_rect(self, 0, 0, fw, h - 1, h / 2,
                       fill=self.color, outline=self.color)


# =====================================================================
#  FENÊTRE PRINCIPALE
# =====================================================================
class LauncherApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.chrome_proc: subprocess.Popen | None = None
        self.bot_module = None          # module bot (RUN_GATE, STOP…)
        self.bot_instance = None        # HybridBot (stats, coût)
        # Phases : idle → launching → ready → running ⇄ paused
        #          → stopping → done   (ou error)
        self.phase = "idle"
        self.stop_sent = False
        self.worked_s = 0.0
        self._last_tick = time.monotonic()
        self._partial = ""
        self._log_lines: deque = deque(maxlen=5000)
        self.show_details = False
        self._search_all = False      # recherche active → tout afficher
        self._matches: list = []
        self._match_idx = -1
        self._log_file = None

        cfg = load_config()
        self.lang_code = cfg.get("langue", "en")
        self.key_mode = cfg.get("key_mode", "fournie")
        self.debug_mode = bool(cfg.get("debug", False))
        self.bot_module = None
        try:
            self.duration_min = int(cfg.get("duree_min", 60))
        except (TypeError, ValueError):
            self.duration_min = 60

        self._setup_scaling_and_fonts()
        self._build_ui(cfg)

        self.root.after(100, self._drain_log)
        self.root.after(1000, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----------------------------------------------------------------
    #  Échelle (écrans haute résolution) + polices
    # ----------------------------------------------------------------
    def _setup_scaling_and_fonts(self) -> None:
        try:
            self.S = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        except tk.TclError:
            self.S = 1.0
        fams = set(tkfont.families(self.root))

        def pick(*names, default="TkDefaultFont"):
            for n in names:
                if n in fams:
                    return n
            return default

        ui = pick("Segoe UI Variable Text", "Segoe UI", "Inter", "SF Pro Text",
                  "Helvetica Neue", "Helvetica", "Arial")
        disp = pick("Segoe UI Variable Display", "Segoe UI Semibold",
                    "Segoe UI", "Inter Display", ui)
        mono = pick("Cascadia Mono", "Cascadia Code", "Consolas", "Menlo",
                    "DejaVu Sans Mono",
                    "Courier New", default="TkFixedFont")
        self.F = {
            "title": (disp, 17, "bold"),
            "sub": (ui, 10),
            "card": (ui, 9, "bold"),
            "body": (ui, 10),
            "small": (ui, 9),
            "chip": (ui, 10),
            "btn": (ui, 11, "bold"),
            "stat": (disp, 20, "bold"),
            "statlbl": (ui, 8, "bold"),
            "pill": (ui, 10),
            "log": (mono, 9),
            "logo": (disp, 14, "bold"),
        }

    def px(self, n: float) -> int:
        return int(round(n * self.S))

    # ----------------------------------------------------------------
    #  Construction de l'interface
    # ----------------------------------------------------------------
    def _card(self, parent, title: str | None = None) -> tk.Frame:
        outer = tk.Frame(parent, bg=C["card"], highlightthickness=1,
                         highlightbackground=C["border"],
                         highlightcolor=C["border"])
        inner = tk.Frame(outer, bg=C["card"])
        inner.pack(fill="both", expand=True, padx=self.px(14),
                   pady=self.px(12))
        if title:
            tk.Label(inner, text=title.upper(), bg=C["card"], fg=C["muted"],
                     font=self.F["card"]).pack(anchor="w",
                                               pady=(0, self.px(8)))
        outer.inner = inner
        return outer

    def _build_ui(self, cfg: dict) -> None:
        r = self.root
        r.title("Bot GlobalExam")
        r.configure(bg=C["bg"])
        w, h = self.px(1080), self.px(720)
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        w, h = min(w, sw - 40), min(h, sh - 80)
        r.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        r.minsize(min(self.px(900), sw - 40), min(self.px(600), sh - 80))

        style = ttk.Style(r)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Dark.Vertical.TScrollbar", background=C["card2"],
                        troughcolor=C["log"], bordercolor=C["log"],
                        arrowcolor=C["muted"], lightcolor=C["card2"],
                        darkcolor=C["card2"], gripcount=0)
        style.map("Dark.Vertical.TScrollbar",
                  background=[("active", C["hover"])])

        pad = self.px(20)

        # ── En-tête ────────────────────────────────────────────────
        head = tk.Frame(r, bg=C["bg"])
        head.pack(fill="x", padx=pad, pady=(pad, self.px(14)))
        self._logo_img = _logo_image(LOGO_HEADER_B64, self.px(75),
                                     self.px(44))
        logo = tk.Label(head, image=self._logo_img, bg=C["bg"], bd=0)
        logo.pack(side="left")
        try:
            self._icon_img = _logo_image(LOGO_ICON_B64, 64, 64)
            r.iconphoto(True, self._icon_img)
        except tk.TclError:
            pass
        # Windows : la barre des tâches garde la plume Tk tant qu'on ne lui
        # donne pas un vrai .ico. On fabrique le .ico (logo intégré) puis on
        # l'impose à la fenêtre via l'API Windows (WM_SETICON).
        if sys.platform == "win32":
            ico = _write_logo_ico()
            if ico:
                try:
                    r.iconbitmap(default=ico)
                except tk.TclError:
                    pass
                r.after(300, lambda: _force_win_icon(r, ico))
        tt = tk.Frame(head, bg=C["bg"])
        tt.pack(side="left", padx=(self.px(12), 0))
        tk.Label(tt, text="Bot GlobalExam", bg=C["bg"], fg=C["text"],
                 font=self.F["title"]).pack(anchor="w")
        tk.Label(tt, text="Tes exercices de langue en pilote automatique",
                 bg=C["bg"], fg=C["muted"], font=self.F["sub"]).pack(anchor="w")
        self.pill = StatusPill(head, self.F["pill"], self.px(32))
        self.pill.pack(side="right")
        # Bouton « Mode debug » (Oui = pause à chaque erreur)
        self.debug_btn = RoundButton(head, "Mode debug : NON", kind="chip",
                                     height=self.px(32), font=self.F["chip"],
                                     radius=self.px(9), width=self.px(170),
                                     command=lambda: self._select_debug(
                                         not self.debug_mode))
        self.debug_btn.pack(side="right", padx=(0, self.px(10)))

        # ── Corps : barre latérale + panneau principal ─────────────
        body = tk.Frame(r, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=pad, pady=(0, pad))
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        side = tk.Frame(body, bg=C["bg"], width=self.px(340))
        side.grid(row=0, column=0, sticky="ns", padx=(0, self.px(16)))
        side.grid_propagate(False)
        side.pack_propagate(False)
        main = tk.Frame(body, bg=C["bg"])
        main.grid(row=0, column=1, sticky="nsew")

        gap = self.px(12)

        # Langue
        lc = self._card(side, "Langue")
        lc.pack(fill="x", pady=(0, gap))
        grid = tk.Frame(lc.inner, bg=C["card"])
        grid.pack(fill="x")
        self.lang_btns = {}
        for i, (code, name) in enumerate(LANGUES):
            b = RoundButton(grid, name, kind="chip", height=self.px(34),
                            font=self.F["chip"], radius=self.px(9),
                            command=lambda c=code: self._select_lang(c))
            b.grid(row=i // 3, column=i % 3, sticky="ew",
                   padx=(0 if i % 3 == 0 else self.px(6), 0),
                   pady=(0 if i < 3 else self.px(6), 0))
            self.lang_btns[code] = b
        for c in range(3):
            grid.grid_columnconfigure(c, weight=1, uniform="lang")

        # Durée de travail
        dc = self._card(side, "Durée de travail")
        dc.pack(fill="x", pady=(0, gap))
        dg = tk.Frame(dc.inner, bg=C["card"])
        dg.pack(fill="x")
        self.dur_btns = {}
        for i, (mins, label) in enumerate(DURATIONS):
            b = RoundButton(dg, label, kind="chip", height=self.px(34),
                            font=self.F["chip"], radius=self.px(9),
                            command=lambda m=mins: self._select_duration(m))
            b.grid(row=i // 3, column=i % 3, sticky="ew",
                   padx=(0 if i % 3 == 0 else self.px(6), 0),
                   pady=(0 if i < 3 else self.px(6), 0))
            self.dur_btns[mins] = b
        for c in range(3):
            dg.grid_columnconfigure(c, weight=1, uniform="dur")
        # Durée personnalisée (3e case de la 2e ligne)
        cust = tk.Frame(dg, bg=C["card2"], highlightthickness=1,
                        highlightbackground=C["card2"],
                        highlightcolor=C["accent"])
        cust.grid(row=1, column=2, sticky="nsew", padx=(self.px(6), 0),
                  pady=(self.px(6), 0))
        self.custom_var = tk.StringVar(value="")
        self.custom_entry = tk.Entry(
            cust, textvariable=self.custom_var, width=4, justify="center",
            bg=C["card2"], fg=C["text"], insertbackground=C["text"],
            relief="flat", font=self.F["chip"], bd=0, highlightthickness=0,
            disabledbackground=C["card2"], disabledforeground=C["faint"])
        self.custom_entry.pack(side="left", fill="both", expand=True,
                               padx=(self.px(6), 0))
        tk.Label(cust, text="min", bg=C["card2"], fg=C["muted"],
                 font=self.F["small"]).pack(side="left",
                                            padx=(0, self.px(8)))
        self.custom_entry.bind("<KeyRelease>", self._on_custom_duration)
        self.custom_frame = cust
        self.dur_hint = tk.Label(dc.inner, text="", bg=C["card"],
                                 fg=C["muted"], font=self.F["small"],
                                 justify="left", anchor="w",
                                 wraplength=self.px(300))
        self.dur_hint.pack(fill="x", pady=(self.px(8), 0))

        # Clé API
        kc = self._card(side, "Clé API Gemini")
        kc.pack(fill="x", pady=(0, gap))
        kg = tk.Frame(kc.inner, bg=C["card"])
        kg.pack(fill="x")
        self.key_btns = {}
        for i, (mode, label) in enumerate((("fournie", "Clé fournie"),
                                           ("perso", "Ma propre clé"))):
            b = RoundButton(kg, label, kind="chip", height=self.px(34),
                            font=self.F["chip"], radius=self.px(9),
                            command=lambda m=mode: self._select_key_mode(m))
            b.grid(row=0, column=i, sticky="ew",
                   padx=(0 if i == 0 else self.px(6), 0))
            self.key_btns[mode] = b
        kg.grid_columnconfigure(0, weight=1, uniform="key")
        kg.grid_columnconfigure(1, weight=1, uniform="key")
        self.key_row = tk.Frame(kc.inner, bg=C["card2"], highlightthickness=1,
                                highlightbackground=C["border"],
                                highlightcolor=C["accent"])
        self.key_entry = tk.Entry(
            self.key_row, show="•", bg=C["card2"], fg=C["text"],
            insertbackground=C["text"], relief="flat", bd=0,
            highlightthickness=0,
            font=self.F["body"], disabledbackground=C["card2"],
            disabledforeground=C["faint"])
        self.key_entry.pack(side="left", fill="x", expand=True,
                            padx=self.px(10), pady=self.px(8))
        self.key_entry.insert(0, cfg.get("api_key", ""))
        self.eye = tk.Label(self.key_row, text="Voir", bg=C["card2"],
                            fg=C["muted"], font=self.F["small"],
                            cursor="hand2")
        self.eye.pack(side="right", padx=(0, self.px(10)))
        self.eye.bind("<Button-1>", lambda _e: self._toggle_key_visible())
        self.key_hint = tk.Label(kc.inner, text="", bg=C["card"],
                                 fg=C["muted"], font=self.F["small"],
                                 justify="left", anchor="w",
                                 wraplength=self.px(300))
        self.key_hint.pack(fill="x", pady=(self.px(8), 0))

        # Boutons d'action
        self.primary = RoundButton(side, "Démarrer", kind="primary",
                                   height=self.px(48), font=self.F["btn"],
                                   radius=self.px(12),
                                   command=self._on_primary)
        self.primary.pack(fill="x", pady=(self.px(4), 0))
        self.secondary = RoundButton(side, "Terminer après cet exercice",
                                     kind="ghost", height=self.px(38),
                                     font=self.F["body"], radius=self.px(10),
                                     command=self._on_finish_request)
        self.hint = tk.Label(side, text="Choisis tes réglages puis clique "
                                        "« Démarrer ». Chrome va s'ouvrir.",
                             bg=C["bg"], fg=C["muted"], font=self.F["small"],
                             justify="left", anchor="w",
                             wraplength=self.px(330))
        self.hint.pack(fill="x", pady=(self.px(10), 0))

        # ── Panneau principal : stats + progression + journal ──────
        stats = tk.Frame(main, bg=C["bg"])
        stats.pack(fill="x")
        self.tiles = {}
        specs = [("remain", "Temps restant"), ("worked", "Temps de travail"),
                 ("exos", "Exercices"), ("cost", "Coût IA")]
        for i, (key, label) in enumerate(specs):
            card = self._card(stats)
            card.grid(row=0, column=i, sticky="nsew",
                      padx=(0 if i == 0 else self.px(10), 0))
            stats.grid_columnconfigure(i, weight=1, uniform="tiles")
            tk.Label(card.inner, text=label.upper(), bg=C["card"],
                     fg=C["muted"], font=self.F["statlbl"]).pack(anchor="w")
            val = tk.Label(card.inner, text="—", bg=C["card"], fg=C["text"],
                           font=self.F["stat"])
            val.pack(anchor="w", pady=(self.px(2), 0))
            self.tiles[key] = val

        self.progress = ProgressBar(main, self.px(6))
        self.progress.pack(fill="x", pady=(self.px(12), self.px(12)))

        logc = tk.Frame(main, bg=C["card"], highlightthickness=1,
                        highlightbackground=C["border"])
        logc.pack(fill="both", expand=True)
        lh = tk.Frame(logc, bg=C["card"])
        lh.pack(fill="x", padx=self.px(14), pady=(self.px(10), self.px(8)))
        tk.Label(lh, text="JOURNAL", bg=C["card"], fg=C["muted"],
                 font=self.F["card"]).pack(side="left")
        self.detail_btn = RoundButton(lh, "Afficher les détails",
                                      kind="chip", height=self.px(28),
                                      width=self.px(160), font=self.F["small"],
                                      radius=self.px(8),
                                      command=self._toggle_details)
        self.detail_btn.pack(side="right")

        # Recherche (comme Ctrl+F) : surligne toutes les occurrences dans
        # TOUT le journal (détails compris), ▲ ▼ ou Entrée pour naviguer.
        sf = tk.Frame(lh, bg=C["card2"], highlightthickness=1,
                      highlightbackground=C["border"],
                      highlightcolor=C["accent"])
        sf.pack(side="right", padx=(0, self.px(10)))
        tk.Label(sf, text="⌕", bg=C["card2"], fg=C["muted"],
                 font=self.F["body"]).pack(side="left",
                                           padx=(self.px(8), 0))
        self.search_var = tk.StringVar()
        self.search_entry = tk.Entry(
            sf, textvariable=self.search_var, width=24, bg=C["card2"],
            fg=C["text"], insertbackground=C["text"], relief="flat", bd=0,
            highlightthickness=0, font=self.F["small"])
        self.search_entry.pack(side="left", padx=self.px(6),
                               pady=self.px(5))
        self.search_count = tk.Label(sf, text="Ctrl+F", bg=C["card2"],
                                     fg=C["faint"], font=self.F["small"])
        self.search_count.pack(side="left", padx=(0, self.px(6)))
        for txt, d in (("▲", -1), ("▼", 1)):
            arrow = tk.Label(sf, text=txt, bg=C["card2"], fg=C["muted"],
                             font=self.F["small"], cursor="hand2")
            arrow.pack(side="left", padx=(0, self.px(6)))
            arrow.bind("<Button-1>", lambda _e, d=d: self._search_step(d))
        clear = tk.Label(sf, text="✕", bg=C["card2"], fg=C["faint"],
                         font=self.F["small"], cursor="hand2")
        clear.pack(side="left", padx=(0, self.px(8)))
        clear.bind("<Button-1>", lambda _e: self.search_var.set(""))
        self.search_var.trace_add("write",
                                  lambda *_: self._search_update())
        self.search_entry.bind("<Return>", lambda _e: self._search_step(1))
        self.search_entry.bind("<Shift-Return>",
                               lambda _e: self._search_step(-1))
        self.search_entry.bind("<Escape>",
                               lambda _e: self.search_var.set(""))
        r.bind_all("<Control-f>", self._focus_search)
        r.bind_all("<Control-F>", self._focus_search)

        lb = tk.Frame(logc, bg=C["log"])
        lb.pack(fill="both", expand=True, padx=1, pady=(0, 1))
        self.log = tk.Text(
            lb, bg=C["log"], fg=C["text"], font=self.F["log"],
            relief="flat", bd=0, highlightthickness=0, wrap="word",
            padx=self.px(14),
            pady=self.px(10), insertbackground=C["text"],
            selectbackground=C["accent_lo"], spacing1=self.px(1),
            spacing3=self.px(1), state="disabled", cursor="arrow")
        sb = ttk.Scrollbar(lb, orient="vertical", command=self.log.yview,
                           style="Dark.Vertical.TScrollbar")
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        for tag, color in (("ok", C["ok"]), ("warn", C["warn"]),
                           ("err", C["err"]), ("info", C["info"]),
                           ("muted", C["faint"]), ("time", C["faint"])):
            self.log.tag_configure(tag, foreground=color)
        bold_mono = tkfont.Font(font=self.F["log"])
        bold_mono.configure(weight="bold")
        self._bold_mono = bold_mono
        self.log.tag_configure("accent", foreground=C["accent_hi"],
                               font=bold_mono, spacing1=self.px(8))
        self.log.tag_configure("match", background="#3A2F6E")
        self.log.tag_configure("match_cur", background=C["warn"],
                               foreground="#111111")
        self.log.tag_raise("match")
        self.log.tag_raise("match_cur")

        # État initial
        self._select_lang(self.lang_code if self.lang_code in dict(LANGUES)
                          else "en")
        self._select_key_mode(self.key_mode)
        self._select_debug(self.debug_mode)
        if any(m == self.duration_min for m, _ in DURATIONS):
            self._select_duration(self.duration_min)
        else:
            self.custom_var.set(str(self.duration_min))
            self._on_custom_duration()
        self._set_phase("idle")
        self._refresh_stats()

    # ----------------------------------------------------------------
    #  Réglages
    # ----------------------------------------------------------------
    def _select_lang(self, code: str) -> None:
        if self.phase not in ("idle",):
            return
        self.lang_code = code
        for c, b in self.lang_btns.items():
            b.set(kind="chip_on" if c == code else "chip")

    def _select_debug(self, on: bool) -> None:
        """Mode debug : Oui = pause à chaque erreur (+ notification) ;
        Non = l'erreur est enregistrée dans « erreurs\\ » et le bot
        continue. Modifiable à tout moment, même pendant le travail."""
        self.debug_mode = bool(on)
        self.debug_btn.set(
            text="Mode debug : OUI" if self.debug_mode
            else "Mode debug : NON",
            kind="warn" if self.debug_mode else "chip")
        if self.bot_module is not None and hasattr(self.bot_module,
                                                   "DEBUG_PAUSE"):
            self.bot_module.DEBUG_PAUSE = self.debug_mode
        cfg = load_config()
        if cfg.get("debug") != self.debug_mode:
            cfg["debug"] = self.debug_mode
            save_config(cfg)

    def _select_key_mode(self, mode: str) -> None:
        if self.phase not in ("idle",):
            return
        self.key_mode = mode if mode in ("fournie", "perso") else "fournie"
        for m, b in self.key_btns.items():
            b.set(kind="chip_on" if m == self.key_mode else "chip")
        if self.key_mode == "perso":
            self.key_row.pack(fill="x", pady=(self.px(10), 0),
                              before=self.key_hint)
            self.key_hint.configure(
                text="Colle ta clé Gemini (gratuite sur AI Studio, "
                     "elle commence par « AIza » ou « AQ. »).")
        else:
            self.key_row.pack_forget()
            self.key_hint.configure(
                text="Utilise la clé intégrée, facturée 2 €/h (réglé à part).")

    def _toggle_key_visible(self) -> None:
        hidden = self.key_entry.cget("show") == "•"
        self.key_entry.configure(show="" if hidden else "•")
        self.eye.configure(text="Masquer" if hidden else "Voir")

    def _duration_editable(self) -> bool:
        return not self.stop_sent and self.phase not in ("stopping", "done",
                                                         "error")

    def _select_duration(self, minutes: int) -> None:
        if not self._duration_editable():
            return
        self.duration_min = minutes
        for m, b in self.dur_btns.items():
            b.set(kind="chip_on" if m == minutes else "chip")
        self.custom_var.set("")
        self.custom_frame.configure(highlightbackground=C["card2"])
        self._update_duration_hint()
        self._refresh_stats()

    def _on_custom_duration(self, _e=None) -> None:
        if not self._duration_editable():
            return
        raw = self.custom_var.get().strip()
        if not raw:
            return
        try:
            minutes = int(raw)
        except ValueError:
            self.custom_frame.configure(highlightbackground=C["err"])
            return
        if minutes <= 0:
            self.custom_frame.configure(highlightbackground=C["err"])
            return
        self.duration_min = minutes
        for b in self.dur_btns.values():
            b.set(kind="chip")
        self.custom_frame.configure(highlightbackground=C["accent"])
        self._update_duration_hint()
        self._refresh_stats()

    def _update_duration_hint(self) -> None:
        if self.duration_min <= 0:
            txt = "Le bot tourne jusqu'à ce que tu l'arrêtes."
        else:
            txt = (f"{fmt_duration_label(self.duration_min)} de travail, "
                   "puis le bot finit l'exercice en cours et ferme l'appli.")
        self.dur_hint.configure(text=txt)

    @property
    def limit_s(self) -> float | None:
        return self.duration_min * 60 if self.duration_min > 0 else None

    # ----------------------------------------------------------------
    #  Phases (état global de l'interface)
    # ----------------------------------------------------------------
    PHASES = {
        "idle": ("Prêt", "muted"),
        "launching": ("Ouverture de Chrome…", "warn"),
        "ready": ("En attente de ton feu vert", "warn"),
        "running": ("En cours", "ok"),
        "paused": ("En pause", "warn"),
        "error_pause": ("Erreur à vérifier", "err"),
        "stopping": ("Fin de l'exercice en cours…", "info"),
        "done": ("Session terminée", "ok"),
        "error": ("Arrêté", "err"),
    }

    def _set_phase(self, phase: str, hint: str | None = None) -> None:
        self.phase = phase
        text, color = self.PHASES.get(phase, ("", "muted"))
        self.pill.set(text, C[color])
        p, s = self.primary, self.secondary
        locked = phase != "idle"
        for b in list(self.lang_btns.values()) + list(self.key_btns.values()):
            b.set(enabled=not locked or b.kind == "chip_on")
        self.key_entry.configure(state="disabled" if locked else "normal")
        dur_ok = self._duration_editable()
        for b in self.dur_btns.values():
            b.set(enabled=dur_ok or b.kind == "chip_on")
        self.custom_entry.configure(state="normal" if dur_ok else "disabled")

        if phase == "idle":
            p.set("Démarrer", kind="primary", enabled=True)
        elif phase == "launching":
            p.set("Ouverture de Chrome…", kind="primary", enabled=False)
        elif phase == "ready":
            p.set("Commencer les exercices", kind="primary", enabled=True)
        elif phase == "running":
            p.set("Pause", kind="ghost", enabled=True)
        elif phase in ("paused", "error_pause"):
            p.set("Reprendre", kind="primary", enabled=True)
        elif phase == "stopping":
            p.set("Fin de l'exercice en cours…", kind="ghost", enabled=False)
        elif phase == "done":
            p.set("Session terminée", kind="ok", enabled=False)
        elif phase == "error":
            p.set("Bot arrêté", kind="ghost", enabled=False)

        if phase in ("ready", "running", "paused", "error_pause"):
            if not s.winfo_ismapped():
                s.pack(fill="x", pady=(self.px(8), 0), after=p)
            s.set(text="Quitter" if phase == "ready"
                  else "Terminer après cet exercice", enabled=True)
        elif phase == "stopping":
            s.set(enabled=False)
        else:
            s.pack_forget()

        default_hints = {
            "idle": "Choisis tes réglages puis clique « Démarrer ». "
                    "Chrome va s'ouvrir.",
            "launching": "Chrome s'ouvre avec ton profil dédié…",
            "ready": "1. Connecte-toi à GlobalExam dans Chrome\n"
                     "2. Ouvre ta leçon ou ton parcours\n"
                     "3. Clique « Commencer les exercices »",
            "running": "Le minuteur ne compte que lorsque le bot travaille.",
            "paused": "Le bot finit son action puis attend. "
                      "Le minuteur est arrêté.",
            "stopping": "Le bot termine l'exercice en cours, "
                        "puis tout se ferme.",
        }
        self.hint.configure(text=hint or default_hints.get(phase, ""),
                            fg=C["err"] if phase in ("error", "error_pause")
                            else C["muted"])

    # ----------------------------------------------------------------
    #  Boutons d'action
    # ----------------------------------------------------------------
    def _on_primary(self) -> None:
        if self.phase == "idle":
            self._on_start()
            return
        gate = self.bot_module.RUN_GATE if self.bot_module else None
        if gate is None:
            return
        if self.phase in ("ready", "paused", "error_pause"):
            self._last_tick = time.monotonic()
            gate.set()
            self._set_phase("running")
        elif self.phase == "running":
            gate.clear()
            self._set_phase("paused")

    def _on_finish_request(self) -> None:
        if self.phase in ("ready",):
            # Rien n'a encore commencé : on ferme directement.
            self._on_close(force=True)
            return
        self._request_stop("Arrêt demandé : le bot finit l'exercice "
                           "en cours, puis tout se ferme.")

    def _request_stop(self, hint: str) -> None:
        if self.stop_sent or not self.bot_module:
            return
        self.stop_sent = True
        stop = getattr(self.bot_module, "STOP_REQUESTED", None)
        if stop is not None:
            stop.set()
        # S'il était en pause, on le relance pour qu'il puisse conclure.
        self.bot_module.RUN_GATE.set()
        self._last_tick = time.monotonic()
        self._set_phase("stopping", hint)
        self._log("⏱️  " + hint)

    # ----------------------------------------------------------------
    #  DÉMARRAGE
    # ----------------------------------------------------------------
    def _on_start(self) -> None:
        api_key = self.key_entry.get().strip()
        if self.key_mode == "perso":
            if not api_key:
                messagebox.showwarning(
                    "Clé manquante",
                    "Tu as choisi « Ma propre clé » mais le champ est vide.\n"
                    "Colle ta clé Gemini, ou choisis « Clé fournie ».")
                return
            api_key = "".join(api_key.split())   # espaces collés par erreur
            if not api_key.startswith(("AIza", "AQ.")):
                if not messagebox.askyesno(
                        "Clé inhabituelle",
                        "Cette clé ne ressemble pas à une clé Gemini "
                        "(elles commencent par « AIza » ou « AQ. »).\n"
                        "Lancer quand même ?"):
                    return
        save_config({
            "key_mode": self.key_mode,
            "api_key": api_key if self.key_mode == "perso" else "",
            "langue": self.lang_code,
            "duree_min": self.duration_min,
            "debug": self.debug_mode,
        })
        self._set_phase("launching")
        threading.Thread(target=self._run_everything,
                         args=(self.key_mode, api_key, self.lang_code),
                         daemon=True).start()

    def _ui(self, fn, *args) -> None:
        """Exécute fn dans le thread de l'interface (thread-safe)."""
        self.root.after(0, lambda: fn(*args))

    def _run_everything(self, key_mode: str, api_key: str, lang: str) -> None:
        chrome = find_chrome()
        if not chrome:
            self._log("❌ Chrome introuvable. Installe Google Chrome, "
                      "puis relance.")
            self._ui(self._set_phase, "error", "Chrome introuvable.")
            return

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._log("🌐 Ouverture de Chrome (profil dédié)…")
        try:
            self.chrome_proc = subprocess.Popen([
                chrome,
                f"--remote-debugging-port={DEBUG_PORT}",
                f"--user-data-dir={PROFILE_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
                # Le site peut lancer le son des exercices même après un
                # rechargement de page (sinon Chrome bloque l'audio →
                # « Pronounce » reste grisé).
                "--autoplay-policy=no-user-gesture-required",
                START_URL,
            ])
        except OSError as e:
            self._log(f"❌ Impossible de lancer Chrome : {e}")
            self._ui(self._set_phase, "error", "Échec du lancement de Chrome.")
            return

        self._log("⏳ Attente de Chrome…")
        if not wait_for_port("127.0.0.1", DEBUG_PORT, total=25):
            self._log("❌ Chrome ne répond pas. Ferme TOUTES les fenêtres "
                      "Chrome puis relance l'appli.")
            self._ui(self._set_phase, "error",
                     "Chrome ne répond pas : ferme toutes ses fenêtres "
                     "puis relance.")
            return
        self._log("✅ Chrome prêt.")

        try:
            bot = load_bot_module(self._log)
        except Exception as e:  # noqa: BLE001
            self._log(f"❌ Impossible de charger le bot : {e}")
            self._ui(self._set_phase, "error", "Erreur de chargement du bot.")
            return
        self.bot_module = bot
        if hasattr(bot, "DEBUG_PAUSE"):
            bot.DEBUG_PAUSE = self.debug_mode
            self._log("🐞 Mode debug : " + ("OUI (pause à chaque erreur)"
                      if self.debug_mode else
                      "NON (erreurs enregistrées sans pause)"))

        def _on_auto_pause(folder: str) -> None:
            name = os.path.basename(folder)
            self._ui(self._set_phase, "error_pause",
                     f"Réponse fausse : capture enregistrée dans "
                     f"« erreurs\\{name} ». Demande à Claude de la regarder, "
                     f"puis clique « Reprendre ».")
        if hasattr(bot, "ON_AUTO_PAUSE"):
            bot.ON_AUTO_PAUSE = _on_auto_pause

        if key_mode == "perso":
            bot.API_KEYS[0] = api_key
            self._log("🔑 Clé personnelle utilisée.")
        else:
            self._log("🔑 Clé fournie utilisée (facturée 2 €/h, à part).")

        # Démarre EN PAUSE : le bot se connecte à Chrome puis attend
        # « Commencer ».
        bot.RUN_GATE.clear()
        stop = getattr(bot, "STOP_REQUESTED", None)
        if stop is not None:
            stop.clear()

        sys.stdout = QueueWriter(self.log_queue)
        sys.stderr = QueueWriter(self.log_queue)

        self._ui(self._set_phase, "ready")

        try:
            b = bot.HybridBot(langue=lang)
            self.bot_instance = b
            b.run()
        except SystemExit:
            self._log("❌ Clé API invalide ou modèle indisponible.")
            self._ui(self._set_phase, "error",
                     "Clé API invalide : vérifie-la puis relance l'appli.")
            return
        except Exception as e:  # noqa: BLE001
            self._log(f"\n💥 Le bot s'est arrêté : {e}")
            self._ui(self._set_phase, "error", f"Le bot s'est arrêté : {e}")
            return
        self._ui(self._on_bot_finished)

    def _on_bot_finished(self) -> None:
        if not self.stop_sent:
            self._set_phase("error", "Le bot s'est arrêté.")
            return
        exos = self._stat("exos")
        worked = fmt_hms(self.worked_s)
        self._set_phase("done", f"{exos} exercice(s) en {worked}. "
                                "Fermeture dans quelques secondes…")
        self._log(f"🏁 Session terminée : {exos} exercice(s) en {worked}.")
        notify = getattr(self.bot_module, "notify_phone", None)
        if notify:
            try:
                notify(f"Session terminée : {exos} exercice(s) en {worked}.",
                       title="Bot GlobalExam - termine", priority="default",
                       tags="white_check_mark")
            except Exception:  # noqa: BLE001
                pass
        self._refresh_stats()
        self.root.after(6000, lambda: self._on_close(force=True))

    # ----------------------------------------------------------------
    #  Minuteur + statistiques (toutes les secondes)
    # ----------------------------------------------------------------
    def _tick(self) -> None:
        now = time.monotonic()
        dt = now - self._last_tick
        self._last_tick = now
        gate = self.bot_module.RUN_GATE if self.bot_module else None
        if (self.phase in ("running", "stopping") and gate is not None
                and gate.is_set()):
            self.worked_s += dt
        lim = self.limit_s
        if (lim is not None and not self.stop_sent
                and self.phase == "running" and self.worked_s >= lim):
            self._request_stop("Temps écoulé : le bot finit l'exercice en "
                               "cours, puis tout se ferme.")
        self._refresh_stats()
        self.root.after(1000, self._tick)

    def _stat(self, key: str) -> int:
        b = self.bot_instance
        try:
            return int(getattr(b, "stats", {}).get(key, 0)) if b else 0
        except (TypeError, ValueError):
            return 0

    def _refresh_stats(self) -> None:
        lim = self.limit_s
        if lim is None:
            self.tiles["remain"].configure(text="∞", fg=C["text"])
            self.progress.set(None)
        else:
            remain = lim - self.worked_s
            color = (C["warn"] if 0 < remain <= 300 else
                     C["muted"] if remain <= 0 else C["text"])
            self.tiles["remain"].configure(
                text=fmt_hms(remain) if remain > 0 else "00:00", fg=color)
            self.progress.set(self.worked_s / lim if lim else None,
                              C["warn"] if 0 < remain <= 300 else C["accent"])
        self.tiles["worked"].configure(text=fmt_hms(self.worked_s))
        exos = self._stat("exos")
        errs = self._stat("errors")
        self.tiles["exos"].configure(
            text=f"{exos}" + (f"  ·  {errs} ✗" if errs else ""))
        cost = 0.0
        b = self.bot_instance
        if b is not None:
            try:
                rate = getattr(self.bot_module, "USD_TO_EUR", 0.92)
                cost = float(b._usage.get("cost_usd", 0.0)) * rate
            except (AttributeError, TypeError, ValueError):
                cost = 0.0
        self.tiles["cost"].configure(
            text=f"{cost:.2f} €".replace(".", ","))

    # ----------------------------------------------------------------
    #  Journal
    # ----------------------------------------------------------------
    @staticmethod
    def _tag_for(line: str) -> str:
        s = line.strip()
        if "ANALYSE" in s and "═══" in s:
            return "accent"
        if s.startswith(("❌", "🛑", "💥")) or "FAUSSE" in s:
            return "err"
        if s.startswith(("⚠", "⏸")):
            return "warn"
        if s.startswith(("✅", "🏁", "▶", "➡")):
            return "ok"
        if s.startswith(("📋 PLAN", "📝", "⌨", "⏱", "🎯 Dashboard")):
            return "info"
        if len(line) - len(line.lstrip(" ")) >= 6:
            return "muted"
        return ""

    @staticmethod
    def _is_detail(line: str) -> bool:
        if not line.strip():
            return True
        if len(line) - len(line.lstrip(" ")) >= 6:
            return True
        return any(m in line for m in LOG_DETAIL_MARKERS)

    def _append_line(self, stamp: str, line: str, tag: str) -> None:
        self.log.insert("end", stamp + " ", ("time",))
        self.log.insert("end", line.rstrip() + "\n", (tag,) if tag else ())

    def _visible(self, detail: bool) -> bool:
        return self.show_details or self._search_all or not detail

    def _write_session_file(self, lines: list) -> None:
        """Copie chaque ligne dans journaux/session_<date>.txt (gardé sur
        le disque : utile pour retrouver un problème après coup)."""
        try:
            if self._log_file is None:
                d = project_dir() / LOG_DIR_NAME
                d.mkdir(parents=True, exist_ok=True)
                old = sorted(d.glob("session_*.txt"))
                for f in old[:max(0, len(old) - (LOG_KEEP - 1))]:
                    try:
                        f.unlink()
                    except OSError:
                        pass
                name = time.strftime("session_%Y-%m-%d_%H-%M-%S.txt")
                self._log_file = open(d / name, "a", encoding="utf-8")
            now = time.strftime("%H:%M:%S")
            for line in lines:
                self._log_file.write(f"{now} {line.rstrip()}\n")
            self._log_file.flush()
        except OSError:
            self._log_file = False  # désactivé si le dossier est bloqué

    def _drain_log(self) -> None:
        new = []
        try:
            while True:
                self._partial += self.log_queue.get_nowait()
        except queue.Empty:
            pass
        if "\n" in self._partial:
            *lines, self._partial = self._partial.split("\n")
            if self._log_file is not False:
                self._write_session_file(lines)
            stamp = time.strftime("%H:%M")
            for line in lines:
                item = (stamp, line, self._tag_for(line),
                        self._is_detail(line))
                self._log_lines.append(item)
                if self._visible(item[3]):
                    new.append(item)
        if new:
            at_bottom = self.log.yview()[1] >= 0.999
            self.log.configure(state="normal")
            for stamp, line, tag, _ in new:
                self._append_line(stamp, line, tag)
            excess = int(self.log.index("end-1c").split(".")[0]) - 4000
            if excess > 0:
                self.log.delete("1.0", f"{excess + 1}.0")
            self.log.configure(state="disabled")
            if self.search_var.get().strip():
                self._search_update(keep_index=True, scroll=False)
            if at_bottom and not self.search_var.get().strip():
                self.log.see("end")
        self.root.after(120, self._drain_log)

    def _render_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        for stamp, line, tag, detail in self._log_lines:
            if self._visible(detail):
                self._append_line(stamp, line, tag)
        self.log.configure(state="disabled")
        self.log.see("end")

    def _toggle_details(self) -> None:
        self.show_details = not self.show_details
        self.detail_btn.set(
            text="Masquer les détails" if self.show_details
            else "Afficher les détails",
            kind="chip_on" if self.show_details else "chip")
        self._render_log()
        if self.search_var.get().strip():
            self._search_update()

    # ── Recherche ───────────────────────────────────────────────────
    def _focus_search(self, _e=None):
        self.search_entry.focus_set()
        self.search_entry.select_range(0, "end")
        return "break"

    def _search_update(self, keep_index: bool = False,
                       scroll: bool = True) -> None:
        q = self.search_var.get().strip()
        want_all = bool(q)
        if want_all != self._search_all:
            # Pendant une recherche, on affiche TOUT le journal (détails
            # compris) pour ne rien rater ; on revient ensuite à l'affichage
            # choisi.
            self._search_all = want_all
            self._render_log()
            keep_index = False
        self.log.tag_remove("match", "1.0", "end")
        self.log.tag_remove("match_cur", "1.0", "end")
        self._matches = []
        if not q:
            self._match_idx = -1
            self.search_count.configure(text="Ctrl+F", fg=C["faint"])
            return
        start = "1.0"
        cnt = tk.IntVar()
        while True:
            pos = self.log.search(q, start, stopindex="end", nocase=True,
                                  count=cnt)
            if not pos or cnt.get() <= 0:
                break
            end = f"{pos}+{cnt.get()}c"
            self.log.tag_add("match", pos, end)
            self._matches.append((pos, end))
            start = end
        if not self._matches:
            self._match_idx = -1
            self.search_count.configure(text="0 / 0", fg=C["err"])
            return
        if keep_index and 0 <= self._match_idx < len(self._matches):
            pass
        else:
            self._match_idx = len(self._matches) - 1  # la plus récente
        self._show_match(scroll)

    def _show_match(self, scroll: bool = True) -> None:
        self.log.tag_remove("match_cur", "1.0", "end")
        pos, end = self._matches[self._match_idx]
        self.log.tag_add("match_cur", pos, end)
        if scroll:
            self.log.see(pos)
        self.search_count.configure(
            text=f"{self._match_idx + 1} / {len(self._matches)}",
            fg=C["muted"])

    def _search_step(self, direction: int) -> None:
        if not self._matches:
            self._search_update()
            if not self._matches:
                return
        self._match_idx = (self._match_idx + direction) % len(self._matches)
        self._show_match(True)

    def _log(self, msg: str) -> None:
        self.log_queue.put(msg + "\n")

    # ----------------------------------------------------------------
    #  Fermeture
    # ----------------------------------------------------------------
    def _on_close(self, force: bool = False) -> None:
        if not force and self.phase in ("running", "stopping"):
            if not messagebox.askyesno(
                    "Quitter ?",
                    "Le bot est en train de travailler.\n"
                    "Fermer maintenant (sans finir l'exercice) ?"):
                return
        if self.chrome_proc and self.chrome_proc.poll() is None:
            try:
                self.chrome_proc.terminate()
            except OSError:
                pass
        try:
            self.root.destroy()
        finally:
            os._exit(0)  # tue le thread daemon du bot sans attendre


def main() -> None:
    enable_windows_dpi_awareness()
    # Identité propre dans la barre des tâches Windows (sinon l'appli est
    # regroupée sous « Python/Tk » avec la plume par défaut)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "BotGlobalExam.Launcher")
        except Exception:  # noqa: BLE001
            pass
    root = tk.Tk()
    use_dark_title_bar(root)
    LauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
