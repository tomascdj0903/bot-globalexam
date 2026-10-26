"""
Bot hybride V5.1 — Correcteur d'exercices linguistiques niveau C1/C2
=====================================================================
Refonte complète : corrections de régressions, safe Selenium, caches,
full-page screenshot, factorisation, garde-fous exhaustifs.

Langues supportées : es, fr, en, de, pt, it
"""

from __future__ import annotations

import base64
import difflib
import io
import json
import os
import re
import subprocess
import sys
import time
import threading
from typing import Iterable

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    JavascriptException,
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from PIL import Image

try:
    import google.generativeai as genai
except ImportError:
    if getattr(sys, "frozen", False):
        raise
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--upgrade",
         "google-generativeai", "--break-system-packages", "-q"]
    )
    import google.generativeai as genai


# =====================================================================
# CONFIGURATION
# =====================================================================
# Version du bot (comparée à version.txt sur GitHub pour la mise à jour auto)
BOT_VERSION = "2026.10.26.2"

# Clé Gemini : fournie par le launcher (« Clé fournie » = cle_fournie.py,
# fichier PRIVÉ non publié sur GitHub ; ou la clé perso de l'utilisateur).
API_KEYS = [
    "",
]
try:
    from cle_fournie import CLE_FOURNIE as _CF
    API_KEYS[0] = _CF
except Exception:  # noqa: BLE001
    pass
MODEL_NAME      = "gemini-2.5-flash"
# Modèle MOINS CHER utilisé UNIQUEMENT pour l'arbitrage (quand la passe A
# et la passe B divergent). ~3× moins cher en entrée et ~6× en sortie que
# Flash, et il gère la VISION → l'arbitre garde l'accès à la capture
# d'écran ET aux textes mémorisés. Le reste du bot (résolution principale,
# passe 2, vérifications) reste sur MODEL_NAME. Après chaque appel
# d'arbitrage, _call_gemini repasse automatiquement sur MODEL_NAME.
MODEL_ARBITRAGE = "gemini-2.5-flash-lite"
# Modèles pour les VÉRIFICATIONS (relectures de recoupement).
#  - text_input : relecture des MOTS À TAPER. Erreurs subtiles de
#    forme/complétude (ex: "clear" au lieu de "clear the air") que le
#    modèle léger laisse passer → on garde Flash (le modèle fort) ici.
#  - QCM multi : re-jugement option par option, moins risqué → Flash-Lite
#    suffit (économie conservée).
# Deux constantes séparées pour pouvoir régler chacune indépendamment.
# La résolution (passe 1 + passe 2), les auto-critiques et les retries
# forcés restent sur MODEL_NAME (Flash).
MODEL_VERIF_TEXT = "gemini-2.5-flash"
MODEL_VERIF_QCM  = "gemini-2.5-flash-lite"
# ÉCONOMIE : sur les exercices FACILES (QCM de vocabulaire à réponses
# courtes, reorder court), la PASSE 1 utilise Flash-Lite (≈ 6× moins cher
# en sortie, sans « réflexion »). La PASSE 2 reste TOUJOURS sur Flash et,
# en cas de désaccord, l'arbitrage aussi → jamais de réponse Flash-Lite
# seule. Mettre False pour revenir au tout-Flash.
LITE_FOR_EASY = True
MODEL_EASY = "gemini-2.5-flash-lite"

# Tarifs Gemini (USD par MILLION de tokens) : (entrée, sortie).
# La sortie inclut le "thinking" et coûte bien plus cher que l'entrée.
# Sert UNIQUEMENT à estimer le coût dans les logs (mesure).
PRICING = {
    "gemini-2.5-flash":      (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
}
USD_TO_EUR = 0.92            # approximatif, pour l'affichage € des logs

DEBUG_PORT      = 9222
REFRESH_INTERVAL = 120       # secondes
RPM_PER_KEY     = 60         # appels IA max / min (palier 2 Gemini : limite
#                             réelle 2000 RPM, on reste très en dessous ;
#                             supprime les attentes inutiles entre appels)
MAX_NONE_RUNS   = 5          # échecs consécutifs avant déblocage forcé
CAPTURE_TIMEOUT = 15         # timeout CDP full-page screenshot
# VISION CONDITIONNELLE. "auto" = n'envoyer la capture d'écran à Gemini
# (passe 1, chemin générique) QUE si l'exercice a une vraie image de
# contenu (src .../media/…) ou des images-réponses. Les exos 100 % texte
# (QCM de transcript, reorder, drag de mots) répondent depuis le stock +
# les textes déjà fournis → ~4400 tokens d'entrée économisés par passe 1.
# Mettre "always" pour toujours envoyer la capture (retour arrière
# immédiat si un doute sur la précision apparaît).
VISION_MODE = "auto"
JS_ASYNC_TIMEOUT = 10        # timeout execute_async_script (s)

# Portillon de contrôle PAUSE / REPRISE, piloté par l'interface graphique.
#  • Mode terminal (lancement direct) : reste OUVERT (set) → le bot tourne
#    normalement, le portillon ne bloque jamais. Comportement inchangé.
#  • Mode interface (.exe) : l'application le FERME (clear) au démarrage,
#    pour laisser l'utilisateur se connecter et choisir son exercice, puis
#    l'OUVRE via le bouton « Commencer / Reprendre ». La boucle run() attend
#    ce portillon au début de chaque cycle.
RUN_GATE = threading.Event()
RUN_GATE.set()

# ── PRONONCIATION : reconnaissance vocale simulée ─────────────────────
# GlobalExam utilise la reconnaissance vocale intégrée à Chrome (Web
# Speech API). On la remplace dans la page par une version factice qui
# « entend » directement la phrase attendue (posée par le bot dans
# window.__botSpeechText). Aucun micro, aucun appel IA. Si ça échoue, le
# bot retombe sur « I can't talk right now ». Mettre False pour couper.
SPEAK_MODE = True

JS_FAKE_SPEECH = r"""
(function () {
  if (window.__botFakeSR) return;
  window.__botFakeSR = true;
  class FakeSR extends EventTarget {
    constructor() {
      super();
      this.lang = 'en-US'; this.continuous = false;
      this.interimResults = false; this.maxAlternatives = 1;
      this.grammars = null;
      ['onstart','onend','onresult','onerror','onaudiostart','onaudioend',
       'onsoundstart','onsoundend','onspeechstart','onspeechend',
       'onnomatch'].forEach(k => { this[k] = null; });
      this._running = false; this._gave = false; this._timers = [];
      window.__botSRLast = this;
    }
    _emit(type, extra) {
      const ev = new Event(type);
      if (extra) for (const k in extra)
        Object.defineProperty(ev, k, {value: extra[k]});
      try { const h = this['on' + type];
            if (typeof h === 'function') h.call(this, ev); }
      catch (e) { console.error(e); }
      this.dispatchEvent(ev);
    }
    _later(ms, fn) { this._timers.push(setTimeout(fn, ms)); }
    _result(text, isFinal) {
      const alt = {transcript: text, confidence: 0.96};
      const res = [alt]; res.isFinal = isFinal; res.item = i => res[i];
      const list = [res]; list.item = i => list[i];
      this._emit('result', {results: list, resultIndex: 0});
      if (isFinal) { this._gave = true;
        window.__botSRResults = (window.__botSRResults || 0) + 1; }
    }
    _finish() {
      if (!this._running) return;
      this._running = false; this._emit('end');
    }
    start() {
      if (this._running) return;
      this._running = true; this._gave = false;
      window.__botSRStarts = (window.__botSRStarts || 0) + 1;
      const text = (window.__botSpeechText || '').trim();
      this._later(150, () => { this._emit('start'); this._emit('audiostart');
                               this._emit('soundstart');
                               this._emit('speechstart'); });
      if (!text) {
        this._later(1500, () => { this._emit('error',
            {error: 'no-speech', message: ''}); this._finish(); });
        return;
      }
      const dur = Math.min(6000, 900 + text.length * 55);
      if (this.interimResults) {
        const w = text.split(' ');
        this._later(Math.round(dur / 2), () =>
          this._result(w.slice(0, Math.ceil(w.length / 2)).join(' '), false));
      }
      this._later(dur, () => {
        this._emit('speechend'); this._emit('soundend');
        this._emit('audioend'); this._result(text, true);
        this._later(250, () => this._finish());
      });
    }
    stop() {
      this._timers.forEach(clearTimeout); this._timers = [];
      if (this._running) {
        const t = (window.__botSpeechText || '').trim();
        if (t && !this._gave) this._result(t, true);
        this._finish();
      }
    }
    abort() {
      this._timers.forEach(clearTimeout); this._timers = [];
      this._finish();
    }
  }
  window.SpeechRecognition = FakeSR;
  window.webkitSpeechRecognition = FakeSR;
  // MICRO FACTICE : GlobalExam ENREGISTRE la voix (MediaRecorder). Quand
  // le bot a préparé une voix (window.__botVoiceB64 = l'audio natif de
  // l'exercice, en base64), on renvoie un flux micro qui JOUE cet audio.
  // Sinon : vrai micro (comportement normal).
  try {
    // Flux micro factice PARTAGÉ : le site peut demander le micro dès le
    // chargement de la page (avant que le bot ait préparé la voix) puis
    // réutiliser ce flux. La voix est jouée dedans plus tard via
    // window.__botPlayVoice().
    window.__botMicStream = function () {
      if (!window.__botDst) {
        const Ctx = window.AudioContext || window.webkitAudioContext;
        window.__botCtx = new Ctx();
        window.__botDst = window.__botCtx.createMediaStreamDestination();
      }
      return window.__botDst.stream;
    };
    window.__botPlayVoice = async function () {
      const b64 = window.__botVoiceB64;
      if (!b64) return 0;
      window.__botMicStream();
      const ctx = window.__botCtx;
      window.__botCtxState = ctx.state;
      if (ctx.state === 'suspended') {
        // resume() peut rester bloqué sans geste utilisateur → 800 ms max
        try { await Promise.race([ctx.resume(),
                new Promise(r => setTimeout(r, 800))]); } catch (e) {}
        window.__botCtxState = ctx.state;
      }
      const bin = atob(b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      const audio = await ctx.decodeAudioData(bytes.buffer);
      const src = ctx.createBufferSource();
      src.buffer = audio; src.connect(window.__botDst);
      src.start(ctx.currentTime + 0.4);
      window.__botVoiceDur = audio.duration;
      window.__botGumCalls = (window.__botGumCalls || 0) + 1;
      return audio.duration;
    };
    const md = navigator.mediaDevices;
    if (md && md.getUserMedia && !md.__botPatched) {
      md.__botPatched = true;
      const real = md.getUserMedia.bind(md);
      md.getUserMedia = function (c) {
        if (c && c.audio && !c.video) {
          window.__botGumEntered = (window.__botGumEntered || 0) + 1;
          return (async () => {
            const stream = window.__botMicStream();
            if (window.__botVoiceB64) await window.__botPlayVoice();
            return stream;
          })().catch(e => { window.__botGumErr = String(e && (e.name + ': '
                              + e.message)); throw e; });
        }
        return real(c);
      };
    }
  } catch (e) {}
})();
"""

# ── ARRÊT PROPRE (minuteur de session) ────────────────────────────────
# Mis à True par le launcher quand le temps de travail est écoulé (ou via
# « Terminer après cet exo »). Le bot finit l'exercice en cours, puis
# run() rend la main. Délai max pour finir l'exo : STOP_GRACE_S.
STOP_REQUESTED = threading.Event()
STOP_GRACE_S = 300

# ── RAPPORT D'ERREUR AUTOMATIQUE ──────────────────────────────────────
# Quand la correction indique une réponse FAUSSE, le bot enregistre une
# capture + le contexte dans le dossier « erreurs/ » puis se met en
# pause, pour qu'on puisse analyser et corriger le bot.
PAUSE_ON_ERROR = True
# Mode debug (réglé par le launcher) : True = pause + notification à chaque
# erreur ; False = l'erreur est enregistrée dans erreurs/ et le bot continue.
DEBUG_PAUSE = True
# Réglage de connexion Gemini qui a fonctionné (clé AIza / AQ., gRPC/REST)
_GENAI_CONF = None
# Clé « AQ.… » acceptée seulement en en-tête x-goog-api-key : le bot parle
# alors DIRECTEMENT à l'API REST (sans le SDK, qui la transmet mal).
_REST_KEY = None
_AQ_CHAIN: list = []


class _RestGemini:
    """Remplaçant minimal de genai.GenerativeModel (generate_content →
    .text / .usage_metadata) qui appelle l'API REST avec l'en-tête
    x-goog-api-key, seule façon acceptée pour les nouvelles clés AQ."""

    URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"

    def __init__(self, model_name: str, key: str):
        self.model_name = model_name
        self.key = key

    def generate_content(self, content, generation_config=None,
                         request_options=None, **_kw):
        import urllib.request
        import urllib.error
        parts = []
        for c in (content if isinstance(content, (list, tuple)) else [content]):
            if isinstance(c, str):
                parts.append({"text": c})
            elif c is not None:              # image PIL
                bio = io.BytesIO()
                img = c.convert("RGB") if getattr(c, "mode", "") != "RGB" else c
                img.save(bio, format="JPEG", quality=90)
                parts.append({"inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(bio.getvalue()).decode()}})
        cfg = {}
        for k_src, k_dst in (("temperature", "temperature"),
                             ("max_output_tokens", "maxOutputTokens")):
            v = getattr(generation_config, k_src, None)
            if v is not None:
                cfg[k_dst] = v
        body = {"contents": [{"role": "user", "parts": parts}]}
        if cfg:
            body["generationConfig"] = cfg
        timeout = (request_options or {}).get("timeout", 90)
        req = urllib.request.Request(
            self.URL.format(self.model_name),
            data=json.dumps(body).encode("utf-8"),
            headers={"x-goog-api-key": self.key,
                     "Content-Type": "application/json"},
            method="POST")
        try:
            data = json.loads(urllib.request.urlopen(
                req, timeout=timeout).read().decode("utf-8"))
        except urllib.error.HTTPError as he:
            raise RuntimeError(f"{he.code} {he.read().decode('utf-8', 'ignore')[:300]}")
        text = ""
        for cand in data.get("candidates") or []:
            for prt in (cand.get("content") or {}).get("parts") or []:
                if not prt.get("thought"):
                    text += prt.get("text", "")
            break
        um = data.get("usageMetadata") or {}

        class _U:
            prompt_token_count = um.get("promptTokenCount", 0)
            candidates_token_count = um.get("candidatesTokenCount", 0)
            thoughts_token_count = um.get("thoughtsTokenCount", 0)
            total_token_count = um.get("totalTokenCount", 0)

        class _R:
            pass
        r = _R()
        r.text = text
        r.usage_metadata = _U()
        return r


def _pick_available_models(key: str) -> None:
    """Clés « AQ. » (souvent sur le NIVEAU GRATUIT) : gemini-2.5 n'est plus
    ouvert aux nouveaux comptes, et chaque modèle a son propre quota/jour
    (ex. Flash-Lite 500/j et 15/min, Flash 20/j et 5/min). On construit une
    CHAÎNE de modèles : d'abord les Flash-Lite (gros quota, « un cran en
    dessous »), puis les Flash. Quand le quota du jour d'un modèle est
    épuisé, le bot passe au suivant (voir _next_aq_model)."""
    global MODEL_NAME, MODEL_ARBITRAGE, MODEL_VERIF_TEXT, MODEL_VERIF_QCM
    global MODEL_EASY, RPM_PER_KEY, _AQ_CHAIN
    import urllib.request
    try:
        data = json.loads(urllib.request.urlopen(urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models"
            "?pageSize=1000", headers={"x-goog-api-key": key}),
            timeout=20).read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"   ⚠️ Liste des modèles indisponible : {str(e)[:80]}")
        return
    names = [m["name"].split("/", 1)[-1] for m in data.get("models", [])
             if "generateContent" in (m.get("supportedGenerationMethods")
                                      or [])]

    def ranked(lite: bool) -> list:
        pat = re.compile(r"^gemini-(\d+(?:\.\d+)?)-flash"
                         + (r"-lite" if lite else r"(?!-lite)")
                         + r"(-latest|-preview[\w-]*|-\d+)?$")
        found = []
        for n in names:
            m = pat.match(n)
            if not m or any(x in n for x in ("image", "tts", "live",
                                             "audio", "2.5", "2.0")):
                continue
            stable = 0 if (m.group(2) or "").startswith("-preview") else 1
            plain = 1 if not m.group(2) else 0
            found.append((stable, float(m.group(1)), plain, n))
        return [f[3] for f in sorted(found, reverse=True)]

    chain = ranked(True) + ranked(False)
    # Un seul appel de test (sur le 1er modèle utilisable) : chaque test
    # consomme du quota du jour.
    refused = []
    while chain:
        try:
            _RestGemini(chain[0], key).generate_content(
                ["Reply OK"], request_options={"timeout": 30})
            break
        except Exception as e:  # noqa: BLE001
            txt = " ".join(str(e).split())
            refused.append(txt[:3])
            print(f"   ⚠️ {chain[0]} refusé : {txt[:90]}")
            if txt.startswith("402"):
                chain = []
                break
            chain.pop(0)
    if not chain:
        if "402" in refused:
            print("   💳 Google refuse : « crédits prépayés épuisés » sur le "
                  "projet de cette clé (projet FACTURÉ). Pour le gratuit, "
                  "crée la clé dans un projet SANS facturation, ou recharge "
                  "des crédits, ou utilise « Clé fournie ».")
        else:
            print("   ❌ Aucun modèle Gemini utilisable avec cette clé.")
        raise RuntimeError("aucun modèle utilisable")
    _AQ_CHAIN = chain
    _use_aq_model(chain[0])
    RPM_PER_KEY = 14                  # quota gratuit Flash-Lite : 15/min
    print(f"   🪶 Mode économe (clé AQ.) : {chain[0]} pour tout ; relais si "
          f"quota du jour épuisé : {', '.join(chain[1:]) or 'aucun'}")


def _use_aq_model(name: str) -> None:
    global MODEL_NAME, MODEL_ARBITRAGE, MODEL_VERIF_TEXT, MODEL_VERIF_QCM
    global MODEL_EASY
    lite = "lite" in name
    PRICING.setdefault(name, (0.10, 0.40) if lite else (0.30, 2.50))
    MODEL_NAME = MODEL_ARBITRAGE = MODEL_VERIF_TEXT = name
    MODEL_VERIF_QCM = MODEL_EASY = name


def _next_aq_model() -> str | None:
    """Quota du jour épuisé sur le modèle courant → modèle suivant."""
    if MODEL_NAME in _AQ_CHAIN:
        i = _AQ_CHAIN.index(MODEL_NAME)
        if i + 1 < len(_AQ_CHAIN):
            _use_aq_model(_AQ_CHAIN[i + 1])
            if "lite" not in MODEL_NAME:
                globals()["RPM_PER_KEY"] = 4      # Flash gratuit : 5/min
            return MODEL_NAME
    return None


def _make_model(name: str):
    """GenerativeModel du SDK, ou client REST direct pour les clés AQ."""
    if _REST_KEY:
        return _RestGemini(name, _REST_KEY)
    return genai.GenerativeModel(name)
# Appelé par le launcher (s'il est là) lors d'une pause automatique,
# pour mettre à jour le bouton « Reprendre ». Signature : f(dossier: str)
ON_AUTO_PAUSE = None


def _project_dir() -> str:
    """Dossier du projet : à côté du bot (ou de l'exe ; si l'exe est dans
    dist/, on remonte d'un cran pour tout garder dans le dossier projet)."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
        if os.path.basename(base).lower() == "dist":
            base = os.path.dirname(base)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return base


def _errors_dir() -> str:
    return os.path.join(_project_dir(), "erreurs")


def _prune_old_reports(keep_errors: int = 40, keep_diag: int = 12) -> None:
    """Ménage au démarrage : ne garde que les rapports d'erreur et les
    diagnostics les plus récents (les plus anciens ne servent plus)."""
    import shutil
    for sub, keep in (("erreurs", keep_errors), ("diagnostics", keep_diag)):
        d = os.path.join(_project_dir(), sub)
        try:
            items = sorted(e for e in os.listdir(d)
                           if os.path.isdir(os.path.join(d, e)))
        except OSError:
            continue
        for old in items[:-keep] if len(items) > keep else []:
            shutil.rmtree(os.path.join(d, old), ignore_errors=True)


# ── NOTIFICATION IPHONE (ntfy) ────────────────────────────────────────
# Active SEULEMENT si le fichier « notification_iphone.txt » existe dans
# le dossier du projet et contient un nom de canal ntfy. Un collègue qui
# reçoit juste l'exe n'a pas ce fichier → aucune notification envoyée.
NTFY_FILE = "notification_iphone.txt"


def _ntfy_topic() -> str | None:
    try:
        with open(os.path.join(_project_dir(), NTFY_FILE),
                  encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except OSError:
        pass
    return None


def notify_phone(message: str, title: str = "Bot GlobalExam",
                 priority: str = "high", tags: str = "warning") -> None:
    """Envoie une notification push (app ntfy) sans bloquer le bot."""
    topic = _ntfy_topic()
    if not topic:
        return

    def _send() -> None:
        try:
            import urllib.request
            req = urllib.request.Request(
                f"https://ntfy.sh/{topic}",
                data=message.encode("utf-8"),
                method="POST",
                headers={
                    "Title": title.encode("ascii", "ignore").decode(),
                    "Priority": priority,
                    "Tags": tags,
                },
            )
            urllib.request.urlopen(req, timeout=8).read()
        except Exception as e:  # noqa: BLE001
            print(f"   ⚠️ Notification iPhone non envoyée : {str(e)[:60]}")

    threading.Thread(target=_send, daemon=True).start()


class _LogTee:
    """Duplique stdout et garde les ~400 dernières lignes en mémoire
    (pour joindre le journal récent au rapport d'erreur)."""
    def __init__(self, inner, max_lines: int = 400):
        from collections import deque
        self.inner = inner
        self.lines = deque(maxlen=max_lines)
        self._buf = ""

    def write(self, text: str) -> int:
        try:
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self.lines.append(line)
        except Exception:
            pass
        return self.inner.write(text)

    def flush(self) -> None:
        try:
            self.inner.flush()
        except Exception:
            pass

    def recent(self) -> str:
        return "\n".join(list(self.lines) + ([self._buf] if self._buf else []))

# Langues : code → nom humain (français)
LANG_NAMES: dict[str, str] = {
    "es": "espagnol",  "fr": "français", "en": "anglais",
    "de": "allemand",  "pt": "portugais","it": "italien",
}

# Mots à TOUJOURS refuser (clic / drag / validate)
SKIP_WORDS      = {"skip", "passer", "sauter"}
# Liste COMPLÈTE (utilisée pour détecter les étiquettes de boutons).
VALIDATE_WORDS  = {
    "validate", "valider", "submit", "check", "vérifier",
    "finish", "terminer", "done", "terminé",
    "verify",
}
# Sous-ensemble STRICT : ces mots ne font JAMAIS partie d'une phrase
# d'exercice, seulement d'un bouton de validation. Utilisé pour filtrer
# les actions proposées par l'IA sans risque de supprimer un mot
# légitime. Les mots comme "check", "finish", "done", "verify" sont
# exclus ici car ils peuvent apparaître dans un reorder_sentence
# ("I would prefer to CHECK the car", "I'm DONE with homework", etc.).
VALIDATE_BUTTON_ONLY = {
    "validate", "valider", "submit", "vérifier", "terminer", "terminé",
}
NAV_WORDS       = {"next", "suivant", "continue", "continuer"}

# Boutons réflexes (navigation automatique) — Skip ABSENT volontairement
REFLEX_BUTTONS = [
    "Move on to the next", "Next activity",
    "Finish", "Terminer", "See results", "Consulter les résultats",
    "Next", "Suivant", "Continue", "Continuer",
]


# =====================================================================
# SCRIPTS JS (raw strings pour éviter les SyntaxWarning sur \s \d)
# =====================================================================

JS_HEAVY_DRAG = r"""
async function dragAndDrop(source, target) {
    const wait = ms => new Promise(r => setTimeout(r, ms));
    const srcRect = source.getBoundingClientRect();
    const tgtRect = target.getBoundingClientRect();
    const srcX = srcRect.left + srcRect.width / 2;
    const srcY = srcRect.top + srcRect.height / 2;
    const tgtX = tgtRect.left + tgtRect.width * 0.60;
    const tgtY = tgtRect.top + tgtRect.height * 0.60;
    const dataTransfer = new DataTransfer();
    function trigger(elem, type, x, y, buttons) {
        if (buttons === undefined) buttons = 1;
        elem.dispatchEvent(new MouseEvent(type, {
            view: window, bubbles: true, cancelable: true,
            clientX: x, clientY: y, button: 0, buttons: buttons,
            dataTransfer: dataTransfer
        }));
    }
    trigger(source, 'pointerdown', srcX, srcY);
    trigger(source, 'mousedown',   srcX, srcY);
    trigger(source, 'dragstart',   srcX, srcY);
    await wait(50);
    trigger(source, 'mousemove', srcX + 10, srcY + 10);
    await wait(50);
    trigger(target, 'dragenter', tgtX, tgtY);
    trigger(target, 'dragover',  tgtX, tgtY);
    trigger(target, 'mousemove', tgtX, tgtY);
    await wait(80);
    trigger(target, 'drop',    tgtX, tgtY);
    trigger(source, 'dragend', tgtX, tgtY);
    trigger(target, 'mouseup', tgtX, tgtY);
    trigger(target, 'pointerup', tgtX, tgtY);
}
var callback = arguments[arguments.length - 1];
dragAndDrop(arguments[0], arguments[1]).then(callback).catch(e => callback(String(e)));
"""

JS_POINTER_DRAG = r"""
async function pointerDrag(source, target) {
    const wait = ms => new Promise(r => setTimeout(r, ms));
    const srcRect = source.getBoundingClientRect();
    const tgtRect = target.getBoundingClientRect();
    const srcX = srcRect.left + srcRect.width / 2;
    const srcY = srcRect.top + srcRect.height / 2;
    const tgtX = tgtRect.left + tgtRect.width / 2;
    const tgtY = tgtRect.top + tgtRect.height / 2;
    function pe(type, x, y, pressure) {
        return new PointerEvent(type, {
            bubbles: true, cancelable: true,
            clientX: x, clientY: y,
            pointerId: 1, pointerType: 'mouse',
            isPrimary: true, pressure: pressure || 0,
            buttons: type === 'pointerup' ? 0 : 1
        });
    }
    source.dispatchEvent(pe('pointerover',  srcX, srcY, 0.5));
    source.dispatchEvent(pe('pointerenter', srcX, srcY, 0.5));
    source.dispatchEvent(pe('pointerdown',  srcX, srcY, 0.5));
    await wait(80);
    const steps = 10;
    for (let i = 1; i <= steps; i++) {
        const x = srcX + (tgtX - srcX) * i / steps;
        const y = srcY + (tgtY - srcY) * i / steps;
        document.dispatchEvent(pe('pointermove', x, y, 0.5));
        await wait(20);
    }
    target.dispatchEvent(pe('pointerover',  tgtX, tgtY, 0.5));
    target.dispatchEvent(pe('pointerenter', tgtX, tgtY, 0.5));
    await wait(60);
    target.dispatchEvent(pe('pointerup',    tgtX, tgtY, 0));
    target.dispatchEvent(pe('pointerleave', tgtX, tgtY, 0));
    source.dispatchEvent(pe('pointerleave', srcX, srcY, 0));
}
var callback = arguments[arguments.length - 1];
pointerDrag(arguments[0], arguments[1]).then(callback).catch(e => callback(String(e)));
"""

# Drag à COORDONNÉES EXPLICITES (pas au centre de target).
# Utilisé en mode accumulateur pour déposer chaque mot JUSTE À DROITE
# du dernier mot déjà déposé, et éviter d'écraser les mots du stock.
# arguments[0] = source (élément à glisser)
# arguments[1] = target (élément récepteur, pour les events dispatch)
# arguments[2] = tgtX (coordonnée x explicite du drop)
# arguments[3] = tgtY (coordonnée y explicite du drop)
JS_DRAG_AT_POSITION = r"""
async function dragAt(source, target, tgtX, tgtY) {
    const wait = ms => new Promise(r => setTimeout(r, ms));
    const srcRect = source.getBoundingClientRect();
    const srcX = srcRect.left + srcRect.width / 2;
    const srcY = srcRect.top + srcRect.height / 2;
    const dataTransfer = new DataTransfer();
    function trigger(elem, type, x, y, buttons) {
        if (buttons === undefined) buttons = 1;
        elem.dispatchEvent(new MouseEvent(type, {
            view: window, bubbles: true, cancelable: true,
            clientX: x, clientY: y, button: 0, buttons: buttons,
            dataTransfer: dataTransfer
        }));
    }
    // Pointer events + MouseEvents (double voie pour max compatibilité)
    function pe(type, x, y, pressure) {
        return new PointerEvent(type, {
            bubbles: true, cancelable: true,
            clientX: x, clientY: y,
            pointerId: 1, pointerType: 'mouse',
            isPrimary: true, pressure: pressure || 0,
            buttons: type === 'pointerup' ? 0 : 1
        });
    }
    source.dispatchEvent(pe('pointerdown', srcX, srcY, 0.5));
    trigger(source, 'pointerdown', srcX, srcY);
    trigger(source, 'mousedown',   srcX, srcY);
    trigger(source, 'dragstart',   srcX, srcY);
    await wait(50);
    // Mouvement progressif vers la cible X/Y demandée
    const steps = 10;
    for (let i = 1; i <= steps; i++) {
        const x = srcX + (tgtX - srcX) * i / steps;
        const y = srcY + (tgtY - srcY) * i / steps;
        document.dispatchEvent(pe('pointermove', x, y, 0.5));
        trigger(target, 'mousemove', x, y);
        await wait(15);
    }
    // Élément sous la cible (peut être target ou un enfant)
    const elAt = document.elementFromPoint(tgtX, tgtY) || target;
    elAt.dispatchEvent(pe('pointerover', tgtX, tgtY, 0.5));
    elAt.dispatchEvent(pe('pointerenter', tgtX, tgtY, 0.5));
    trigger(elAt, 'dragenter', tgtX, tgtY);
    trigger(elAt, 'dragover',  tgtX, tgtY);
    trigger(elAt, 'mousemove', tgtX, tgtY);
    await wait(80);
    trigger(elAt, 'drop',    tgtX, tgtY);
    trigger(source, 'dragend', tgtX, tgtY);
    elAt.dispatchEvent(pe('pointerup', tgtX, tgtY, 0));
    trigger(elAt, 'mouseup', tgtX, tgtY);
}
var callback = arguments[arguments.length - 1];
dragAt(arguments[0], arguments[1], arguments[2], arguments[3])
    .then(function() { callback(null); })
    .catch(function(e) { callback(String(e)); });
"""

# Scan JS des conteneurs scrollables horizontaux
JS_SCROLL_HORIZONTAL = r"""
var all = document.querySelectorAll('*');
for (var i = 0; i < all.length; i++) {
    var el = all[i];
    var s = window.getComputedStyle(el);
    var ox = s.overflowX;
    if ((ox === 'auto' || ox === 'scroll') &&
        el.scrollWidth > el.clientWidth + 10) {
        el.scrollLeft = el.scrollWidth;
    }
}
"""

# Scan JS des éléments draggables (stock à l'écran). Robuste :
#  - parcourt plusieurs sélecteurs de draggables (framework-agnostic)
#  - déduplique par RÉFÉRENCE (WeakSet) pour éviter que le même nœud du
#    DOM qui matche plusieurs sélecteurs soit ajouté deux fois, MAIS on
#    ne déduplique PAS par texte pour les caractères uniques (sinon
#    "TICKET" perd son 2e T).
#  - PAS de filtre de viewport : certains sites gardent le stock hors
#    du viewport visible (wrapping multi-lignes, flex, etc.) et seraient
#    sinon perdus. Seuls display/visibility/opacity filtrent.
JS_SCAN_DRAGGABLES = r"""
var results = [];
var seenEls = new WeakSet();
var seenMulti = {};          // dédup textes multi-caractères seulement
var sels = [
    '[data-sortable-item]',   // exos « choose the correct spelling » (SortableJS)
    '[draggable="true"]',
    '.draggable-item',
    '[class*="draggable"]',
    '[class*="sortable-item"]',
    '[class*="ordering-item"]',
    '[class*="drag-item"]',
    '[class*="reorder"]',
    '[class*="word-item"]',
    '[class*="word-card"]',
    '[class*="word-tile"]',
    '[class*="token"]',
    '.answer-option'
];
for (var s = 0; s < sels.length; s++) {
    var els;
    try { els = document.querySelectorAll(sels[s]); }
    catch (e) { continue; }
    for (var i = 0; i < els.length; i++) {
        var el = els[i];
        if (seenEls.has(el)) continue;
        var rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;
        var style = window.getComputedStyle(el);
        if (style.display === 'none' ||
            style.visibility === 'hidden' ||
            parseFloat(style.opacity) < 0.1) continue;
        var txt = (el.textContent || '').trim();
        txt = txt.replace(/^[0-9]+\s+/, '').trim();
        if (!txt || txt.length > 500) continue;
        // Dédup UNIQUEMENT pour les textes multi-caractères
        // (les lettres identiques d'un anagramme doivent passer toutes)
        // Tuiles du site (id propre) : deux tuiles « it » sont DEUX tuiles
        // → pas de dédup par texte pour elles (bug : « it » perdu).
        var hasId = el.hasAttribute && (el.hasAttribute('data-draggable-item-id')
                     || el.hasAttribute('data-sortable-item'));
        if (txt.length > 1 && seenMulti[txt] && !hasId) continue;
        var inResult = false;
        var p = el;
        while (p) {
            var cls = (p.className || '').toString().toLowerCase();
            if (cls.indexOf('result') >= 0 ||
                cls.indexOf('feedback') >= 0 ||
                cls.indexOf('correction') >= 0 ||
                cls.indexOf('no-worries') >= 0 ||
                (cls.indexOf('answer-container') >= 0 && p !== el)) {
                inResult = true; break;
            }
            p = p.parentElement;
        }
        if (inResult) continue;
        seenEls.add(el);
        if (txt.length > 1) seenMulti[txt] = true;
        results.push(txt);
    }
}
return results;
"""

# Scan par BADGES NUMÉROTÉS : les exercices "Organize the words"
# numérotent chaque mot (1, 2, 3…) avec un petit badge visuel.
# Ce scan récupère, pour chaque badge détecté, le TEXTE du mot
# associé (via proximité DOM ou voisinage géométrique).
#
# Ce scan contourne les problèmes de sélecteurs CSS spécifiques :
# on s'appuie sur la structure VISUELLE (badge à côté d'un mot)
# plutôt que sur une classe précise. Fiable même si le site change
# ses conventions de nommage.
#
# Retourne : liste de {n: <numero>, text: <mot>, x: <px>, y: <px>}
JS_SCAN_BADGED_WORDS = r"""
var results = [];
var all = document.querySelectorAll('*');
var badges = [];

// Étape 1 : identifier les BADGES (leaf element, texte = chiffre)
for (var i = 0; i < all.length; i++) {
    var el = all[i];
    if (el.children && el.children.length > 0) continue;
    var txt = (el.textContent || '').trim();
    if (!/^\d{1,2}$/.test(txt)) continue;
    var n = parseInt(txt, 10);
    if (isNaN(n) || n < 1 || n > 30) continue;
    var rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    if (rect.width > 60 || rect.height > 60) continue;
    var style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    // Pas à l'intérieur d'une zone de résultat
    var inResult = false;
    var p = el;
    while (p) {
        var cls = (p.className || '').toString().toLowerCase();
        if (cls.indexOf('result') >= 0 ||
            cls.indexOf('feedback') >= 0 ||
            cls.indexOf('correction') >= 0) {
            inResult = true; break;
        }
        p = p.parentElement;
    }
    if (inResult) continue;
    badges.push({n: n, el: el, rect: rect});
}

// Étape 2 : pour CHAQUE badge, trouver le mot associé
// Stratégie en cascade :
//   (a) Sibling immédiat (frère suivant) qui contient du texte
//   (b) Cousin : remonter jusqu'au conteneur commun, chercher texte
//   (c) Voisin géométrique : élément à droite avec texte et rect proche
function getTextFromNode(node) {
    if (!node) return '';
    var t = (node.textContent || '').trim();
    // Rejette les autres badges purs (juste un chiffre)
    if (/^\d{1,2}$/.test(t)) return '';
    if (t.length > 100) return '';
    // Retire les éventuels numéros en tête
    t = t.replace(/^\d+\s+/, '').trim();
    return t;
}

for (var b = 0; b < badges.length; b++) {
    var badge = badges[b];
    var text = '';

    // (a) Sibling immédiat
    var sib = badge.el.nextElementSibling;
    while (sib && !text) {
        text = getTextFromNode(sib);
        sib = sib.nextElementSibling;
    }

    // (b) Remonter au parent et chercher texte dans les frères du parent
    if (!text) {
        var container = badge.el.parentElement;
        for (var up = 0; up < 3 && container && !text; up++) {
            // Scan texte de tous les descendants SAUF le badge lui-même
            var walker = document.createTreeWalker(
                container, NodeFilter.SHOW_ELEMENT, null
            );
            var node;
            while ((node = walker.nextNode())) {
                if (node === badge.el) continue;
                if (node.children && node.children.length > 0) continue;
                var t = getTextFromNode(node);
                if (t && t.length > 0) { text = t; break; }
            }
            container = container.parentElement;
        }
    }

    // (c) Voisinage géométrique : élément à DROITE du badge
    if (!text) {
        var bx = badge.rect.left + badge.rect.width;
        var by = badge.rect.top + badge.rect.height / 2;
        var candidates = [];
        for (var k = 0; k < all.length; k++) {
            var el = all[k];
            if (el === badge.el) continue;
            if (el.children && el.children.length > 0) continue;
            var r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) continue;
            // Doit être à droite du badge (même ligne approximativement)
            if (r.left < bx - 5) continue;
            if (r.left > bx + 200) continue;
            if (Math.abs(r.top + r.height / 2 - by) > 30) continue;
            var t = getTextFromNode(el);
            if (!t) continue;
            candidates.push({el: el, t: t, dx: r.left - bx});
        }
        candidates.sort(function(a, b) { return a.dx - b.dx; });
        if (candidates.length > 0) text = candidates[0].t;
    }

    if (text) {
        results.push({
            n: badge.n,
            text: text,
            x: badge.rect.left,
            y: badge.rect.top
        });
    }
}

return results;
"""

# Rétro-compatibilité : version simple qui ne retourne que les numéros
# (utilisée pour détecter incohérence stock/badges)
JS_SCAN_NUMBER_BADGES = r"""
var results = [];
var all = document.querySelectorAll('*');
for (var i = 0; i < all.length; i++) {
    var el = all[i];
    if (el.children && el.children.length > 0) continue;
    var txt = (el.textContent || '').trim();
    if (!/^\d{1,2}$/.test(txt)) continue;
    var n = parseInt(txt, 10);
    if (isNaN(n) || n < 1 || n > 30) continue;
    var rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    if (rect.width > 60 || rect.height > 60) continue;
    var style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    var inResult = false;
    var p = el;
    while (p) {
        var cls = (p.className || '').toString().toLowerCase();
        if (cls.indexOf('result') >= 0 ||
            cls.indexOf('feedback') >= 0 ||
            cls.indexOf('correction') >= 0) {
            inResult = true; break;
        }
        p = p.parentElement;
    }
    if (inResult) continue;
    results.push(n);
}
return results;
"""

# Scan JS des radios / checkboxes via leurs labels
# IMPORTANT : on filtre (a) les inputs invisibles, (b) les labels dans
# une zone de résultat/feedback (résidu de l'exercice précédent).
JS_SCAN_RADIO_LABELS = r"""
var results = [];
var inputs = document.querySelectorAll('input[type="radio"], input[type="checkbox"]');
for (var i = 0; i < inputs.length; i++) {
    var inp = inputs[i];
    if (!inp.id) continue;
    // Filtre : input invisible ou hors-flux (résidu)
    var inpRect = inp.getBoundingClientRect();
    var inpStyle = window.getComputedStyle(inp);
    // Radio/checkbox sont parfois masqués visuellement mais leur LABEL
    // est visible ; on vérifie plutôt via display/visibility explicites.
    if (inpStyle.display === 'none' ||
        inpStyle.visibility === 'hidden') continue;
    var lbl;
    try { lbl = document.querySelector('label[for="' + CSS.escape(inp.id) + '"]'); }
    catch (e) { continue; }
    if (!lbl) continue;
    // Filtre : label lui-même invisible
    var lblStyle = window.getComputedStyle(lbl);
    if (lblStyle.display === 'none' ||
        lblStyle.visibility === 'hidden' ||
        parseFloat(lblStyle.opacity) < 0.1) continue;
    var lblRect = lbl.getBoundingClientRect();
    if (lblRect.width === 0 || lblRect.height === 0) continue;
    // Filtre : label dans une zone de résultat/feedback
    var inResult = false;
    var p = lbl;
    while (p) {
        var cls = (p.className || '').toString().toLowerCase();
        if (cls.indexOf('result') >= 0 ||
            cls.indexOf('feedback') >= 0 ||
            cls.indexOf('correction') >= 0 ||
            cls.indexOf('no-worries') >= 0 ||
            cls.indexOf('user-answer') >= 0 ||
            cls.indexOf('swal') >= 0 ||
            cls.indexOf('toast') >= 0) {
            inResult = true; break;
        }
        p = p.parentElement;
    }
    if (inResult) continue;
    var spans = lbl.querySelectorAll('span');
    var text = '';
    for (var j = 0; j < spans.length; j++) {
        var t = (spans[j].textContent || '').trim();
        if (t && !spans[j].querySelector('span')) { text = t; break; }
    }
    if (!text) text = (lbl.textContent || '').trim();
    text = text.replace(/^\d+\s+/, '').trim();
    if (text && text.length > 0 && text.length < 300) results.push(text);
}
return results;
"""

# Vérifie si un élément est dans une zone "résultat / correction"
JS_IN_RESULT_ZONE = r"""
var el = arguments[0];
var bad = ['result','feedback','correction','score','swal',
           'alert','toast','modal','no-worries','almost'];
while (el) {
    var cls = (el.className || '').toString().toLowerCase();
    for (var i = 0; i < bad.length; i++) {
        if (cls.indexOf(bad[i]) >= 0) return true;
    }
    el = el.parentElement;
}
return false;
"""

# Calcule le point de drop OPTIMAL pour un mode accumulateur.
# Recherche les enfants DÉJÀ placés dans la zone (tokens visibles),
# et renvoie un point situé juste à DROITE du dernier token — sur la
# même ligne, ou au début de la ligne suivante si la zone wrap.
# Si la zone est vide, renvoie le point en haut-gauche de la zone.
#
# arguments[0] = target (la zone accumulatrice)
# Retourne : {x, y, n_children, last_token, tokens_debug}
JS_ACCUMULATOR_DROP_POINT = r"""
var zone = arguments[0];
var zoneRect = zone.getBoundingClientRect();

// Scanner tous les éléments DIRECTEMENT visibles dans la zone qui
// contiennent du texte (tokens déjà déposés). Stratégie : on cherche
// les "leaves textuelles" stricto sensu = nœuds qui contiennent du
// texte non-blanc et dont les enfants sont uniquement inline/vides.
var candidates = [];
var walker = document.createTreeWalker(
    zone, NodeFilter.SHOW_ELEMENT, null
);
var node;
while ((node = walker.nextNode())) {
    var rect = node.getBoundingClientRect();
    if (rect.width < 10 || rect.height < 10) continue;
    // Doit être ENTIÈREMENT dans la zone
    if (rect.left < zoneRect.left - 2) continue;
    if (rect.right > zoneRect.right + 2) continue;
    if (rect.top < zoneRect.top - 2) continue;
    if (rect.bottom > zoneRect.bottom + 2) continue;
    // Trop gros : c'est un conteneur, pas un token
    if (rect.width > zoneRect.width * 0.85) continue;
    if (rect.height > 80) continue;
    // Doit avoir du texte significatif court
    var txt = (node.textContent || '').trim();
    if (!txt || txt.length > 100) continue;
    // Ignorer badges purs (juste un chiffre)
    if (/^\d{1,2}$/.test(txt)) continue;
    // Accepter les nœuds dont tous les children sont des éléments
    // INLINE (span, i, b, em, strong) OU pas d'enfant du tout.
    // On ACCEPTE les wrappers qui ont juste un span de texte dedans.
    var blockChildCount = 0;
    for (var c = 0; c < node.children.length; c++) {
        var ch = node.children[c];
        var chTag = (ch.tagName || '').toLowerCase();
        if (chTag !== 'span' && chTag !== 'i' && chTag !== 'b'
            && chTag !== 'em' && chTag !== 'strong'
            && chTag !== 'p' && chTag !== 'a') {
            blockChildCount++;
        }
    }
    // Plus de 1 child block = probablement un conteneur
    if (blockChildCount > 1) continue;
    candidates.push({
        el: node, rect: rect, txt: txt,
        cx: rect.left + rect.width / 2,
        cy: rect.top + rect.height / 2
    });
}

// Dédupliquer : parmi les imbriqués, garder seulement le plus PETIT
// (la feuille). Un token imbriqué dans un wrapper → on veut la feuille.
var tokens = [];
for (var i = 0; i < candidates.length; i++) {
    var t = candidates[i];
    var hasSmallerChild = false;
    for (var j = 0; j < candidates.length; j++) {
        if (i === j) continue;
        if (t.el.contains(candidates[j].el)
            && candidates[j].rect.width * candidates[j].rect.height
               < t.rect.width * t.rect.height * 0.9) {
            hasSmallerChild = true; break;
        }
    }
    if (!hasSmallerChild) tokens.push(t);
}

// Dédup par texte + proximité (évite doublons si plusieurs éléments
// avec même texte collés)
var seen = {};
var final_tokens = [];
for (var k = 0; k < tokens.length; k++) {
    var t = tokens[k];
    // Clé basée sur texte + position arrondie
    var key = t.txt + '_' + Math.round(t.rect.left / 10)
                    + '_' + Math.round(t.rect.top / 10);
    if (seen[key]) continue;
    seen[key] = true;
    final_tokens.push(t);
}
tokens = final_tokens;

var result = {n_children: tokens.length};
var dbg = [];
for (var d = 0; d < Math.min(tokens.length, 10); d++) {
    dbg.push(tokens[d].txt + '@' + Math.round(tokens[d].rect.left)
             + ',' + Math.round(tokens[d].rect.top));
}
result.tokens_debug = dbg;

if (tokens.length === 0) {
    // Zone vide : point en haut-gauche avec marge
    result.x = zoneRect.left + 20;
    result.y = zoneRect.top + Math.min(zoneRect.height / 2, 50);
    return result;
}

// Trier par ligne (y) puis par colonne (x)
tokens.sort(function(a, b) {
    if (Math.abs(a.rect.top - b.rect.top) > 10) {
        return a.rect.top - b.rect.top;
    }
    return a.rect.left - b.rect.left;
});

// Dernier token placé = dernier dans l'ordre de lecture
var last = tokens[tokens.length - 1];

// Point cible : juste à droite du dernier token, sur la même ligne
var targetX = last.rect.right + 15;
var targetY = last.cy;

// Si ça dépasse le bord droit de la zone, passer à la ligne suivante
if (targetX > zoneRect.right - 30) {
    targetX = zoneRect.left + 20;
    targetY = last.rect.bottom + last.rect.height / 2 + 5;
    // Si ça dépasse aussi le bas, retomber sur le dernier token
    if (targetY > zoneRect.bottom - 10) {
        targetX = last.rect.right + 5;
        targetY = last.cy;
    }
}

result.x = targetX;
result.y = targetY;
result.last_token = last.txt;
return result;
"""


# =====================================================================
# UTILITAIRES PURS (hors classe, testables indépendamment)
# =====================================================================

_CLEAN_PREFIX_NUMBERED = re.compile(
    r'^(click_radio_button|click_button|click_radio|click_checkbox|'
    r'click|select_option|select|drag_drop|drag|drop|choose|'
    r'input|type|fill|radio|checkbox)[_\s]*\d+\s*$',
    re.IGNORECASE,
)
_CLEAN_PREFIX_ACTION = re.compile(
    r'^(click_radio_button|click_button|click_radio|click_checkbox|'
    r'click|select_option|select|drag_drop|drag|drop|choose|'
    r'choisir|cliquer|mettre|cocher|sélectionner|'
    r'input|type|fill|radio|checkbox)(?:\s*[:_]\s*|\s+-\s+)',
    re.IGNORECASE,
)
# Clés (norm_key) des éléments du stock de l'exercice en cours. Mis à
# jour à chaque analyse. Si la réponse de l'IA EST un élément du stock
# (ex. une option 'Select the best one'), clean_ai n'y touche pas.
_CURRENT_STOCK_KEYS: set = set()


def set_current_stock(options) -> None:
    global _CURRENT_STOCK_KEYS
    _CURRENT_STOCK_KEYS = {norm_key(str(o)) for o in (options or []) if o}
_CLEAN_PREFIX_WORD = re.compile(
    r'^(Choisir|Cliquer|Mettre|Select|Click|Choose)\s+',
    re.IGNORECASE,
)
_CLEAN_LEADING_BRACKET = re.compile(r'^[\(\[][A-Z0-9]+[\)\]]\s*')
_CLEAN_LEADING_NUMBER  = re.compile(r'^\d+\s+')


def clean_text(text: str) -> str:
    """Supprime les préfixes genre (A), [1], etc."""
    if not text:
        return ""
    return _CLEAN_LEADING_BRACKET.sub('', text).strip()


def clean_ai(text: str) -> str:
    """
    Nettoie une chaîne produite par l'IA : supprime préfixes d'action,
    guillemets EN BORDURE uniquement (pas les apostrophes internes).

    CRITIQUE : les apostrophes internes doivent être PRÉSERVÉES
    (ex: "hasn't traveled" doit rester "hasn't traveled" et NON
    "hasnt traveled"). Sinon le matching DOM échoue.
    """
    if not text:
        return ""
    text = str(text)
    # Tuile de PONCTUATION seule (« , » « : » « ; » …) : c'est une vraie
    # tuile à placer → jamais nettoyée (bug : « , » et « : » disparaissaient
    # du plan → phrase incomplète).
    _t = text.strip()
    if _t and not any(ch.isalnum() for ch in _t) and len(_t) <= 3:
        return _t
    # Réponse identique à un élément du stock → on la garde telle quelle
    # (seuls les guillemets de bordure sont retirés plus bas).
    _in_stock = bool(_CURRENT_STOCK_KEYS) and (
        norm_key(text.strip().strip('"\'\u2018\u2019\u201C\u201D\u00AB\u00BB '))
        in _CURRENT_STOCK_KEYS
    )
    # ORDRE IMPORTANT : le pattern NUMBERED (ex: click_radio_button_2) doit
    # être appliqué AVANT ACTION, sinon ACTION mange juste le préfixe et
    # laisse le numéro ("click_radio_button_2" → "2" au lieu de "").
    if not _in_stock:
        text = _CLEAN_PREFIX_NUMBERED.sub('', text)
        text = _CLEAN_PREFIX_ACTION.sub('', text)
        text = _CLEAN_PREFIX_WORD.sub('', text)
    # Supprime UNIQUEMENT les guillemets/apostrophes en BORDURE
    # (pas ceux à l'intérieur du texte qui font partie des mots).
    # Paires symétriques courantes :
    _WRAPPERS = (
        ('"', '"'), ("'", "'"),
        ("\u2018", "\u2019"), ("\u201C", "\u201D"),
        ("\u00AB", "\u00BB"),  # «  »
        ("\u00AB ", " \u00BB"),
    )
    changed = True
    while changed:
        changed = False
        t = text.strip()
        for open_q, close_q in _WRAPPERS:
            if (t.startswith(open_q) and t.endswith(close_q)
                    and len(t) > len(open_q) + len(close_q)):
                t = t[len(open_q):-len(close_q)]
                changed = True
        # Cas asymétriques : un seul guillemet en bordure sans pair
        for q in ('"', "\u2018", "\u2019", "\u201C", "\u201D",
                  "\u00AB", "\u00BB"):
            if t.startswith(q):
                t = t[len(q):]
                changed = True
            if t.endswith(q):
                t = t[:-len(q)]
                changed = True
        text = t
    text = text.strip().strip(",;:").strip()
    return clean_text(text)


def fix_encoding(text: str) -> str:
    """Corrige les doubles accents et retire les numéros en tête."""
    if not text:
        return ""
    text = re.sub(
        r'([ñáéíóúüàèìòùâêîôûäëïöüç])\1', r'\1', text,
    )
    text = _CLEAN_LEADING_NUMBER.sub('', text.strip())
    return text.strip()


def is_skip_word(text: str) -> bool:
    return bool(text) and text.strip().lower() in SKIP_WORDS


# Libellés signalant la FIN d'un exercice/leçon → on peut oublier les
# textes (transcripts/dialogues) mémorisés. On matche en sous-chaîne car
# le bouton peut être "Finish the lesson", "See your results", etc.
FINISH_LABELS = (
    "finish", "terminer", "terminé", "see results", "see your results",
    "consulter les résultats", "voir les résultats", "view results",
    "ver resultados", "ergebnisse", "ver resultados", "vedi i risultati",
    "résultats", "results", "complete the lesson", "lesson complete",
)


def is_finish_label(text: str) -> bool:
    if not text:
        return False
    t = text.strip().lower()
    return any(lbl in t for lbl in FINISH_LABELS)


def is_validate_word(text: str) -> bool:
    """
    Mots de validation au sens LARGE : inclut "check", "finish",
    "done", "verify". Utilisé pour détecter les BOUTONS de validation
    sur la page (comme "Check answer" ou "I'm done").
    """
    return bool(text) and text.strip().lower() in VALIDATE_WORDS


def is_validate_button_word(text: str) -> bool:
    """
    Mots de validation STRICTS : ces mots ne font jamais partie
    d'une phrase d'exercice. Utilisé pour filtrer les actions
    proposées par l'IA sans risque de supprimer un mot légitime
    comme "check" (qui peut être dans "I need to check the car").
    """
    return bool(text) and text.strip().lower() in VALIDATE_BUTTON_ONLY


def norm_key(text: str) -> str:
    """
    Clé de normalisation pour comparer des options (case + trim +
    espaces internes réduits + ponctuation finale ignorée).

    La ponctuation finale (`,`, `.`, `!`, `?`, `;`, `:`) est retirée
    pour que les comparaisons fonctionnent quand l'IA retourne un mot
    sans sa ponctuation (ex: 'later' vs 'later,' du stock).

    Les suites d'espaces (et tabulations/sauts de ligne) sont réduites à
    UN seul espace : sur Site, une même phrase peut apparaître avec
    un double espace dans le DOM (textContent JS) mais un espace simple
    via Selenium .text ou après normalisation par l'IA. Sans cette
    réduction, 'A mí me pasa igual' et 'A mí me pasa  igual' étaient vues
    comme DEUX phrases → ajout d'un doublon fantôme → 4 actions pour 3
    tuiles → boucle infinie.

    L'opération est non-destructrice côté flux : le texte EXACT du
    stock reste utilisé pour cliquer le bouton, seule la clé de
    comparaison est normalisée.
    """
    s = (text or "").strip().lower()
    # Réduit toute suite d'espaces blancs internes à un seul espace
    s = re.sub(r"\s+", " ", s)
    # Retire la ponctuation finale (souvent attachée à un mot dans
    # les exercices reorder qui utilisent des mots-tokens comme
    # 'later,' ou 'sentence.')
    core = s.rstrip(",.!?;:").strip()
    # Tuile composée UNIQUEMENT de ponctuation ('.', '?', '!'…) : on
    # garde la ponctuation, sinon '.' et '?' donneraient tous deux ''
    # et deviendraient interchangeables lors du matching.
    return core if core else s.strip()


# --- Safe wrappers Selenium ------------------------------------------
# Ces helpers encapsulent les accès sensibles à un WebElement pour
# éviter tout StaleElementReferenceException qui pollue la console.

def safe_is_displayed(element) -> bool:
    try:
        return bool(element.is_displayed())
    except (StaleElementReferenceException, WebDriverException, Exception):
        return False


def safe_text(element) -> str:
    try:
        return (element.text or "").strip()
    except (StaleElementReferenceException, WebDriverException, Exception):
        return ""


def safe_attr(element, name: str) -> str:
    try:
        return element.get_attribute(name) or ""
    except (StaleElementReferenceException, WebDriverException, Exception):
        return ""


def safe_tag(element) -> str:
    try:
        return (element.tag_name or "").lower()
    except (StaleElementReferenceException, WebDriverException, Exception):
        return ""


def safe_location(element) -> dict:
    try:
        return element.location or {}
    except (StaleElementReferenceException, WebDriverException, Exception):
        return {}


def safe_size(element) -> dict:
    try:
        return element.size or {}
    except (StaleElementReferenceException, WebDriverException, Exception):
        return {}


def dedup_ordered(seq: Iterable) -> list:
    """Dedup en conservant l'ordre (pour strings comparables)."""
    seen, out = set(), []
    for x in seq:
        k = norm_key(x) if isinstance(x, str) else x
        if k in seen:
            continue
        seen.add(k)
        out.append(x)
    return out


# =====================================================================
# CACHES GLOBAUX (évite recharger les dicos 300k mots à chaque coup)
# =====================================================================

class _DictCache:
    """Cache des dictionnaires wordfreq + SpellChecker par langue."""

    _wordfreq_cache: dict[str, list[str]] = {}
    _spellchecker_cache: dict = {}
    _wordfreq_import_attempted = False
    _spellchecker_import_attempted = False

    @classmethod
    def _ensure_wordfreq(cls):
        if cls._wordfreq_import_attempted:
            return
        cls._wordfreq_import_attempted = True
        import importlib.util
        if importlib.util.find_spec("wordfreq") is not None:
            return
        if getattr(sys, "frozen", False):
            # Dans l'exe : pip est indisponible (sys.executable = l'exe).
            return
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install",
                 "wordfreq", "--break-system-packages", "-q"]
            )
        except Exception as e:
            print(f"   ⚠️ Impossible d'installer wordfreq : {e}")

    @classmethod
    def _ensure_spellchecker(cls):
        if cls._spellchecker_import_attempted:
            return
        cls._spellchecker_import_attempted = True
        import importlib.util
        if importlib.util.find_spec("spellchecker") is not None:
            return
        if getattr(sys, "frozen", False):
            # Dans l'exe : pip est indisponible (sys.executable = l'exe).
            return
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install",
                 "pyspellchecker", "--break-system-packages", "-q"]
            )
        except Exception as e:
            print(f"   ⚠️ Impossible d'installer pyspellchecker : {e}")

    @classmethod
    def get_wordlist(cls, lang: str, top_n: int = 300_000) -> list[str]:
        if lang in cls._wordfreq_cache:
            return cls._wordfreq_cache[lang]
        cls._ensure_wordfreq()
        try:
            from wordfreq import top_n_list
            words = top_n_list(lang, top_n)
            cls._wordfreq_cache[lang] = words
            return words
        except Exception as e:
            print(f"   ⚠️ wordfreq[{lang}] indisponible : {e}")
            cls._wordfreq_cache[lang] = []
            return []

    @classmethod
    def get_spellchecker(cls, lang: str):
        if lang in cls._spellchecker_cache:
            return cls._spellchecker_cache[lang]
        cls._ensure_spellchecker()
        try:
            from spellchecker import SpellChecker
            sc = SpellChecker(language=lang)
            cls._spellchecker_cache[lang] = sc
            return sc
        except Exception as e:
            # Pas de repli sur une AUTRE langue : un dictionnaire
            # espagnol validerait un anagramme anglais/français à tort.
            # Sans checker, le solveur se passe simplement de cette
            # vérification (checker None est géré par l'appelant).
            print(f"   ⚠️ SpellChecker[{lang}] indisponible : {e}")
            cls._spellchecker_cache[lang] = None
            return None


def _strip_accents_keep_n(s: str) -> str:
    """MAJUSCULES sans accents, en gardant Ñ (lettre à part en espagnol)."""
    import unicodedata
    out = []
    for ch in s.upper():
        if ch == "Ñ":
            out.append(ch)
            continue
        d = unicodedata.normalize("NFD", ch)
        out.append("".join(c for c in d if unicodedata.category(c) != "Mn"))
    return "".join(out)


def anagram_phrase_candidates(letters: list[str], words: list[str],
                              pattern: list[int] | None = None,
                              max_words: int = 3, limit: int = 12,
                              vocab: int = 80_000) -> list[str]:
    """Expressions de 1 à 3 mots du dictionnaire qui utilisent EXACTEMENT
    les lettres données. `pattern` = longueurs des mots dans l'ordre (lu
    sur les cases de réponse, ex. [3, 7] pour « DOS MUJERES ») : il fixe
    le nombre de mots ET leur ordre. Classement : mots les plus fréquents.
    """
    from collections import Counter
    tiles = [str(l).upper() for l in letters if str(l).strip()]
    keep_accents = any(_strip_accents_keep_n(t) != t for t in tiles)
    norm = (lambda w: w.upper()) if keep_accents else _strip_accents_keep_n
    target = Counter(tiles)
    n = sum(target.values())
    short_ok = {"Y", "A", "O", "E", "U", "À", "Á", "I"}
    # Mots du vocabulaire qui « rentrent » dans les lettres disponibles
    rank: dict[str, int] = {}
    for i, w in enumerate(words[:vocab]):
        w = str(w).strip()
        if not w or not w.isalpha():
            continue
        W = norm(w)
        if len(W) > n or (len(W) == 1 and W not in short_ok):
            continue
        if pattern and len(W) not in pattern:
            continue
        if Counter(W) - target:
            continue
        if W not in rank:
            rank[W] = i
    by_key: dict[str, list[str]] = {}
    for W in rank:
        by_key.setdefault("".join(sorted(W)), []).append(W)

    results: dict[str, int] = {}

    def add(parts):
        phrase = " ".join(parts)
        score = sum(rank[p] for p in parts) + 50_000 * (len(parts) - 1)
        if phrase not in results or score < results[phrase]:
            results[phrase] = score

    def rest_key(used):
        return "".join(sorted((target - Counter("".join(used))).elements()))

    if pattern:
        if sum(pattern) != n or len(pattern) > 4:
            return []
        lens = list(pattern)

        def rec(idx, used):
            if idx == len(lens) - 1:
                for w in by_key.get(rest_key(used), []):
                    if len(w) == lens[idx]:
                        add(used + [w])
                return
            remaining = target - Counter("".join(used))
            for w in rank:
                if len(w) != lens[idx] or Counter(w) - remaining:
                    continue
                rec(idx + 1, used + [w])
                if len(results) > 400:
                    return
        rec(0, [])
    else:
        for w in by_key.get("".join(sorted(target.elements())), []):
            add([w])
        if max_words >= 2:
            for a in rank:
                if len(a) >= n:
                    continue
                for b in by_key.get(rest_key([a]), []):
                    # ordre naturel probable : mot court (article,
                    # nombre…) en premier
                    add([a, b] if len(a) <= len(b) else [b, a])
        if max_words >= 3 and len(results) < limit:
            ws = [w for w in rank if len(w) < n - 1]
            for i, a in enumerate(ws):
                rem_a = target - Counter(a)
                for b in ws[i:]:
                    if Counter(b) - rem_a:
                        continue
                    for c in by_key.get(rest_key([a, b]), []):
                        add(sorted([a, b, c], key=len))
                if len(results) > 400:
                    break
    return [p for p, _ in sorted(results.items(), key=lambda kv: kv[1])][:limit]


# =====================================================================
# CONSTANTES DE TEXTE — EXCLUSION UI
# =====================================================================

_UI_EXCLUDE_PATTERNS = [
    # Compteurs de progression "1/13", "/12", "5/" → bruit UI.
    # ⚠️ On NE filtre PLUS le nombre seul "^\d+$" : une tuile de contenu
    # peut être un nombre légitime (ex. reorder "...the last 10 years?"),
    # et le bug observé était la disparition de la tuile "10".
    r"^\d+\s*/\s*\d+$", r"^/\s*\d+$", r"^\d+\s*/$",
    # ATTENTION : on ne retire JAMAIS "next", "finish", "check",
    # "continue", "back", "retour", "continuer", "terminer", NI
    # "validate/valider/submit/vérifier" du stock, car ce sont tous
    # des mots d'exercice POTENTIELLEMENT légitimes en anglais/français.
    # Bug observé : une leçon "validating specifications" proposait la
    # tuile "validate" comme MOT À PLACER ; la filtrer cassait l'exo
    # (4 mots pour 5 trous → jamais validé → boucle infinie).
    # On ne garde QUE les mots qui ne peuvent absolument jamais
    # apparaître dans une phrase d'exo (Skip/Sauter = impératif UI).
    # La protection contre une hallucination "Validate" de l'IA reste
    # assurée au niveau des ACTIONS par is_validate_button_word (qui
    # épargne déjà les mots présents dans le stock).
    r"^(skip|sauter|passer)$",
    r"^(home|accueil|menu|settings|paramètres)$",
    r"^(you have provided all the correct answers|you have not provided|"
    r"no worries|almost|correct|incorrect|bravo|félicitations|"
    r"congratulations).*$",
    r"^(¡correcto|incorrecto|muy bien|casi|excelente).*$",
    r"^(correct|incorrect|presque|bien joué|bonne réponse|"
    r"mauvaise réponse).*$",
]
_UI_EXCLUDE_RE = [re.compile(p, re.IGNORECASE) for p in _UI_EXCLUDE_PATTERNS]


def is_ui_noise(text: str) -> bool:
    if not text:
        return True
    t = text.strip().lower()
    if not t:
        return True
    for pat in _UI_EXCLUDE_RE:
        if pat.match(t):
            return True
    return False


# =====================================================================
# RÉFÉRENTIEL GRAMMATICAL C1 PAR LANGUE
# =====================================================================

GRAMMAR_C1: dict[str, str] = {
    "es": """GRAMMAIRE ESPAGNOLE C1 :
SUBJONCTIF — obligatoire après :
  • Verbes de volonté/émotion/doute : querer que, esperar que, dudar que, alegrarse de que, temer que
  • Expressions impersonnelles : es importante/necesario/posible/probable que, ojalá
  • Conjonctions : para que, aunque (doute), a menos que, con tal de que, antes de que, sin que
  • Subj. présent : -e/-es/-e/-emos/-éis/-en (AR) | -a/-as/-a/-amos/-áis/-an (ER/IR)
  • Subj. imparfait : -ara/-aras/-ara/-áramos/-arais/-aran | -iera/-ieras/-iera/-iéramos
  • Subj. parfait : haya/hayas/haya + pp | Subj. PQP : hubiera + pp
INDICATIF vs SUBJONCTIF :
  • Creer que + INDICATIF (affirmatif) / No creer que + SUBJONCTIF
  • Aunque + INDICATIF (fait réel) / Aunque + SUBJONCTIF (hypothèse)
PRONOMS CLITIQUES — ordre fixe : (se) + me/te/le/nos/os/les + lo/la/los/las
  • AVANT le verbe conjugué : "Se lo dije" ✓ / "Díjoselo" ✗ (sauf impératif/infinitif)
TEMPS & ASPECT :
  • Prétérit indéfini (action ponctuelle) vs Imparfait (état/habitude) — distinction cruciale C1
  • "Llevar + gérondif" : llevo dos horas esperando
  • "Acabar de + infinitif" : acabo de llegar
  • "Ir a + infinitif" (futur proche) vs futur simple (certitude/promesse)
PREPOSITIONS RÉGIES : depender DE, consistir EN, tratar DE, pensar EN, soñar CON, quedar EN
SER vs ESTAR : ser (identité/caractéristique) vs estar (état/position/résultat)
  • estar + adj = changement d'état : está cansado (il est fatigué maintenant)
  • ser + adj = caractéristique : es alto (il est grand de nature)""",

    "fr": """GRAMMAIRE FRANÇAISE C1 :
SUBJONCTIF — obligatoire après :
  • Verbes : vouloir que, souhaiter que, craindre que, douter que, regretter que
  • Expressions : il faut que, il est important/possible/douteux que, bien que, quoique
  • Conjonctions : pour que, avant que, à moins que, pourvu que, sans que, afin que
  • Subj. présent : que je sois/aie, que tu sois/aies, qu'il soit/ait...
  • Subj. passé : que j'aie été/eu, que tu aies fait...
INDICATIF vs SUBJONCTIF :
  • Penser que + INDICATIF (affirmatif) / Ne pas penser que + SUBJONCTIF
  • Quoique/Bien que toujours + SUBJONCTIF
TEMPS :
  • Passé composé (action terminée) vs Imparfait (état/habitude/description) — distinction C1
  • Plus-que-parfait : j'avais déjà mangé quand il est arrivé
  • Conditionnel passé : j'aurais voulu (regret/reproche)
  • Futur antérieur : quand tu auras fini, tu pourras partir
PRONOMS :
  • EN (quantité/partitif) : j'en veux deux | Y (lieu/chose) : j'y pense
  • Ordre : sujet + ne + COI + COD + y + en + verbe + pas
  • Relatifs complexes : dont (de qui/duquel), lequel (préposition + lequel), où (lieu/temps)
ACCORDS COMPLEXES :
  • Participe passé avec avoir : accord avec COD si antéposé (les livres que j'ai lus)
  • Participe passé avec être : accord avec le sujet""",

    "en": """ENGLISH GRAMMAR C1 :
TENSES & ASPECT :
  • Present Perfect vs Past Simple : "I have seen" (relevance now) vs "I saw" (specific past time)
  • Past Perfect : "She had already left when I arrived"
  • Mixed conditionals : "If I had studied, I would be a doctor now"

REPORTED SPEECH — BACKSHIFT (crucial C1) :
  • "She said she WAS confident" ← NOT "is" (present→past backshift)
  • "He said he WOULD come" ← will→would | "She said she COULD help" ← can→could
  • RULE : after "said/told/asked/thought" → shift tense BACK one step

DISCOURSE MARKERS — REORDER_CHRONOLOGICAL (critical C1) :
  • "though" at END of sentence → concession/contrast to PREVIOUS statement
    Ex: "Your performance was amazing" → "I wasn't optimistic at first, though!" (contrast)
  • "however / but / yet / nevertheless" → introduces contrast, comes AFTER what it contrasts
  • "at first" → indicates beginning of sequence, early stage
  • "in the end / eventually / finally" → indicates conclusion
  • "to be honest / to tell the truth" → personal admission, often follows a positive statement
  • ANAPHORA : if sentence contains "more X" → it refers to a PREVIOUS mention of "X"
    Ex: "made me feel MORE optimistic" → refers to "wasn't optimistic" → must come AFTER

CONVERSATIONAL SEQUENCE (reorder_chronological) :
  1. Opening/compliment/general statement
  2. Contrast or personal reaction (markers: though, at first, to be honest)
  3. Elaboration/detail (markers: but, however, as time went on)

SUBJUNCTIVE / MANDATIVE :
  • "It is essential that he BE present" | "I suggest that she TAKE the exam"

MODAL PERFECTS :
  • must have been | could have done | should have known | needn't have worried""",

    "de": """DEUTSCHE GRAMMATIK C1 :
KONJUNKTIV :
  • Konjunktiv II (Gegenwart) : wäre, hätte, würde + Infinitiv (Irrealis/Höflichkeit)
    "Wenn ich Zeit hätte, würde ich kommen" (Wenn-Satz → kein würde!)
  • Konjunktiv II (Vergangenheit) : wäre/hätte + Partizip II
    "Wenn er früher gegangen wäre, hätte er den Zug genommen"
  • Konjunktiv I (Indirekte Rede) : er sagt, er SEI krank | sie meinte, sie HABE
PASSIV :
  • Vorgangspassiv : wird + Partizip II (Aktion) "Das Buch wird gelesen"
  • Zustandspassiv : ist + Partizip II (Zustand) "Das Buch ist gelesen"
  • Passiv mit Modalverb : muss gemacht werden | konnte nicht gefunden werden
RELATIVSÄTZE :
  • Genitiv-Relativ : "Das Buch, dessen Autor bekannt ist..."
  • Präposition + Relativ : "Die Person, mit der ich gesprochen habe..."
KONNEKTOREN C1 :
  • Konzessiv : obwohl, obgleich, wenngleich, trotzdem, dennoch
  • Kausal : da, weil, denn | Konsekutiv : so dass, sodass, folglich
  • Temporal : sobald, nachdem, während, seitdem, bevor
WORTSTELLUNG :
  • Hauptsatz : Subjekt + Verb (Position 2) | Nebensatz : Verb am Ende
  • Modalpartikeln : doch, ja, mal, eigentlich, wohl (nuancent le registre)
ADJEKTIVDEKLINATION : stark/schwach/gemischt nach Artikel""",

    "pt": """GRAMÁTICA PORTUGUESA C1 :
SUBJUNTIVO :
  • Presente : -e/-es/-e/-emos/-eis/-em (AR) | -a/-as/-a/-amos/-ais/-am (ER/IR)
  • Imperfeito : -asse/-asses/-asse | -esse/-esses/-esse
  • Futuro do subjuntivo : -ar/-ares/-ar/-armos/-ardes/-arem
  • Obrigatório após : querer que, esperar que, embora, caso, se (condição futura)
FUTURO DO CONJUNTIVO : "Quando ele chegar, diga-lhe que..." (ação futura condicionada)
PERÍFRASES VERBAIS :
  • "Acabar de + infinitivo" (ação recente) | "Estar a + infinitivo" (PE) / "Estar + gerúndio" (PB)
  • "Ir + infinitivo" (futuro próximo) | "Ter de/que + infinitivo" (obrigação)
CONCORDÂNCIA NOMINAL : género e número — atenção às exceções (o programa, a vítima)
PRONOMES CLÍTICOS : posição pré-verbal (PB) vs mesoclítica/enclítica (PE)""",

    "it": """GRAMMATICA ITALIANA C1 :
CONGIUNTIVO — obbligatorio dopo :
  • Verbi : volere che, sperare che, temere che, credere che (neg.), dubitare che
  • Congiunzioni : benché, sebbene, affinché, a meno che, prima che, senza che
  • Congiuntivo presente : -i/-i/-i/-iamo/-iate/-ino (ARE) | -a/-a/-a/-iamo/-iate/-ano (ERE/IRE)
  • Congiuntivo imperfetto : -assi/-assi/-asse/-assimo | -essi/-essi/-esse/-essimo
  • Congiuntivo passato : abbia/abbia + pp | Congiuntivo trapassato : avesse + pp
CONDIZIONALE PASSATO : "avrei voluto" (rimpianto) | "avrebbe dovuto" (rimprovero)
PRONOMI COMBINATI : me lo, te la, glielo, ce li — posizione PRIMA del verbo coniugato
GERUNDIO : "Avendo studiato, ho superato l'esame" (anteriorità)
PASSATO PROSSIMO vs IMPERFETTO : distinzione fondamentale C1""",
}

GRAMMAR_SHORT: dict[str, str] = {
    "es": "Subjonctif: que/para que/aunque/ojalá/es importante que. Pronoms clitiques AVANT verbe. Ser≠Estar. Indéfini≠Imparfait. Régimes: depender DE, pensar EN, soñar CON.",
    "fr": "Subjonctif: bien que/pour que/vouloir que. PC≠Imparfait. Accord pp avec avoir+COD antéposé. EN(quantité)/Y(lieu). Inversion interrogative.",
    "en": "REPORTED SPEECH BACKSHIFT: said/told→is→was, has→had, will→would. DISCOURSE MARKERS for reorder: 'though'(end)=contrast to prev statement | 'at first'=opening | 'more X'=refers to prev X | sequence: compliment→reaction(though/at first)→elaboration(but/however). Modal perfects(must/should/could+have+pp).",
    "de": "Konjunktiv II: wäre/hätte/würde+Inf. Passiv: wird(Vorgang)≠ist(Zustand)+pp. Verb Position2(Haupts.)/Ende(Nebs.). Relativpronomen im Genitiv(dessen/deren).",
    "pt": "Futuro do conjuntivo(quando chegar). Infinitivo pessoal(para nós sabermos). PC≠Imperfeito. Mesóclise PE vs próclise PB.",
    "it": "Congiuntivo: benché/sebbene/affinché/volere che. Cong.imperfetto(-asse/-esse). Condizionale passato(avrei voluto). Pronomi combinati(me lo/glielo).",
}

CRITIQUE_RULES: dict[str, str] = {
    "es": """Vérifie dans cet ordre :
1. ¡ ou ¿ en début de phrase → l'élément avec ¡/¿ DOIT être en 1ère position
2. Pronoms clitiques (me/te/lo/la/le/nos/os/les/los/las/se) → AVANT le verbe conjugué
3. Négation "no" → AVANT le verbe ou le pronom clitique
4. Subjonctif : est-il présent après que/para que/aunque/ojalá/es importante que ?
5. Accord verbe-sujet (yo→-o, tú→-as/-es, él→-a/-e, nosotros→-amos/-emos)
6. Tous les éléments du stock utilisés ?""",
    "fr": """Vérifie dans cet ordre :
1. Pronoms (me/te/le/la/les/nous/vous/y/en) → AVANT le verbe conjugué
2. Négation ne...pas → encadre le verbe
3. Subjonctif : présent après bien que/pour que/vouloir que/il faut que ?
4. Accord du participe passé avec avoir si COD antéposé
5. Tous les éléments du stock utilisés ?""",
    "en": """Check in order:
1. Subject-verb agreement
2. Present Perfect vs Past Simple (time reference)
3. Modal perfect (must/should/could + have + pp)
4. Word order: subject + aux + verb + object
5. All stock elements used?""",
    "de": """Überprüfe der Reihe nach:
1. Verb an Position 2 im Hauptsatz
2. Verb am Ende im Nebensatz (dass/weil/wenn/obwohl...)
3. Konjunktiv II korrekt (wäre/hätte/würde+Inf)
4. Adjektivdeklination (stark/schwach/gemischt)
5. Alle Elemente verwendet?""",
    "pt": """Verifica por ordem:
1. Pronomes clíticos — próclise / ênclise / mesóclise
2. Subjuntivo após embora/caso/quando (futuro do conjuntivo)
3. Concordância nominal e verbal
4. Todos os elementos do stock utilizados ?""",
    "it": """Verifica nell'ordine:
1. Congiuntivo dopo benché/sebbene/affinché/volere che
2. Pronomi combinati PRIMA del verbo coniugato
3. Passato prossimo vs imperfetto
4. Tutti gli elementi dello stock utilizzati ?""",
}


# =====================================================================
# CLASSE PRINCIPALE : HybridBot
# =====================================================================

class HybridBot:
    """Bot qui résout automatiquement des exercices linguistiques C1."""

    # --- constantes internes ---
    NEEDS_DOUBLE_PASS = {
        "reorder_sentence", "reorder_chronological", "drag_drop", "qcm",
    }
    FUZZY_MATCH_CUTOFF = 0.82
    ELEMENT_FUZZY_THRESHOLD = 0.65

    # =================================================================
    #  INIT
    # =================================================================
    def __init__(self, langue: str = "es"):
        self.langue    = langue if langue in LANG_NAMES else "es"
        self.lang_name = LANG_NAMES[self.langue]
        if not isinstance(sys.stdout, _LogTee):
            sys.stdout = _LogTee(sys.stdout)

        print(f"\n🧠 Bot V5.1 — Langue : {self.lang_name.upper()} [{self.langue}]")

        self.model = None
        self._call_timestamps: list[float] = []

        self._init_best_model()

        opts = Options()
        opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{DEBUG_PORT}")
        self.driver = None
        # 1) webdriver-manager : télécharge AUTOMATIQUEMENT le chromedriver
        #    correspondant à la version de Chrome installée (plus de souci
        #    de version ni de PATH, sur n'importe quel PC — y compris si
        #    Chrome se met à jour). C'est la méthode recommandée pour
        #    distribuer le bot.
        try:
            from selenium.webdriver.chrome.service import Service
            from webdriver_manager.chrome import ChromeDriverManager
            service = Service(ChromeDriverManager().install())
            self.driver = webdriver.Chrome(service=service, options=opts)
        except ModuleNotFoundError:
            print("❌ Module 'webdriver-manager' manquant.")
            print("   Lance d'abord cette commande dans le terminal :")
            print("   pip install webdriver-manager")
            sys.exit(1)
        except Exception as e1:
            # 2) Repli : driver présent dans le PATH / Selenium Manager.
            try:
                self.driver = webdriver.Chrome(options=opts)
            except Exception:
                print(f"❌ Chrome non trouvé. Lance avec "
                      f"--remote-debugging-port={DEBUG_PORT}")
                print(f"   Vérifie que la fenêtre Chrome (port "
                      f"{DEBUG_PORT}) est bien OUVERTE avant de lancer "
                      f"le bot.")
                print(f"   Détail : {e1}")
                sys.exit(1)
        try:
            _prune_old_reports()
        except Exception:  # noqa: BLE001
            pass
        try:
            # Micro factice enregistré dès le départ → actif sur toutes
            # les pages chargées ensuite.
            self._install_fake_speech()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.driver.set_script_timeout(JS_ASYNC_TIMEOUT)
            print("   ✅ Chrome connecté")
            self._install_fake_speech()
        except Exception as e:
            print(f"❌ Connexion à Chrome échouée. Détail : {e}")
            sys.exit(1)

        # --- état dynamique (réinitialisable) ---
        self.memory_context: str   = ""
        # ACCUMULATEUR DE TEXTES DE LA LEÇON (transcripts + dialogues).
        # Une leçon peut enchaîner PLUSIEURS textes : un transcript sur la
        # slide 1, un autre sur la slide 2, un dialogue affiché pendant
        # l'exercice... Les questions peuvent porter sur n'importe lequel,
        # donc on les CUMULE tous (avec dédup). Vidé uniquement à la fin de
        # l'exercice (bouton "Finish") ou au reset complet (nouvelle leçon).
        # Chaque entrée : {"label": str, "text": str}.
        self.lesson_texts: list[dict] = []
        # Signature de la dernière page où on a TENTÉ une capture de
        # transcript (anti-boucle si l'extraction échoue).
        self._transcript_attempted_sig: str | None = None
        self.used_elements_cache: list = []
        self.last_reflex_clicked   = None
        self.last_refresh_time: float = time.time()
        # Refresh périodique : drapeau pour ne logger "report" qu'une fois
        # par période d'attente (anti-spam quand un exo est en cours).
        self._refresh_deferred_logged: bool = False
        # Refresh préventif sur page de correction : drapeau anti-boucle
        # (on ne rafraîchit qu'UNE fois par page de correction, sinon
        # recharger la correction la réafficherait et on boucverait).
        self._correction_refreshed: bool = False
        # Mesure de consommation Gemini (tokens + coût estimé) sur la
        # session, cumulée à chaque appel dans _call_gemini.
        self._usage = {"calls": 0, "in": 0, "out": 0,
                       "think": 0, "cost_usd": 0.0}
        # Compteurs pour les logs anti-spam
        self._last_zones_count: int | None = None
        self._last_empty_zones_ratio: tuple | None = None
        self._last_zones_diag_sig = None
        # Page où l'on a déjà fait l'attente "média" (anti-spam 3s/cycle).
        self._media_wait_sig = None
        # Signatures des parties (chevron) déjà dépliées sur la page
        # "Learning path" (anti-boucle : ne pas re-cliquer/replier).
        self._expanded_chevrons: set = set()
        # Signatures d'écrans « écoute + orthographe » dont l'audio a déjà
        # été joué (évite de relancer l'audio à chaque cycle).
        self._audio_clicked_sigs: set = set()
        # Vrai dès qu'un exo SortableJS s'est révélé non plaçable par le
        # bot : on Skip les suivants direct, SANS rappeler Gemini (coût).
        self._sortable_unsolvable: bool = False
        # Exercice de prononciation : écran courant (signature) et nombre
        # de tentatives de « I can't talk right now » sur cet écran.
        self._speak_sig = None
        self._speak_tries: int = 0
        # Suivi de session (affiché par le launcher, utilisé par le
        # minuteur pour s'arrêter ENTRE deux exercices).
        self._exo_in_progress: bool = False
        self.stats = {"exos": 0, "errors": 0}

    # -----------------------------------------------------------------
    #  Centralisation du reset d'état (évite les régressions)
    # -----------------------------------------------------------------
    def _reset_state(self, full: bool = False) -> None:
        """
        Remet à zéro l'état qui devient stale après une navigation.
        full=True → on remet aussi le timer d'auto-refresh.
        """
        self.memory_context        = ""
        self.used_elements_cache   = []
        self.last_reflex_clicked   = None
        # Reset des compteurs de logs (pour que le premier log du
        # prochain cycle affiche bien le nombre de zones)
        self._last_zones_count = None
        self._last_empty_zones_ratio = None
        if full:
            self.last_refresh_time = time.time()
            # ⚠️ NE PAS effacer lesson_texts ici : un reset complet est
            # déclenché par l'AUTO-REFRESH ou _unblock, qui rechargent la
            # MÊME leçon (le transcript n'est pas réaffiché après refresh
            # mais reste nécessaire aux questions). Les textes ne sont
            # vidés qu'au bouton "Finish" (vraie fin) ou à l'arrivée sur
            # le dashboard / sélecteur de leçon (nouvelle leçon).
            self._transcript_attempted_sig = None

    # =================================================================
    #  GESTION MODÈLE (clé unique, pas de rotation)
    # =================================================================
    def _init_best_model(self) -> None:
        print(f"   🔍 Connexion à {MODEL_NAME}...")
        key = API_KEYS[0].strip()
        # Plusieurs façons d'envoyer la clé : les anciennes clés « AIza… »
        # passent partout ; les nouvelles clés « AQ.… » de Google AI Studio
        # peuvent être refusées par le transport gRPC → on essaie aussi en
        # REST, puis comme jeton d'accès (en-tête Authorization).
        global _GENAI_CONF
        tries = [("gRPC", dict(api_key=key)),
                 ("REST", dict(api_key=key, transport="rest"))]
        if not key.startswith("AIza"):
            # Nouvelles clés « AQ.… » : Google les accepte (chez certains
            # comptes) UNIQUEMENT dans l'en-tête « x-goog-api-key », seul,
            # sans « ?key= » ni autre identifiant.
            tries.insert(0, ("en-tête x-goog-api-key", "header"))
            tries.append(("jeton", "token"))
        last = ""
        for label, conf in tries:
            try:
                if conf == "header":
                    import urllib.request
                    import urllib.error
                    url = ("https://generativelanguage.googleapis.com/v1beta/"
                           f"models/{MODEL_NAME}")
                    try:
                        urllib.request.urlopen(urllib.request.Request(
                            url, headers={"x-goog-api-key": key}),
                            timeout=15).read()
                    except urllib.error.HTTPError as he:
                        body = he.read().decode("utf-8", "ignore")
                        raise RuntimeError(f"{he.code} {body[:200]}")
                    global _REST_KEY
                    _REST_KEY = key
                    try:
                        _pick_available_models(key)
                    except RuntimeError:
                        print("❌ Clé API acceptée mais aucun modèle Gemini "
                              "utilisable.")
                        sys.exit(1)
                    self.model = _RestGemini(MODEL_NAME, key)
                    _GENAI_CONF = None
                    print(f"   ✅ {MODEL_NAME} (clé acceptée en {label}, "
                          f"connexion directe)")
                    return
                if conf == "token":
                    # Test direct (sans le SDK) : la clé passe-t-elle comme
                    # jeton « Authorization: Bearer » ?
                    import urllib.request
                    import urllib.error
                    url = ("https://generativelanguage.googleapis.com/v1beta/"
                           f"models/{MODEL_NAME}")
                    try:
                        urllib.request.urlopen(urllib.request.Request(
                            url, headers={"Authorization": f"Bearer {key}"}),
                            timeout=15).read()
                    except urllib.error.HTTPError as he:
                        body = he.read().decode("utf-8", "ignore")
                        raise RuntimeError(f"{he.code} {body[:200]}")
                    # Une variable d'environnement GOOGLE_API_KEY /
                    # GEMINI_API_KEY ferait échouer le SDK (clé + jeton
                    # « mutuellement exclusifs ») → on les ignore ici.
                    for env in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
                        os.environ.pop(env, None)
                    from google.oauth2.credentials import Credentials
                    conf = dict(credentials=Credentials(token=key),
                                transport="rest")
                genai.configure(**conf)
                # Vérifie clé + modèle via les métadonnées (gratuit, rapide)
                genai.get_model(f"models/{MODEL_NAME}")
                self.model = _make_model(MODEL_NAME)
                _GENAI_CONF = conf
                print(f"   ✅ {MODEL_NAME}"
                      + ("" if label == "gRPC" else f" (clé acceptée en {label})"))
                return
            except Exception as e:  # noqa: BLE001
                last = str(e)
                print(f"   ⚠️ Clé ({label}) : {last[:160]}")
        if "ACCESS_TOKEN_TYPE_UNSUPPORTED" in last or key.startswith("AQ."):
            print("   💡 Google refuse cette clé « AQ.… » (problème connu chez "
                  "Google, pas dans le bot). Solutions : 1) sur "
                  "console.cloud.google.com → APIs & Services → Credentials "
                  "→ Create credentials → API key (clé « AIza… », active "
                  "l'API « Generative Language » sur le projet) ; 2) ou "
                  "choisis « Clé fournie ».")
        print("❌ Clé API invalide ou modèle indisponible.")
        sys.exit(1)

    def _get_status(self) -> str:
        return MODEL_NAME

    # =================================================================
    #  UTILITAIRES DRIVER
    # =================================================================
    def _scroll_bottom(self) -> None:
        try:
            self.driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight);"
            )
            time.sleep(0.5)
        except WebDriverException:
            pass

    def _scroll_top(self) -> None:
        try:
            self.driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(0.3)
        except WebDriverException:
            pass

    def _scroll_horizontal_containers(self) -> None:
        """Scroll horizontal des conteneurs pour révéler les draggables cachés."""
        try:
            self.driver.execute_script(JS_SCROLL_HORIZONTAL)
            time.sleep(0.3)
        except WebDriverException:
            pass

    def _check_auto_refresh(self) -> bool:
        if time.time() - self.last_refresh_time < REFRESH_INTERVAL:
            return False
        # Le timer est écoulé, MAIS on ne rafraîchit JAMAIS en plein
        # exercice : un refresh à ce moment ferait recommencer l'exo
        # (re-analyse Gemini = tokens gaspillés). On attend un point
        # "sûr" — page de correction / navigation / dashboard, càd quand
        # AUCUN exercice n'attend d'action (_has_pending_exercise faux).
        # Le refresh se déclenchera dès que l'exo en cours sera terminé
        # et validé. On ne remet pas le timer : on re-teste chaque cycle.
        try:
            pending = self._has_pending_exercise()
        except Exception:
            pending = False
        if pending:
            if not self._refresh_deferred_logged:
                print("   ⏳ Refresh dû (timer écoulé) → reporté à la fin "
                      "de l'exercice en cours")
                self._refresh_deferred_logged = True
            return False
        self._refresh_deferred_logged = False
        print("⏰ Auto-Refresh...")
        try:
            self.driver.refresh()
            time.sleep(5)
            self._activate_page()
            self._reset_state(full=True)
            # Si la page rechargée est une page de correction, le bloc
            # dédié ne doit PAS re-rafraîchir derrière (évite un double
            # refresh). S'il s'avère que ce n'est pas une correction, ce
            # drapeau sera de toute façon réarmé au cycle suivant.
            self._correction_refreshed = True
            return True
        except WebDriverException as e:
            print(f"   ⚠️ Refresh échoué : {e}")
            return False

    def _get_page_text(self) -> str:
        try:
            body = self.driver.find_element(By.TAG_NAME, "body")
            txt = safe_text(body)
        except (NoSuchElementException, WebDriverException):
            return ""
        # Mémorise les mots vus pendant la session (sert à retrouver
        # l'orthographe EXACTE du site, même quand elle contient une
        # coquille, ex. « limipio » au lieu de « limpio »).
        try:
            lines = self.__dict__.setdefault("_seen_lines", {})
            for ln in (txt or "").splitlines():
                ln = ln.strip()
                if 2 <= len(ln) <= 80:
                    lines[ln] = None
            if len(lines) > 4000:
                lines.clear()
            seen = self.__dict__.setdefault("_seen_words", {})
            for w in re.findall(r"[^\W\d_]{3,}", txt or ""):
                k = w.lower()
                seen[k] = seen.get(k, 0) + 1
            if len(seen) > 20000:
                seen.clear()
        except Exception:  # noqa: BLE001
            pass
        return txt

    def _get_reading_passage(self) -> str:
        """
        Extrait le TEXTE DE LECTURE affiché à côté de l'exercice (dialogue,
        article, transcription inline) quand il existe. Heuristique : le
        plus gros bloc de prose qui NE contient AUCUN élément interactif
        (pas d'input/textarea/select/button/role=button) — donc ce n'est
        ni le panneau de question ni celui des options.

        Renvoie "" si aucun passage substantiel n'est trouvé.
        """
        try:
            passage = self.driver.execute_script(
                """
                const blocks = Array.from(
                    document.querySelectorAll('div,section,article')
                );
                let best = '';
                for (const e of blocks) {
                    // Exclure tout conteneur portant de l'interactif :
                    // c'est le panneau question/options, pas la lecture.
                    if (e.querySelector(
                        'input,textarea,select,button,[role="button"]'))
                        continue;
                    const t = (e.innerText || '').trim();
                    if (t.length > best.length) best = t;
                }
                return best;
                """
            ) or ""
        except WebDriverException:
            return ""
        passage = passage.strip()
        # Seuil : un vrai passage de lecture fait plusieurs phrases.
        if len(passage) < 400:
            return ""
        return passage[:6000]

    def _text_before_first_blank(self):
        """Texte qui PRÉCÈDE immédiatement le 1er champ de saisie dans sa
        phrase (ou None si indéterminé). Sert à savoir si le trou est en
        DÉBUT de phrase (→ majuscule attendue) ou au MILIEU (→ minuscule).
        Bug observé : 'You sound ___' → le bot écrivait 'Misled' (majuscule)
        alors que 'misled' au milieu de phrase doit rester en minuscule."""
        try:
            return self.driver.execute_script(
                """
                const inp = document.querySelector(
                    'input[type="text"], input:not([type]), textarea, '
                    + '[contenteditable="true"]');
                if (!inp) return null;
                const parts = [];
                let cur = inp;
                for (let depth = 0; depth < 4 && cur; depth++) {
                    let p = cur.previousSibling;
                    while (p) {
                        parts.unshift(p.textContent || '');
                        p = p.previousSibling;
                    }
                    const parent = cur.parentElement;
                    if (!parent) break;
                    const tag = parent.tagName;
                    cur = parent;
                    if (tag === 'P' || tag === 'LI') break;
                }
                return parts.join('').replace(/\\s+/g, ' ').trim();
                """
            )
        except WebDriverException:
            return None

    def _get_emphasized_words(self) -> list[str]:
        """Mots/expressions mis en GRAS dans le CONTENU (<strong>/<b>/
        classes font-bold/semibold). Sur Site, le mot-réponse d'un
        exercice de vocabulaire (phrase à trou) est très souvent en gras
        dans le texte de lecture, mais cette mise en forme est perdue à
        l'extraction innerText. On exclut l'UI (boutons/nav/liens) et on
        ne garde que des expressions courtes (≤ 6 mots, ≤ 40 car.)."""
        try:
            words = self.driver.execute_script(
                """
                const out = [];
                const els = Array.from(document.querySelectorAll(
                    'strong, b, [class*="font-bold"], '
                    + '[class*="font-semibold"]'));
                for (const e of els) {
                    // Exclure l'UI : bouton / lien / interactif / nav.
                    if (e.closest('button,a,[role="button"],input,'
                        + 'textarea,select,nav,header')) continue;
                    const t = (e.innerText || '').trim();
                    if (!t) continue;
                    if (t.length > 40) continue;       // pas un paragraphe
                    if (t.split(/\\s+/).length > 6) continue;
                    out.push(t);
                }
                return out;
                """
            ) or []
        except WebDriverException:
            return []
        seen = set()
        uniq = []
        for w in words:
            k = (w or "").strip().lower()
            if k and k not in seen:
                seen.add(k)
                uniq.append(w.strip())
        return uniq[:12]

    def _bold_hint_block(self) -> str:
        """Bloc d'indice à insérer dans les prompts : liste des mots en
        gras du texte (souvent la réponse). Renvoie '' s'il n'y en a pas."""
        emph = self._get_emphasized_words()
        if not emph:
            return ""
        liste = ", ".join(f"\"{w}\"" for w in emph)
        return (
            "=== MOTS EN GRAS DANS LE TEXTE ===\n"
            "Sur Site, le mot à trouver (vocabulaire / blanc à "
            "compléter) est SOUVENT un mot mis en GRAS dans le texte de "
            f"lecture. Mots en gras repérés : {liste}.\n"
            "Si l'un d'eux complète correctement la phrase (SENS + "
            "grammaire), privilégie-le. Ne force PAS s'il ne convient "
            "pas vraiment.\n\n"
        )

    # -----------------------------------------------------------------
    #  ACCUMULATEUR DE TEXTES DE LEÇON (transcripts + dialogues)
    # -----------------------------------------------------------------
    @staticmethod
    def _norm_text(t: str) -> str:
        return " ".join((t or "").split()).lower()

    def _remember_text(self, text: str, label: str = "TEXTE") -> bool:
        """
        Ajoute un texte (transcript / dialogue) à l'accumulateur de la
        leçon, avec dédup intelligente :
          - ignoré s'il est déjà présent ou inclus dans un bloc existant ;
          - s'il ENGLOBE un bloc existant (version plus complète, ex.
            révélée au scroll), il REMPLACE ce bloc ;
          - sinon c'est un NOUVEAU texte (slide 2, 3...) → ajouté.
        Retourne True si l'accumulateur a changé.
        """
        text = (text or "").strip()
        if len(text) < 40:
            return False
        norm = self._norm_text(text)
        for i, entry in enumerate(self.lesson_texts):
            en = self._norm_text(entry["text"])
            if norm == en or norm in en:
                return False                       # déjà couvert
            if en in norm:                          # nouveau + complet
                self.lesson_texts[i] = {"label": label, "text": text}
                print(f"   🧠 Texte MIS À JOUR ({label}, {len(text)} car.) "
                      f"— {len(self.lesson_texts)} bloc(s) en mémoire")
                return True
        self.lesson_texts.append({"label": label, "text": text})
        print(f"   🧠 Nouveau texte mémorisé ({label}, {len(text)} car.) "
              f"— {len(self.lesson_texts)} bloc(s) en mémoire pour la leçon")
        return True

    def _clear_lesson_texts(self, reason: str = "") -> None:
        """Vide l'accumulateur (fin d'exercice / nouvelle leçon)."""
        if self.lesson_texts:
            print(f"   🗑️  Mémoire textes effacée"
                  f"{(' (' + reason + ')') if reason else ''} "
                  f"— {len(self.lesson_texts)} bloc(s) oublié(s)")
        self.lesson_texts = []

    def _build_lesson_texts_block(self) -> str:
        """Construit la section du prompt regroupant TOUS les textes
        mémorisés (transcripts + dialogues) de la leçon en cours."""
        if not self.lesson_texts:
            return ""
        # Budget total partagé pour ne pas exploser le prompt
        budget = 7000
        parts = []
        per = max(1500, budget // max(1, len(self.lesson_texts)))
        for n, entry in enumerate(self.lesson_texts, 1):
            tag = entry.get("label", "TEXTE")
            txt = entry["text"][:per]
            parts.append(f"--- {tag} #{n} ---\n{txt}")
        body = "\n\n".join(parts)
        return (
            "=== TEXTES DE LA LEÇON (transcripts / dialogues mémorisés) ===\n"
            "⚠️ Les questions de compréhension portent sur CES textes "
            "(il peut y en avoir plusieurs : slides successives, dialogue "
            "affiché puis masqué...). Cherche la réponse dans l'ENSEMBLE "
            "ci-dessous, pas seulement dans ce qui est visible à l'écran.\n"
            f"{body}\n\n"
        )

    def _update_reading_passage(self) -> None:
        """
        Capture le texte de lecture visible (dialogue/article) et l'ajoute
        à l'accumulateur de la leçon. Appelé à chaque cycle : la dédup gère
        les répétitions ; chaque NOUVEAU dialogue (slide suivante) est ajouté
        en plus des précédents.
        """
        try:
            current = self._get_reading_passage()
        except Exception:
            current = ""
        if current:
            self._remember_text(current, "DIALOGUE/TEXTE")

    # -----------------------------------------------------------------
    #  TRANSCRIPT — ouverture + extraction propre (persistant)
    # -----------------------------------------------------------------
    # Libellés du bouton "Voir la transcription" dans les 6 langues
    # supportées (ouverture). On essaie tout, insensible à la casse.
    TRANSCRIPT_OPEN_LABELS = [
        "View the transcript", "Show the transcript", "Transcript",
        "Voir la transcription", "Afficher la transcription", "Transcription",
        "Ver la transcripción", "Ver transcripción", "Transcripción",
        "Transkript anzeigen", "Transkript",
        "Ver a transcrição", "Transcrição",
        "Visualizza la trascrizione", "Trascrizione",
    ]
    # Racines de mots (matching TOLÉRANT, insensible à la casse) pour
    # rattraper toutes les variantes ("Ver la transcripción del audio",
    # "Mostrar transcripción", "Show transcript", etc.) :
    #   transcri → EN transcript/transcription, FR transcription,
    #              ES transcripción, PT transcrição
    #   trascri  → IT trascrizione
    #   transkri → DE Transkript
    TRANSCRIPT_KEYWORDS = ("transcri", "trascri", "transkri")

    def _has_transcript_testid(self) -> bool:
        """Le bouton transcript de Site porte data-testid=
        'transcript-toggle' (stable, non traduit)."""
        try:
            return bool(self.driver.find_elements(
                By.CSS_SELECTOR,
                "[data-testid='transcript-toggle'], "
                "[data-testid*='transcript']"))
        except WebDriverException:
            return False

    def _page_has_media(self) -> bool:
        """Détecte un lecteur audio/vidéo sur la page (balise <audio>/
        <video>, data-testid de lecteur, ou un minuteur type '00:00 /
        01:06'). Sert à savoir s'il faut attendre le chargement async du
        widget transcript avant d'abandonner."""
        try:
            if self.driver.execute_script(
                "return !!(document.querySelector('audio, video') || "
                "document.querySelector(\"[data-testid*='audio'],"
                "[data-testid*='player'],[data-testid*='sound'],"
                "[class*='audio-player'],[class*='AudioPlayer']\"));"
            ):
                return True
        except (JavascriptException, WebDriverException):
            pass
        try:
            # Minuteur audio "00:00 / 01:06"
            if re.search(r"\d{1,2}:\d{2}\s*/\s*\d{1,2}:\d{2}",
                         self._get_page_text()):
                return True
        except Exception:
            pass
        return False

    def _find_transcript_button(self):
        """Trouve le bouton d'ouverture du transcript. Priorité au
        sélecteur STABLE de Site (data-testid='transcript-toggle'),
        puis libellés exacts, puis racine de mot tolérante."""
        # 0) sélecteur stable Site (le plus fiable, non traduit)
        try:
            for el in self.driver.find_elements(
                By.CSS_SELECTOR,
                "[data-testid='transcript-toggle'], "
                "[data-testid*='transcript']",
            ):
                if safe_is_displayed(el):
                    return el
        except WebDriverException:
            pass
        # 1) libellés exacts (rapide)
        for label in self.TRANSCRIPT_OPEN_LABELS:
            try:
                el = self._find_element_by_text(label)
            except WebDriverException:
                el = None
            if el and safe_is_displayed(el):
                return el
        # 2) recherche tolérante par racine de mot
        try:
            el = self.driver.execute_script(
                """
                const kws = arguments[0];
                const nodes = Array.from(document.querySelectorAll(
                    "button, a, [role='button'], [class*='cursor-pointer']"));
                for (const n of nodes) {
                    const hay = ((n.innerText || '') + ' ' +
                        (n.getAttribute('aria-label') || '') + ' ' +
                        (n.getAttribute('title') || '')).toLowerCase();
                    if (kws.some(k => hay.indexOf(k) >= 0)) {
                        const r = n.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) return n;
                    }
                }
                return null;
                """,
                list(self.TRANSCRIPT_KEYWORDS),
            )
            if el and safe_is_displayed(el):
                return el
        except (JavascriptException, WebDriverException):
            pass
        return None

    def _extract_transcript_text(self, before_text: str) -> str:
        """
        Extrait UNIQUEMENT le texte du transcript en comparant le texte
        de la page AVANT et APRÈS l'ouverture du panneau.

        Avantages :
        - langue-agnostique (pas besoin de connaître la structure DOM) ;
        - robuste au scroll (innerText/textContent renvoie tout le texte
          du conteneur même hors viewport) ;
        - ne garde que ce qui est APPARU (le transcript), pas le bruit UI
          (Takeaways, objectifs, boutons…).
        """
        # Laisse le panneau s'afficher / charger
        time.sleep(2)

        # Tente de scroller dans le panneau pour forcer le rendu complet
        # (certains panneaux ne peuplent le DOM qu'au scroll).
        try:
            self.driver.execute_script(
                """
                const candidates = Array.from(
                    document.querySelectorAll('div,section,article')
                ).filter(e => {
                    const s = getComputedStyle(e);
                    return (s.overflowY === 'auto' || s.overflowY === 'scroll')
                           && e.scrollHeight > e.clientHeight + 10;
                });
                candidates.forEach(e => { e.scrollTop = e.scrollHeight; });
                """
            )
            time.sleep(0.4)
        except WebDriverException:
            pass

        after_text = self._get_page_text()

        # Diff ligne à ligne : on garde les lignes présentes APRÈS mais
        # absentes AVANT (= le contenu du transcript révélé).
        def _lines(t: str) -> list[str]:
            return [ln.strip() for ln in (t or "").splitlines() if ln.strip()]

        before_set = set(_lines(before_text))
        new_lines = [ln for ln in _lines(after_text) if ln not in before_set]

        # Filtre le bruit résiduel (libellés de boutons du panneau)
        NOISE = {
            "close the transcript", "fermer la transcription",
            "cerrar la transcripción", "transkript schließen",
            "fechar a transcrição", "chiudi la trascrizione",
            "zoom", "next", "suivant", "continue", "continuar",
            "view the transcript", "voir la transcription",
        }
        cleaned = [
            ln for ln in new_lines
            if ln.lower() not in NOISE and len(ln) > 1
        ]
        transcript = "\n".join(cleaned).strip()

        # Fallback : si le diff n'a rien donné (page entièrement
        # remplacée, ou before vide), on prend le texte du conteneur
        # scrollable le plus volumineux.
        if len(transcript) < 20:
            try:
                biggest = self.driver.execute_script(
                    """
                    const cs = Array.from(
                        document.querySelectorAll('div,section,article')
                    ).filter(e => {
                        const s = getComputedStyle(e);
                        return (s.overflowY === 'auto' || s.overflowY === 'scroll')
                               && e.scrollHeight > e.clientHeight + 10;
                    });
                    if (!cs.length) return '';
                    cs.sort((a,b) => (b.innerText||'').length - (a.innerText||'').length);
                    return cs[0].innerText || '';
                    """
                ) or ""
                if len(biggest.strip()) > len(transcript):
                    transcript = biggest.strip()
            except WebDriverException:
                pass

        return transcript[:8000]

    # Libellés du bouton "Fermer la transcription" (6 langues)
    TRANSCRIPT_CLOSE_LABELS = [
        "Close the transcript", "Hide the transcript",
        "Fermer la transcription", "Cerrar la transcripción",
        "Transkript schließen", "Fechar a transcrição",
        "Chiudi la trascrizione",
    ]

    def _harvest_page_texts(self) -> None:
        """RÉCOLTE SANS NAVIGUER : mémorise le texte de lecture visible
        (dialogue, article) ET, s'il y a un bouton transcript, l'ouvre,
        enregistre le texte puis le referme. Appelée AVANT toute action
        qui quitte la page (Next, carrousel…) pour ne jamais perdre un
        texte dont les questions suivantes auront besoin."""
        try:
            self._update_reading_passage()
        except Exception:  # noqa: BLE001
            pass
        try:
            page_low = self._get_page_text().lower()
            has_kw = (any(k in page_low for k in self.TRANSCRIPT_KEYWORDS)
                      or self._has_transcript_testid())
            btn = self._find_transcript_button() if has_kw else None
        except WebDriverException:
            return
        if not btn:
            return
        sig = re.sub(r"\d{1,2}:\d{2}", "", page_low)[:1200]
        done = self.__dict__.setdefault("_harvest_sigs", set())
        if sig in done or sig == self._transcript_attempted_sig:
            return
        done.add(sig)
        before = self._get_page_text()
        print("   📄 Transcript trouvé avant de quitter la page → "
              "ouverture et enregistrement")
        try:
            self._ghost_click(btn)
        except WebDriverException:
            return
        txt = self._extract_transcript_text(before)
        if txt:
            self._remember_text(txt, "TRANSCRIPT")
            print(f"   📝 Aperçu : {txt[:120].replace(chr(10), ' ')}…")
        # Referme le panneau
        for close_lbl in self.TRANSCRIPT_CLOSE_LABELS:
            try:
                cbtn = self._find_element_by_text(close_lbl)
            except WebDriverException:
                cbtn = None
            if cbtn and safe_is_displayed(cbtn):
                self._ghost_click(cbtn)
                return
        try:
            tg = self.driver.find_elements(
                By.CSS_SELECTOR, "[data-testid='transcript-toggle']")
            if tg and safe_is_displayed(tg[0]):
                self._ghost_click(tg[0])
        except WebDriverException:
            pass

    def _capture_transcript_if_present(self) -> bool:
        """
        DÉTECTION DÉTERMINISTE (DOM) du transcript — ne dépend PAS de la
        classification de l'IA. Si un bouton "Voir la transcription" est
        présent et qu'on n'a pas encore mémorisé le transcript de cette
        leçon, on l'ouvre, on enregistre le texte, puis :
          - page d'INTRO pure (sans exercice) → on clique Next (return True)
          - page avec EXERCICE → on referme le panneau et on rend la main
            au flux normal (transcript déjà en mémoire) (return False)

        Retourne True si une navigation a eu lieu (le cycle doit faire
        `continue`), False sinon.
        """
        # Pré-check peu coûteux : une racine transcript figure-t-elle dans
        # le texte de la page ? (évite la recherche DOM coûteuse sinon).
        # On teste les RACINES (transcri/trascri/transkri) pour rattraper
        # toutes les variantes de libellé.
        page_low = self._get_page_text().lower()
        kw_hit = next((k for k in self.TRANSCRIPT_KEYWORDS
                       if k in page_low), None)
        # Le bouton Site porte data-testid='transcript-toggle' même
        # quand son texte n'est PAS capté par _get_page_text (zone du
        # lecteur audio filtrée). On vérifie donc aussi sa présence DOM.
        if not kw_hit:
            if self._has_transcript_testid():
                kw_hit = "data-testid"
        # ── CHARGEMENT ASYNCHRONE ───────────────────────────────────────
        # Le widget audio + son bouton transcript se charge en différé. Si
        # on ne trouve RIEN du premier coup mais que la page a un lecteur
        # audio/vidéo, on attend (poll court) que le bouton apparaisse
        # AVANT de rendre la main (sinon le cycle clique 'Next' et on rate
        # le transcript). Bug observé : sur réseau lent (504), la capture
        # passait avant le rendu du bouton.
        # ⚠️ On ne fait CETTE attente qu'UNE fois par page : sinon, sur une
        # page de correction qui a aussi un lecteur audio mais AUCUN
        # transcript, on rajoutait 3 s à CHAQUE cycle. La signature de page
        # doit être STABLE dans une page (on retire le minuteur audio
        # volatil "00:03 / 01:00") et UNIQUE entre pages (on prend beaucoup
        # de contenu, pas juste l'en-tête de navigation qui est commun à
        # toutes les pages → sinon collision → attente sautée à tort et
        # transcript raté sur une page d'intro).
        if not kw_hit and self._page_has_media():
            media_sig = re.sub(r"\d{1,2}:\d{2}", "",
                               self._get_page_text())[:1200]
            if media_sig != getattr(self, "_media_wait_sig", None):
                self._media_wait_sig = media_sig
                print("   ⏳ Média détecté sans transcript visible → "
                      "attente du chargement du bouton transcript...")
                for _ in range(12):
                    time.sleep(0.5)
                    if self._has_transcript_testid():
                        kw_hit = "data-testid"
                        break
                    pl = self._get_page_text().lower()
                    if any(k in pl for k in self.TRANSCRIPT_KEYWORDS):
                        kw_hit = "async"
                        page_low = pl
                        break
        if not kw_hit:
            return False

        # Anti-boucle : si on a déjà tenté la capture sur CETTE page, on ne
        # réessaie pas (mais une NOUVELLE page/slide avec son propre
        # transcript sera bien capturée → on accumule plusieurs textes).
        # Signature STABLE (minuteur audio retiré) et UNIQUE par page
        # (beaucoup de contenu, pas juste l'en-tête de nav commun) pour
        # éviter qu'une page d'intro soit prise pour une page déjà traitée.
        sig = re.sub(r"\d{1,2}:\d{2}", "", page_low)[:1200]
        if sig == self._transcript_attempted_sig:
            return False

        btn = self._find_transcript_button()
        if not btn:
            # DIAGNOSTIC : le mot transcript est sur la page mais aucun
            # bouton cliquable n'a été reconnu → on dump les éléments
            # porteurs du mot pour cibler le bon sélecteur.
            print(f"   🔬 Mot '{kw_hit}' présent mais bouton transcript "
                  f"NON trouvé — éléments candidats :")
            try:
                cands = self.driver.execute_script(
                    """
                    const kws = arguments[0];
                    const out = [];
                    for (const n of Array.from(
                            document.querySelectorAll('*'))) {
                        const txt = (n.innerText || '').trim();
                        const aria = ((n.getAttribute &&
                            n.getAttribute('aria-label')) || '');
                        const title = ((n.getAttribute &&
                            n.getAttribute('title')) || '');
                        const hay = (txt + ' ' + aria + ' '
                            + title).toLowerCase();
                        if (!kws.some(k => hay.indexOf(k) >= 0)) continue;
                        // on ne garde que les éléments "serrés" (pas les
                        // gros conteneurs) OU porteurs d'aria/title
                        if (txt.length > 90 && !aria && !title) continue;
                        const r = n.getBoundingClientRect();
                        out.push({
                            tag: n.tagName,
                            role: (n.getAttribute &&
                                n.getAttribute('role')) || null,
                            cls: (n.className || '').toString().slice(0, 70),
                            aria: aria.slice(0, 40),
                            w: Math.round(r.width), h: Math.round(r.height),
                            txt: txt.slice(0, 45)
                        });
                        if (out.length >= 8) break;
                    }
                    return out;
                    """,
                    list(self.TRANSCRIPT_KEYWORDS),
                )
                for c in (cands or []):
                    print(f"      ▸ <{str(c.get('tag','')).lower()}> "
                          f"role={c.get('role')!r} "
                          f"w×h={c.get('w')}×{c.get('h')} "
                          f"aria={c.get('aria','')!r} "
                          f"cls={c.get('cls','')!r} "
                          f"txt={c.get('txt','')!r}")
                if not cands:
                    print("      (aucun élément porteur du mot — le texte "
                          "vient peut-être d'ailleurs)")
            except (JavascriptException, WebDriverException) as e:
                print(f"      ⚠️ diag échoué : {str(e)[:60]}")
            return False

        self._transcript_attempted_sig = sig
        before = self._get_page_text()
        print("   📄 Transcript présent → ouverture (détection déterministe)")
        self._ghost_click(btn)

        transcript = self._extract_transcript_text(before)
        if transcript:
            self._remember_text(transcript, "TRANSCRIPT")
            preview = transcript[:120].replace("\n", " ")
            print(f"   📝 Aperçu : {preview}…")
        else:
            print("   ⚠️ Transcript vide après extraction")

        # Referme le panneau (utile si la page contient un exercice)
        closed = False
        for close_lbl in self.TRANSCRIPT_CLOSE_LABELS:
            try:
                cbtn = self._find_element_by_text(close_lbl)
            except WebDriverException:
                cbtn = None
            if cbtn and safe_is_displayed(cbtn):
                self._ghost_click(cbtn)
                closed = True
                break
        if not closed:
            # Le bouton data-testid='transcript-toggle' bascule ouvert/fermé
            # → un re-clic referme le panneau même si le libellé a changé.
            try:
                tg = self.driver.find_elements(
                    By.CSS_SELECTOR, "[data-testid='transcript-toggle']")
                if tg and safe_is_displayed(tg[0]):
                    self._ghost_click(tg[0])
            except WebDriverException:
                pass

        # La page contient-elle un exercice à résoudre ?
        try:
            has_exercise = bool(
                self._get_drop_zones()
                or self._get_text_inputs()
                or self._has_pending_exercise()
            )
        except WebDriverException:
            has_exercise = False

        if not has_exercise:
            # Page d'intro pure → on avance vers les questions
            nxt = (self._find_element_by_text("Next")
                   or self._find_element_by_text("Suivant"))
            if nxt and safe_is_displayed(nxt):
                print("   ➡️ Page d'intro (sans exercice) → clic Next")
                self._ghost_click(nxt)
                time.sleep(3)
            return True

        # Exercice présent : transcript en mémoire, on laisse le flux
        # normal résoudre la question (ne PAS cliquer Next ici, sinon
        # on skipperait l'exercice).
        return False

    # -----------------------------------------------------------------
    #  FULL-PAGE SCREENSHOT (CDP) — voit tout, pas seulement le viewport
    # -----------------------------------------------------------------
    def _capture(self) -> Image.Image:
        """
        Capture l'intégralité de la page (au-delà du viewport) via
        Chrome DevTools Protocol. Fallback : screenshot classique.
        """
        # 1) Tentative CDP full page
        try:
            result = self.driver.execute_cdp_cmd(
                "Page.captureScreenshot",
                {
                    "format": "png",
                    "captureBeyondViewport": True,
                    "fromSurface": True,
                },
            )
            data = result.get("data") if isinstance(result, dict) else None
            if data:
                raw = base64.b64decode(data)
                with io.BytesIO(raw) as bio:
                    return Image.open(bio).copy()
        except Exception:
            pass

        # 2) Fallback : screenshot viewport classique
        try:
            png = self.driver.get_screenshot_as_png()
            with io.BytesIO(png) as bio:
                return Image.open(bio).copy()
        except Exception as e:
            print(f"   ⚠️ Capture impossible : {e}")
            # Dernière chance : image vide pour ne rien planter
            return Image.new("RGB", (1, 1), (255, 255, 255))

    @staticmethod
    def _combine_images_vertical(images: list) -> Image.Image:
        """Concatène verticalement des images PIL (séparateur gris de 10 px)."""
        if not images:
            return Image.new("RGB", (1, 1), (255, 255, 255))
        if len(images) == 1:
            return images[0]
        sep = 10  # séparateur gris
        max_w = max(im.width for im in images)
        total_h = sum(im.height for im in images) + sep * (len(images) - 1)
        # Cap à 16384 px pour éviter de planter PIL / les modèles.
        LIMIT = 16384
        if total_h > LIMIT:
            n = len(images)
            sum_h = sum(im.height for im in images)
            # Équation : scale * sum_h + sep*(n-1) <= LIMIT
            # On laisse 32 px de marge pour absorber les arrondis int().
            available = max(1, LIMIT - sep * (n - 1) - 32)
            scale = available / sum_h
            images = [
                im.resize(
                    (max(1, int(im.width * scale)),
                     max(1, int(im.height * scale))),
                    Image.LANCZOS,
                )
                for im in images
            ]
            max_w = max(im.width for im in images)
            total_h = sum(im.height for im in images) + sep * (n - 1)
        out = Image.new("RGB", (max_w, total_h), (220, 220, 220))
        y = 0
        for im in images:
            out.paste(im, (0, y))
            y += im.height + sep
        return out

    def _find_clickable_ancestor(self, element, max_depth: int = 6):
        """Remonte jusqu'à un ancêtre cliquable (button/a/role=button/cursor-pointer)."""
        cur = element
        for _ in range(max_depth):
            try:
                parent = cur.find_element(By.XPATH, "./..")
            except (NoSuchElementException,
                    StaleElementReferenceException, WebDriverException):
                return None
            tag = safe_tag(parent)
            cls = safe_attr(parent, "class")
            role = safe_attr(parent, "role")
            if (tag in ("button", "a")
                    or role == "button"
                    or "cursor-pointer" in cls):
                return parent
            cur = parent
        return None

    @staticmethod
    def _is_chevron_right_path(d: str) -> bool:
        """
        Heuristique : le path `d` d'un SVG ressemble-t-il à un chevron-right ?

        Un chevron simple (flèche) est UN SEUL sous-chemin court commençant
        à x faible (côté gauche du viewBox 24x24). Les icônes complexes
        (drapeau, étoile, logo…) contiennent plusieurs sous-chemins et
        partent généralement de x élevé.

        Filtres cumulés (tout doit passer) :
         1. path non vide
         2. 1 seul sous-chemin (un seul `M`/`m`)
         3. longueur ≤ 180 (les chevrons réels font 40-150 chars)
         4. commence par M suivi d'un x entre 2 et 14 (côté gauche)
         5. n'est PAS une croix X (zéro ou un passage par le centre)
            → une croix a deux traits qui se croisent à (12,12)
              et produisent typiquement ≥ 2 occurrences de "L12 "

        Rejette : icônes multi-formes (drapeau), icônes compactes mais
        bord-à-bord (croix de fermeture), etc., qui partagent la classe
        `iconify--global-exam`.
        """
        if not d:
            return False
        d = d.strip()

        # 2) Un seul sous-chemin
        moves = len(re.findall(r'[Mm]', d))
        if moves > 1:
            return False

        # 3) Longueur raisonnable — les chevrons simples sont courts
        # (chevron-right observé : 109 chars ; croix X observée : 228 chars)
        if len(d) > 180:
            return False

        # 4) Départ côté gauche du viewBox (x0 ≤ 14)
        m = re.match(r'^M\s*([\d.]+)', d)
        if not m:
            return False
        try:
            x0 = float(m.group(1))
        except ValueError:
            return False
        if x0 < 2 or x0 > 14:
            return False

        # 5) Anti-croix : un chevron ne passe pas deux fois par le centre.
        # La croix X observée a "L12 13.34" + "L12 10.47" = 2 passages par (12, *)
        center_crossings = d.count('L12 ') + d.count('L12.')
        if center_crossings >= 2:
            return False

        return True

    def _find_stock_pagination_arrow(self):
        """
        Trouve la flèche « suivant » de pagination du stock d'options.

        La classe `iconify--global-exam` est partagée par TOUTES les icônes
        du site (drapeau de fin, validation, Next, etc.) — donc on valide :
         1. le path du SVG ressemble à un chevron-right simple
         2. le bouton cliquable parent n'est PAS un bouton de navigation
            (Next/Continue/Submit qui aurait un chevron en décoration)

        Retourne le parent cliquable ou None.
        """
        try:
            svgs = self.driver.find_elements(
                By.XPATH,
                "//*[name()='svg' and contains(@class, "
                "'iconify--global-exam')]",
            )
        except WebDriverException:
            return None

        # Mots-clés indiquant un bouton de navigation (à rejeter absolument)
        NAV_KEYWORDS = (
            "next", "suivant", "continue", "continuer",
            "finish", "terminer", "submit", "valider", "validate",
            "skip", "passer", "check", "vérifier",
            "siguiente", "terminar", "enviar", "saltar", "verificar",
            "weiter", "beenden", "absenden",
            "avanti", "fine", "invia",
            "próximo", "terminar", "enviar", "pular",
        )

        visible = []
        for svg in svgs:
            if not safe_is_displayed(svg):
                continue

            # 1) Validation du path : chevron-right ?
            try:
                d = self.driver.execute_script(
                    "var p = arguments[0].querySelector('path'); "
                    "return p ? (p.getAttribute('d') || '') : '';",
                    svg,
                ) or ""
            except (JavascriptException, WebDriverException,
                    StaleElementReferenceException):
                d = ""
            if not self._is_chevron_right_path(d):
                continue

            clickable = self._find_clickable_ancestor(svg)
            if clickable is None:
                continue

            # 2) Cliquable non désactivé
            if safe_attr(clickable, "disabled"):
                continue
            aria_disabled = safe_attr(clickable, "aria-disabled").lower()
            if aria_disabled in ("true", "1"):
                continue

            # 3) ANTI-NEXT : rejeter les boutons qui contiennent du TEXTE
            # significatif. Une flèche de pagination pure est un bouton-
            # icône sans label visible. Un bouton "Next" a généralement
            # "Next" / "Suivant" / "Continue" en texte à côté du chevron.
            parent_text = safe_text(clickable).strip()
            if parent_text:
                # Plus d'un caractère → probablement bouton de nav
                if len(parent_text) > 1:
                    continue
                # Un seul caractère : si c'est une lettre d'un mot de nav
                # c'est encore suspect. On tolère les chiffres uniquement.
                if not parent_text.isdigit():
                    continue

            # 4) ANTI-NEXT via aria-label : les boutons Next ont souvent
            # aria-label="Next", "Submit answer", etc.
            aria_label = safe_attr(clickable, "aria-label").lower().strip()
            if aria_label:
                if any(kw in aria_label for kw in NAV_KEYWORDS):
                    continue

            # 5) ANTI-NEXT via title (tooltip HTML classique)
            title = safe_attr(clickable, "title").lower().strip()
            if title and any(kw in title for kw in NAV_KEYWORDS):
                continue

            # 6) ANTI-NEXT via classes connues de boutons de validation
            btn_cls = safe_attr(clickable, "class").lower()
            if any(cls_kw in btn_cls for cls_kw in (
                "button-solid-primary", "btn-primary", "btn-next",
                "btn-submit", "btn-validate", "btn-finish",
                "submit-button", "validate-button", "next-button",
            )):
                continue

            visible.append(clickable)

        if not visible:
            return None

        # S'il y a plusieurs flèches (gauche + droite), on prend celle la
        # PLUS À DROITE (x max) — c'est le « next » par convention.
        try:
            visible.sort(
                key=lambda c: safe_location(c).get("x", 0) or 0,
                reverse=True,
            )
        except Exception:
            pass
        return visible[0]

    # =================================================================
    #  SCAN + CAPTURE COMBINÉ AVEC PAGINATION DU STOCK
    # =================================================================
    def _scan_and_capture(
        self, max_pages: int = 8,
    ) -> tuple[list, "Image.Image", list]:
        """
        Remplace le bloc scroll + scan + capture dans run().

        Si une flèche iconify--global-exam apparaît en bas du stock (= trop
        d'options pour tenir à l'écran), on la clique plusieurs fois, en :
         - capturant un screenshot à chaque page
         - accumulant les nouveaux textes du stock
        Les screenshots sont ensuite concaténés verticalement pour que
        Gemini voie VRAIMENT tous les éléments d'un coup.

        ⚠️ Après cette méthode, le stock reste à la DERNIÈRE page. La
        méthode _find_element_or_paginate gère le retour au bon élément
        pendant l'exécution des actions.
        """
        self._scroll_horizontal_containers()

        code_options = self._get_code_options()
        image_options = self._get_image_options()

        self._scroll_top()
        if not code_options:
            code_options = self._get_code_options()

        # ── RETRY si incohérence avec les badges numérotés ──────────
        # Si _get_code_options détecte que le nombre de mots détectés ne
        # correspond pas au max des badges numérotés (1..N), on attend
        # un peu que le DOM se stabilise et on relance UNE FOIS.
        try:
            badges = self.driver.execute_script(
                JS_SCAN_NUMBER_BADGES,
            ) or []
            if badges:
                expected = max(badges)
                if (expected >= 3
                        and len(code_options) > 0
                        and expected != len(code_options)):
                    # Nb mots attendu ≠ nb mots détecté : on retente
                    print(f"   🔄 Retry scan : {expected} badge(s) "
                          f"mais {len(code_options)} mot(s) détecté(s)")
                    time.sleep(0.8)
                    retry_opts = self._get_code_options()
                    if len(retry_opts) > len(code_options):
                        print(f"   ✅ Retry : "
                              f"{len(code_options)} → {len(retry_opts)}")
                        code_options = retry_opts
        except (JavascriptException, WebDriverException):
            pass

        screenshots = [self._capture()]

        seen_keys = {norm_key(o) for o in code_options}
        pages_done = 0

        for page in range(max_pages):
            arrow = self._find_stock_pagination_arrow()
            if arrow is None:
                break

            # Premier signal trouvé → log explicite
            if page == 0:
                print("   ➡️ Flèche de pagination du stock détectée — "
                      "exploration...")

            try:
                self._scroll_into_view(arrow)
                if not self._ghost_click(arrow):
                    print("   ⚠️ Clic pagination échoué")
                    break
                time.sleep(0.5)
            except Exception as e:
                print(f"   ⚠️ Pagination : {str(e)[:60]}")
                break

            # Nouveau scan : on ajoute les textes inédits
            new_opts = self._get_code_options()
            added = 0
            for opt in new_opts:
                k = norm_key(opt)
                if k not in seen_keys:
                    seen_keys.add(k)
                    code_options.append(opt)
                    added += 1
            # Les nouvelles images révélées sont aussi utiles (rare mais possible)
            new_imgs = self._get_image_options()
            existing_ids = {id(im) for im in image_options}
            for im in new_imgs:
                if id(im) not in existing_ids:
                    image_options.append(im)
                    existing_ids.add(id(im))

            # Capture de la nouvelle page
            screenshots.append(self._capture())
            pages_done = page + 1

            if added == 0:
                print(f"   ℹ️ Page {page+2} : rien de nouveau → fin de pagination")
                break
            print(f"   📖 Page {page+2} : +{added} élément(s) "
                  f"(total {len(code_options)})")

        if pages_done > 0:
            print(f"   📚 Pagination terminée : {pages_done+1} page(s) "
                  f"combinée(s) en un screenshot unique")

        img = self._combine_images_vertical(screenshots)
        return code_options, img, image_options

    def _find_element_or_paginate(self, text: str, max_pages: int = 8):
        """
        Cherche un élément par son texte. Si introuvable à la page courante
        ET qu'une flèche de pagination existe, on clique + retente.

        Après pagination complète pendant le scan, on se retrouve à la
        DERNIÈRE page ; cette méthode permet de continuer à cycler pour
        retrouver un élément de page antérieure (si le site fait du
        wrap-around) ou des pages suivantes.
        """
        el = self._find_element_by_text(text)
        if el is not None:
            return el

        # Pas trouvé → tente la pagination
        for page in range(max_pages):
            arrow = self._find_stock_pagination_arrow()
            if arrow is None:
                return None
            try:
                if not self._ghost_click(arrow):
                    return None
                time.sleep(0.4)
            except Exception:
                return None
            el = self._find_element_by_text(text)
            if el is not None:
                print(f"   📖 '{text[:30]}' trouvé après {page+1} "
                      f"pagination(s)")
                return el
        return None

    # =================================================================
    #  EXTRACTION DU STOCK D'OPTIONS
    # =================================================================
    def _in_result_zone(self, el) -> bool:
        """Vérifie si un élément est dans une zone de résultat/feedback."""
        try:
            return bool(self.driver.execute_script(JS_IN_RESULT_ZONE, el))
        except (JavascriptException, WebDriverException,
                StaleElementReferenceException):
            return False

    # JS : l'élément a-t-il un ancêtre INTERACTIF proche (option d'exo)
    # plutôt que d'être un simple mot en gras dans un paragraphe de prose ?
    JS_HAS_INTERACTIVE_ANCESTOR = r"""
    var e = arguments[0];
    var depth = 0;
    while (e && depth < 6) {
        var tag = (e.tagName || '').toLowerCase();
        var role = (e.getAttribute && e.getAttribute('role')) || '';
        var cls = (e.className || '').toString().toLowerCase();
        if (tag === 'label' || tag === 'button' || role === 'button' ||
            e.getAttribute && e.getAttribute('draggable') === 'true' ||
            cls.indexOf('draggable') >= 0 ||
            cls.indexOf('answer-option') >= 0 ||
            cls.indexOf('cursor-pointer') >= 0) {
            return true;
        }
        e = e.parentElement; depth++;
    }
    return false;
    """

    def _has_interactive_ancestor(self, el) -> bool:
        """
        True si l'élément est (dans) une option cliquable/draggable.
        Sert à écarter les mots en GRAS de la prose (dialogue/texte de
        lecture affiché à côté de l'exercice) qui ne sont PAS des options.
        """
        try:
            return bool(self.driver.execute_script(
                self.JS_HAS_INTERACTIVE_ANCESTOR, el,
            ))
        except (JavascriptException, WebDriverException,
                StaleElementReferenceException):
            # En cas de doute, on garde l'élément (comportement antérieur)
            return True

    # -----------------------------------------------------------------
    #  QCM MULTI-RÉPONSES (checkboxes / "One or many answers possible")
    # -----------------------------------------------------------------
    # Indices textuels d'un QCM à réponses multiples, en 6 langues.
    MULTI_SELECT_HINTS = [
        "one or many answers", "one or more answers",
        "multiple answers", "select all that apply",
        "plusieurs réponses", "une ou plusieurs réponses",
        "varias respuestas", "una o varias respuestas",
        "mehrere antworten", "uma ou mais respostas",
        "várias respostas", "più risposte", "una o più risposte",
    ]

    def _is_multi_select_qcm(self) -> bool:
        """
        Détermine si le QCM courant autorise PLUSIEURS réponses :
          a) présence de cases à cocher visibles (input[type=checkbox]),
             qui par nature autorisent la sélection multiple, OU
          b) une mention type "One or many answers possible" sur la page.
        """
        # a) Cases à cocher visibles ?
        try:
            has_checkbox = bool(self.driver.execute_script(
                """
                const cbs = document.querySelectorAll('input[type="checkbox"]');
                for (const c of cbs) {
                    const s = getComputedStyle(c);
                    if (s.display === 'none' || s.visibility === 'hidden')
                        continue;
                    // checkbox visible OU avec un label visible
                    if (c.id) {
                        let lbl = null;
                        try { lbl = document.querySelector(
                            'label[for="' + CSS.escape(c.id) + '"]'); }
                        catch(e) {}
                        if (lbl) {
                            const r = lbl.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) return true;
                        }
                    }
                    const r = c.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return true;
                }
                return false;
                """
            ))
        except WebDriverException:
            has_checkbox = False
        if has_checkbox:
            return True

        # b) Mention "une ou plusieurs réponses" dans le texte de la page
        page_low = self._get_page_text().lower()
        return any(h in page_low for h in self.MULTI_SELECT_HINTS)

    def _get_code_options(self) -> list[str]:
        """
        Récupère la liste des options/textes cliquables/draggables.
        Stratégies superposées :
         0) JS : scan tous les draggables (y compris hors-viewport proche)
         1) JS : scan radios/checkboxes → texte du label
         2) Selenium : sélecteurs CSS + labels + dédoublonnage parent/enfant
        """
        options: list[str] = []
        seen_texts: set[str] = set()
        # Verrou « lettres » : une fois le scan JS (stratégie 0, source
        # fiable des tuiles) passé, on n'ajoute plus de single-char via les
        # stratégies suivantes → évite de compter 2× les mêmes lettres (bug
        # observé : 4 lettres épelées comptées 8×). Les répétitions
        # LÉGITIMES d'une même lettre (ex. « LETTER ») restent gardées : elles
        # viennent toutes de la stratégie 0, AVANT la pose du verrou.
        single_locked = [False]

        def _add(txt: str, allow_dup: bool = False) -> bool:
            txt = fix_encoding(txt)
            if not txt or len(txt) > 500:
                return False
            if is_ui_noise(txt):
                return False
            is_single = len(txt) == 1
            # single-char : dédupliqués UNIQUEMENT une fois le verrou posé
            # (après la stratégie 0) ; avant, on garde tout.
            if is_single and single_locked[0]:
                return False
            if not is_single and txt in seen_texts and not allow_dup:
                return False
            seen_texts.add(txt)
            options.append(txt)
            return True

        # ── STRATÉGIE 0 : scan JS draggables ─────────────────────────
        try:
            draggable_texts = self.driver.execute_script(JS_SCAN_DRAGGABLES) or []
            for t in draggable_texts:
                # Le scan JS renvoie chaque tuile UNE fois : un doublon de
                # texte = vraie 2e tuile (« it … it »)
                _add(t, allow_dup=True)
            # Le scan JS est la source fiable des tuiles (lettres comprises,
            # avec leurs répétitions). Dès qu'il a fourni ≥ 2 lettres seules,
            # on verrouille → les stratégies suivantes ne les redoublent pas.
            # Le scan JS voit les VRAIES tuiles (doublons légitimes
            # compris) : dès qu'il en a trouvé, les stratégies suivantes
            # ne doivent pas re-compter une tuile d'une lettre (bug : un
            # « y » compté 2 fois → phrase fausse avec deux « y »).
            if options:
                single_locked[0] = True
            if options:
                preview = options[:3]
                print(f"   🕵️ Stock draggable JS ({len(options)}) : {preview}...")
        except (JavascriptException, WebDriverException) as e:
            print(f"   ⚠️ Scan draggable JS : {str(e)[:80]}")

        # ── STRATÉGIE 1 : radios/checkboxes ──────────────────────────
        try:
            radio_texts = self.driver.execute_script(JS_SCAN_RADIO_LABELS) or []
            if radio_texts:
                before = len(options)
                for t in radio_texts:
                    _add(t)
                if len(options) > before:
                    print(f"   🕵️ Stock QCM radio ({len(options)}) : {options}")
        except (JavascriptException, WebDriverException):
            pass

        # ── STRATÉGIE 2 : sélecteurs CSS + dédup parent/enfant ───────
        selectors = [
            ".draggable-item span", ".answer-option span", "span.font-medium",
            ".draggable-item", ".answer-option",
            "[draggable='true']", "[class*='draggable']",
            "[class*='sortable-item']", "[class*='drag-item']",
            "[class*='reorder']", "[class*='ordering-item']",
            "label.group", "label[class*='cursor-pointer']",
        ]
        seen_ids: set = set()
        all_els: list = []
        try:
            for sel in selectors:
                try:
                    found = self.driver.find_elements(By.CSS_SELECTOR, sel)
                except WebDriverException:
                    continue
                for el in found:
                    eid = id(el)  # identité Python stable
                    if eid in seen_ids:
                        continue
                    txt = fix_encoding(safe_text(el))
                    if not txt or len(txt) > 500:
                        continue
                    if is_ui_noise(txt):
                        continue
                    if self._in_result_zone(el):
                        continue
                    # ── ANTI-POLLUTION PAR LA PROSE ──────────────────
                    # Le sélecteur 'span.font-medium' est large : il
                    # attrape aussi les mots en GRAS d'un dialogue/texte
                    # de lecture affiché à côté de l'exercice (ex : last,
                    # supplier, refined...). Ces mots ne sont PAS des
                    # options. On ne les garde QUE s'ils ont un ancêtre
                    # interactif (label/button/draggable = vraie option).
                    if (sel == "span.font-medium"
                            and not self._has_interactive_ancestor(el)):
                        continue
                    is_single = len(txt) == 1
                    if not is_single and txt in seen_texts:
                        continue
                    all_els.append(el)
                    seen_ids.add(eid)
                    seen_texts.add(txt)

            # Dédoublonnage parent/enfant
            for i, e1 in enumerate(all_els):
                is_parent = False
                for j, e2 in enumerate(all_els):
                    if i == j:
                        continue
                    try:
                        if self.driver.execute_script(
                            "return arguments[0].contains(arguments[1]);",
                            e1, e2,
                        ):
                            is_parent = True
                            break
                    except (JavascriptException, StaleElementReferenceException,
                            WebDriverException):
                        continue
                if not is_parent:
                    _add(safe_text(e1))
        except Exception as e:
            print(f"   ⚠️ Scan CSS : {str(e)[:80]}")

        # ── HEURISTIQUE POST-SCAN : nettoyage anagramme ──────────────
        # Si le stock est majoritairement single-char (anagramme de lettres),
        # les éléments multi-mots sont forcément des RÉSIDUS d'exos précédents
        # (textes de QCM cachés, phrases de l'écran d'avant, etc.).
        # On les retire pour que le solveur Python ait les bonnes lettres.
        single_count = sum(1 for o in options if len(o) == 1)
        if options and single_count >= 3 and single_count / len(options) >= 0.7:
            multi = [o for o in options if len(o) > 1]
            if multi:
                options = [o for o in options if len(o) == 1]
                preview = [m[:40] + "..." if len(m) > 40 else m for m in multi]
                print(f"   🧹 Mode anagramme détecté ({single_count} lettres) "
                      f"→ {len(multi)} résidu(s) retiré(s) : {preview}")

        # ── VALIDATION + COMPLÉMENT PAR BADGES NUMÉROTÉS ─────────────
        # Exercices "Organize the words" numérotent chaque mot (1..N).
        # Si on détecte N badges mais M < N éléments dans le stock,
        # on UTILISE le scan des badges pour RÉCUPÉRER les mots manquants
        # (stratégie visuelle/géométrique, indépendante des classes CSS).
        if single_count / max(len(options), 1) < 0.7:  # pas un anagramme
            try:
                badged = self.driver.execute_script(
                    JS_SCAN_BADGED_WORDS,
                ) or []
                if badged:
                    expected = max(b.get("n", 0) for b in badged if b)
                    if expected >= 3 and expected != len(options):
                        # Incohérence détectée → essayer de compléter
                        # le stock via les mots associés aux badges
                        seen_lower = {norm_key(o) for o in options}
                        added_via_badges: list[str] = []
                        # Trier par numéro pour que l'ordre soit stable
                        badged_sorted = sorted(
                            [b for b in badged if b and b.get("text")],
                            key=lambda b: b.get("n", 0),
                        )
                        for b in badged_sorted:
                            t = (b.get("text") or "").strip()
                            t = fix_encoding(t)
                            if not t or len(t) > 200:
                                continue
                            if is_ui_noise(t):
                                continue
                            k = norm_key(t)
                            if k in seen_lower:
                                continue
                            seen_lower.add(k)
                            options.append(t)
                            added_via_badges.append(t)
                        if added_via_badges:
                            print(f"   🎯 Complétion via badges numérotés : "
                                  f"+{len(added_via_badges)} mot(s) récupéré(s) "
                                  f": {added_via_badges}")
                        elif expected != len(options):
                            # Scan badge a fonctionné mais n'a pas
                            # trouvé de texte associé → warning
                            print(f"   ⚠️ Badges numérotés : max={expected}, "
                                  f"stock={len(options)} → "
                                  f"{expected - len(options)} élément(s) "
                                  f"manquant(s) (non récupérables)")
            except (JavascriptException, WebDriverException):
                pass

        if options:
            print(f"   🕵️ Stock ({len(options)}) : {options}")
        return options

    def _exercise_has_image(self) -> bool:
        """True si une image de CONTENU d'exercice est visible.

        Sert à la vision conditionnelle : on n'envoie la capture à Gemini
        que si elle apporte quelque chose. Les images de contenu viennent
        de .../media/… (photo d'un mot, anagramme, image-choix) ; les
        icônes décoratives (.../assets/…, petits SVG) sont ignorées.
        """
        try:
            return bool(self.driver.execute_script(r"""
                var imgs = document.querySelectorAll('img');
                for (var i = 0; i < imgs.length; i++) {
                    var im = imgs[i];
                    var r = im.getBoundingClientRect();
                    if (r.width < 40 || r.height < 40) continue;
                    var s = (im.getAttribute('src') || '');
                    if (s.indexOf('/media/') >= 0) return true;
                }
                return false;
            """))
        except WebDriverException:
            return False

    def _get_drop_zones(self) -> list:
        """
        Retourne les zones de drop détectées dans le DOM.

        STRATÉGIE DE DÉDOUBLONNAGE PARENT/ENFANT :
        Sur beaucoup de sites, la zone de drop est imbriquée :
           <div class="custom-dashed-border ... p-[34px]">   ← parent décoratif
              <div class="drop-zone ordering-drop-zone"       ← VRAIE zone
                   data-name="user-answer-container">
                 <!---->
              </div>
           </div>
        Les deux matchent nos sélecteurs → on se retrouve avec 2 zones
        pour un seul "vrai" drop-target. Le parent a des coordonnées
        plus larges (avec padding) et fait rater le positionnement en
        mode accumulateur (on calcule à partir du grand cadre au lieu
        de la zone flex réelle).

        On préfère donc :
         - les éléments avec `data-name="user-answer-container"` (signal
           le plus fort)
         - à défaut, les éléments qui NE contiennent PAS un autre
           candidat zone (on garde les feuilles, pas les wrappers)
        """
        try:
            xpath = (
                "//span[@data-name='user-answer-container'] | "
                "//div[@data-name='user-answer-container'] | "
                "//*[@data-user-answer-list] | "
                "//div[contains(@class, 'ordering-drop-zone')] | "
                "//span[contains(@class, 'drop-zone')] | "
                "//div[contains(@class, 'drop-zone')] | "
                "//div[contains(@class, 'border-dashed')] | "
                "//div[contains(@class, 'droppable')] | "
                "//div[contains(@class, 'sortable-placeholder')] | "
                "//div[contains(@class, 'drag-placeholder')] | "
                "//li[contains(@class, 'drop')] | "
                "//*[@data-droppable='true'] | "
                "//*[contains(@class,'drop-target')]"
            )
            raw_zones = []
            for z in self.driver.find_elements(By.XPATH, xpath):
                try:
                    # On accepte si :
                    #  - data-name est défini (même chaîne vide OK :
                    #    l'attribut existe → vraie zone marquée)
                    #  - OU classe 'ordering-drop-zone' / 'drop-zone'
                    #    (signal sémantique de zone, même si taille 0
                    #    parce que vide au moment du scan)
                    #  - OU dimensions > 0
                    has_data_name = z.get_attribute("data-name") is not None
                    cls = safe_attr(z, "class").lower()
                    is_marked_zone = (
                        "ordering-drop-zone" in cls
                        or "drop-zone" in cls
                    )
                    # ── EXCLUSION DU RÉSERVOIR D'OPTIONS ───────────────
                    # Le conteneur qui héberge les tuiles à glisser
                    # (fuera/egoísta/traición...) est souvent lui-même un
                    # 'droppable'/'sortable' → il était compté comme une
                    # 2ᵉ « zone » alors que ce n'est PAS une case à
                    # remplir. Une vraie case est VIDE ; le réservoir
                    # contient PLUSIEURS tuiles draggables. On rejette donc
                    # un candidat qui contient ≥ 2 tuiles draggables, sauf
                    # s'il porte le signal fort data-name (jamais le
                    # réservoir).
                    if not has_data_name:
                        try:
                            n_drag = self.driver.execute_script(
                                "return arguments[0].querySelectorAll("
                                "\"[draggable='true'],[class*='draggable'],"
                                "[class*='answer-option']\").length;",
                                z,
                            )
                        except (JavascriptException, WebDriverException):
                            n_drag = 0
                        if (n_drag or 0) >= 2:
                            continue
                    if has_data_name or is_marked_zone:
                        raw_zones.append(z)
                        continue
                    # Zone NON marquée (ni data-name, ni classe drop-zone) :
                    # on exige une taille RÉELLEMENT rendue, largeur ET
                    # hauteur > 0, mesurée par getBoundingClientRect. Bug
                    # observé : un <div> 'border-dashed' de 0×0 (template /
                    # aperçu de drag caché) passait via Selenium .size (qui
                    # renvoie la taille CSS h-10 alors que l'élément n'est
                    # pas rendu) et était compté comme une 2ᵉ zone.
                    try:
                        rect = self.driver.execute_script(
                            "var r=arguments[0].getBoundingClientRect();"
                            "return [r.width, r.height];",
                            z,
                        )
                        rw = (rect or [0, 0])[0] or 0
                        rh = (rect or [0, 0])[1] or 0
                    except (JavascriptException, WebDriverException):
                        rw = rh = 0
                    if rw > 0 and rh > 0:
                        raw_zones.append(z)
                except Exception:
                    continue

            # Dédoublonnage parent/enfant : on enlève un élément si un
            # AUTRE élément de la liste est son descendant (= on garde
            # la vraie zone interne, pas son wrapper décoratif)
            zones = []
            for i, z in enumerate(raw_zones):
                has_inner_zone = False
                for j, other in enumerate(raw_zones):
                    if i == j:
                        continue
                    try:
                        contains = self.driver.execute_script(
                            "return arguments[0].contains(arguments[1]);",
                            z, other,
                        )
                        if contains:
                            has_inner_zone = True
                            break
                    except (JavascriptException,
                            StaleElementReferenceException,
                            WebDriverException):
                        continue
                if not has_inner_zone:
                    zones.append(z)

            # Log uniquement si le compte CHANGE (évite le spam)
            new_count = len(zones)
            prev_count = getattr(self, "_last_zones_count", None)
            if zones and new_count != prev_count:
                dropped = len(raw_zones) - new_count
                extra = (f" ({dropped} wrapper(s) ignoré(s))"
                         if dropped > 0 else "")
                print(f"   🎯 Zones drop : {new_count}{extra}")
                self._last_zones_count = new_count

            # ── DIAGNOSTIC zones (≥2) : décrit chaque zone retenue pour
            # cibler un sur-comptage (réservoir compté comme cible). Imprimé
            # dès qu'il y a ≥2 zones, dédupliqué par signature pour ne pas
            # spammer à chaque cycle sur le même écran.
            if new_count >= 2:
                try:
                    sig_parts = []
                    for zz in zones:
                        sig_parts.append(
                            (safe_attr(zz, "class") or "")[:40]
                            + "|" + (zz.get_attribute("data-name") or ""))
                    sig = (new_count, tuple(sig_parts))
                except WebDriverException:
                    sig = (new_count, ())
                if sig != getattr(self, "_last_zones_diag_sig", None):
                    self._last_zones_diag_sig = sig
                    print(f"   🔬 DIAGNOSTIC {new_count} zones détectées :")
                    for k, zz in enumerate(zones):
                        try:
                            info = self.driver.execute_script(
                                "var e=arguments[0];"
                                "var r=e.getBoundingClientRect();"
                                "return {tag:e.tagName,"
                                "cls:(e.className||'').toString().slice(0,90),"
                                "dn:e.getAttribute('data-name'),"
                                "w:Math.round(r.width),h:Math.round(r.height),"
                                "nchild:e.children.length,"
                                "ndrag:e.querySelectorAll("
                                "\"[draggable='true'],[class*='draggable'],"
                                "[class*='answer-option'],"
                                "[class*='cursor-pointer']\").length,"
                                "txt:(e.textContent||'').trim().slice(0,50)};",
                                zz,
                            )
                            print(f"      ▸ zone[{k}] "
                                  f"<{str(info.get('tag','')).lower()}> "
                                  f"data-name={info.get('dn')!r} "
                                  f"w×h={info.get('w')}×{info.get('h')} "
                                  f"enfants={info.get('nchild')} "
                                  f"draggables={info.get('ndrag')} "
                                  f"cls={info.get('cls','')!r} "
                                  f"txt={info.get('txt','')!r}")
                        except (JavascriptException, WebDriverException):
                            pass
            return zones
        except WebDriverException:
            return []

    def _is_zone_empty(self, zone) -> bool:
        """
        Heuristique fiable : une drop-zone est "vide" si :
         - aucune classe marquant un état rempli (filled/answered/complete…)
         - textContent insignifiant (vide ou seulement des _ . … espaces)
         - aucun enfant draggable déjà inséré
         - zone encore affichée dans le DOM
        Par défaut (en cas d'erreur), on considère la zone PLEINE pour
        éviter de drag dans une zone douteuse.
        """
        try:
            if not safe_is_displayed(zone):
                return False
            cls = safe_attr(zone, "class").lower()
            for kw in ("filled", "answered", "complete",
                       "has-answer", "answer-given"):
                if kw in cls:
                    return False
            # Texte contenu : si c'est juste un placeholder → zone vide
            txt = safe_text(zone)
            if txt:
                stripped = re.sub(r'[\s_\-\.\u2026]+', '', txt)
                if len(stripped) > 0:
                    return False
            # Présence d'un enfant draggable déjà placé
            try:
                has_child = self.driver.execute_script(
                    "return !!arguments[0].querySelector("
                    "'.draggable-item, [data-draggable-item-id], "
                    "[draggable=\"true\"]');",
                    zone,
                )
                if has_child:
                    return False
            except (JavascriptException, StaleElementReferenceException,
                    WebDriverException):
                pass
            return True
        except (StaleElementReferenceException, WebDriverException):
            return False

    def _is_draggable_already_placed(self, text: str) -> bool:
        """True si une tuile draggable portant ce texte est DÉJÀ dans une
        zone de réponse (placée). Sert à NE PAS re-glisser une tuile déjà
        posée : son élément d'origine dans le réservoir devient taille
        zéro → le drag échoue (elementFromPoint tombe sur le texte de
        fond) et la complétion reste bloquée à 2/6 en boucle."""
        try:
            return bool(self.driver.execute_script(
                """
                const target = (arguments[0]||'').trim().toLowerCase()
                    .replace(/\\s+/g, ' ');
                const zones = Array.from(document.querySelectorAll(
                    '[data-name="user-answer-container"], '
                    + '.ordering-drop-zone, .drop-zone'));
                for (const z of zones) {
                    const items = z.querySelectorAll(
                        '[data-draggable-item-id], .draggable-item');
                    for (const it of items) {
                        const t = (it.innerText || '').trim()
                            .toLowerCase().replace(/\\s+/g, ' ');
                        if (t && t === target) return true;
                    }
                }
                return false;
                """,
                text,
            ))
        except (JavascriptException, WebDriverException):
            return False

    def _get_empty_drop_zones(self) -> list:
        """Rescanne le DOM et renvoie UNIQUEMENT les drop-zones vides."""
        all_zones = self._get_drop_zones()
        if not all_zones:
            return []
        empty = [z for z in all_zones if self._is_zone_empty(z)]
        # Log uniquement si le compte CHANGE (évite le spam)
        key = (len(empty), len(all_zones))
        prev = getattr(self, "_last_empty_zones_ratio", None)
        if empty and len(empty) != len(all_zones) and key != prev:
            print(f"   🎯 Zones vides : {len(empty)}/{len(all_zones)}")
            self._last_empty_zones_ratio = key
        return empty

    def _get_image_options(self) -> list:
        """Liste des <img> exercice : filtrage logos, nav, icônes."""
        valid = []
        try:
            imgs = self.driver.find_elements(By.TAG_NAME, "img")
        except WebDriverException:
            return []

        for img in imgs:
            try:
                if not safe_is_displayed(img):
                    continue
                rect = img.rect or {}
                h = rect.get("height", 0) or 0
                w = rect.get("width", 0) or 0
                y = safe_location(img).get("y", 0) or 0
                src = safe_attr(img, "src").lower()

                if h < 80 or w < 80:
                    continue
                if y < 150:
                    continue
                skip_kw = (
                    "logo", "avatar", "flag", "drapeau", "icon", "badge",
                    "laurel", "crown", "trophy", "medal", "sprite",
                    "button", "arrow", "chevron", "nav", "header",
                    "profile", "user", "account", ".svg",
                )
                if any(k in src for k in skip_kw):
                    continue
                ratio = w / max(h, 1)
                if ratio > 4 or ratio < 0.25:
                    continue
                valid.append(img)
            except (StaleElementReferenceException, WebDriverException):
                continue

        # Tri top→bottom, left→right (lignes approx.)
        try:
            valid.sort(key=lambda x: (
                int((safe_location(x).get("y", 0) or 0) / 50),
                safe_location(x).get("x", 0) or 0,
            ))
        except Exception:
            pass

        if valid:
            print(f"   🖼️ Images exercice : {len(valid)}")
        return valid

    def _get_text_inputs(self) -> list:
        try:
            elts = self.driver.find_elements(
                By.XPATH,
                "//input[@type='text'] | //textarea | //input[not(@type)]",
            )
            return [
                i for i in elts
                if safe_is_displayed(i) and self._is_enabled_safe(i)
            ]
        except WebDriverException:
            return []

    @staticmethod
    def _is_enabled_safe(element) -> bool:
        try:
            return bool(element.is_enabled())
        except (StaleElementReferenceException, WebDriverException):
            return False

    def _enable_disabled_text_input(self) -> bool:
        """Active un champ texte désactivé (exo flashcard « écris la réponse »).

        L'input est `disabled` tant que l'audio n'a pas été joué. On clique
        le(s) haut-parleur(s), on attend, et en dernier recours on retire
        l'attribut `disabled` en JS. Renvoie True si un champ texte ACTIF est
        ensuite disponible. No-op (False) s'il n'y a aucun input désactivé.
        """
        if self._get_text_inputs():
            return True
        try:
            disabled_inputs = [
                i for i in self.driver.find_elements(
                    By.XPATH, "//input[@type='text'] | //textarea")
                if safe_is_displayed(i) and not self._is_enabled_safe(i)]
        except WebDriverException:
            disabled_inputs = []
        if not disabled_inputs:
            return False
        # 1) Cliquer le(s) haut-parleur(s) → déclenche l'activation du champ
        try:
            megs = [m for m in self.driver.find_elements(
                By.CSS_SELECTOR, 'svg[data-icon="megaphone"]')
                if safe_is_displayed(m)]
            for m in megs:
                try:
                    mb = m.find_element(By.XPATH, "./ancestor::button[1]")
                except (NoSuchElementException, WebDriverException):
                    mb = m
                try:
                    self._ghost_click(mb)
                except WebDriverException:
                    pass
            if megs:
                print("   🔊 Écoute de l'audio (active le champ texte)")
                time.sleep(1.8)
        except WebDriverException:
            pass
        if self._get_text_inputs():
            return True
        # 2) Dernier recours : forcer l'activation en JS
        try:
            for i in disabled_inputs:
                self.driver.execute_script(
                    "arguments[0].removeAttribute('disabled');"
                    "arguments[0].disabled = false;", i)
            print("   🔧 Champ texte forcé actif (JS)")
        except WebDriverException:
            pass
        return bool(self._get_text_inputs())

    # =================================================================
    #  INTERACTIONS : CLIC & DRAG FIABILISÉS
    # =================================================================
    def _scroll_into_view(self, element) -> None:
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", element,
            )
            time.sleep(0.1)
        except (JavascriptException, StaleElementReferenceException,
                WebDriverException):
            pass

    def _ghost_click(self, element) -> bool:
        """
        Tente plusieurs stratégies pour cliquer un élément.
        Retourne True si un clic a été émis, False sinon.
        Gère spécialement le cas radio/checkbox via label[for=id].
        """
        if element is None:
            return False

        self._scroll_into_view(element)
        tag = safe_tag(element)

        # ── LABEL / RADIO ─────────────────────────────────────────────
        label = None
        try:
            if tag == "label":
                label = element
            elif tag in ("span", "div", "p", "li"):
                el = element
                for _ in range(4):
                    try:
                        el = el.find_element(By.XPATH, "./..")
                    except (NoSuchElementException,
                            StaleElementReferenceException):
                        break
                    if safe_tag(el) == "label":
                        label = el
                        break
        except Exception:
            pass

        if label is not None:
            for_id = safe_attr(label, "for")

            # A) clic JS direct sur l'input radio / checkbox
            if for_id:
                try:
                    inp = self.driver.find_element(By.ID, for_id)
                    input_type = (safe_attr(inp, "type") or "").lower()

                    if input_type == "checkbox":
                        # CHECKBOX (QCM multi-réponses) : c'est un élément
                        # À BASCULE. Surtout NE PAS faire "inp.checked=true"
                        # puis dispatcher un click (double-toggle → la case
                        # se décocherait). On COCHE seulement si pas déjà
                        # cochée, via un vrai inp.click() qui bascule l'état
                        # ET notifie le framework (React/Vue).
                        already = bool(self.driver.execute_script(
                            "return arguments[0].checked;", inp,
                        ))
                        if already:
                            print(f"   ✅ Checkbox déjà cochée #{for_id}")
                            return True
                        self.driver.execute_script(
                            """
                            var inp = arguments[0];
                            inp.dispatchEvent(new MouseEvent('mousedown',
                                {bubbles:true, cancelable:true}));
                            inp.dispatchEvent(new MouseEvent('mouseup',
                                {bubbles:true, cancelable:true}));
                            inp.click();   // bascule natif + events framework
                            inp.dispatchEvent(new Event('change', {bubbles:true}));
                            inp.dispatchEvent(new Event('input',  {bubbles:true}));
                            """, inp,
                        )
                        time.sleep(0.2)
                        now_checked = bool(self.driver.execute_script(
                            "return arguments[0].checked;", inp,
                        ))
                        if now_checked:
                            print(f"   ✅ Checkbox cochée via input#{for_id}")
                            return True
                        # Sinon : on laisse tomber sur le clic label (B/C)
                        print(f"   ⚠️ Checkbox non cochée via input — "
                              f"fallback label")
                    else:
                        # RADIO (ou input simple) : sélection unique, pas
                        # de risque de bascule. checked=true + click OK.
                        self.driver.execute_script(
                            """
                            var inp = arguments[0];
                            inp.checked = true;
                            inp.dispatchEvent(new MouseEvent('click',
                                {bubbles:true, cancelable:true}));
                            inp.dispatchEvent(new Event('change', {bubbles:true}));
                            inp.dispatchEvent(new Event('input',  {bubbles:true}));
                            """, inp,
                        )
                        time.sleep(0.2)
                        print(f"   ✅ Radio cliqué via input#{for_id}")
                        return True
                except (NoSuchElementException, JavascriptException,
                        WebDriverException):
                    pass

            # B) ActionChains sur le label
            try:
                ActionChains(self.driver).move_to_element(label).click().perform()
                time.sleep(0.2)
                print("   ✅ Label cliqué via ActionChains")
                return True
            except (ElementNotInteractableException,
                    StaleElementReferenceException, WebDriverException):
                pass

            # C) JS click sur le label
            try:
                self.driver.execute_script(
                    """
                    var lbl = arguments[0];
                    lbl.dispatchEvent(new MouseEvent('mousedown', {bubbles:true,cancelable:true}));
                    lbl.dispatchEvent(new MouseEvent('mouseup',   {bubbles:true,cancelable:true}));
                    lbl.dispatchEvent(new MouseEvent('click',     {bubbles:true,cancelable:true}));
                    """, label,
                )
                time.sleep(0.2)
                print("   ✅ Label cliqué via JS events")
                return True
            except (JavascriptException, WebDriverException):
                pass

        # ── STRATÉGIE GÉNÉRALE ────────────────────────────────────────
        # A) element.click() natif Selenium — le plus robuste : vise
        #    l'élément directement (pas ses coordonnées), Selenium
        #    s'occupe du scroll et du timing. Fonctionne sur <button>
        #    même si le framework Vue/React a récemment bougé l'élément.
        try:
            element.click()
            time.sleep(0.15)
            return True
        except (ElementClickInterceptedException,
                ElementNotInteractableException,
                StaleElementReferenceException, WebDriverException):
            pass

        # B) ActionChains (fallback si element.click() refuse : overlay,
        #    élément déplacé, etc.)
        try:
            ActionChains(self.driver).move_to_element(element).click().perform()
            time.sleep(0.15)
            return True
        except (ElementNotInteractableException,
                StaleElementReferenceException, WebDriverException):
            pass

        # C) Dernière carte : JS click + parents
        try:
            self.driver.execute_script("arguments[0].click();", element)
            try:
                parent = element.find_element(By.XPATH, "./..")
                self.driver.execute_script("arguments[0].click();", parent)
                try:
                    gparent = parent.find_element(By.XPATH, "./..")
                    self.driver.execute_script("arguments[0].click();", gparent)
                except (NoSuchElementException,
                        StaleElementReferenceException, WebDriverException):
                    pass
            except (NoSuchElementException,
                    StaleElementReferenceException, WebDriverException):
                pass
            return True
        except (JavascriptException, StaleElementReferenceException,
                WebDriverException):
            return False

    def _ghost_drag(self, source, target) -> bool:
        """
        Drag & drop CLASSIQUE (N zones → N items).

        Sur ce site, TOUS les exercices (drag-drop, reorder, anagramme)
        fonctionnent par CLIC, pas par drag. Cliquer sur un
        `<button class="draggable-item">` suffit : le site place
        l'élément dans la 1ère zone vide (ou dans la zone de réponse
        pour les accumulateurs).

        Le paramètre `target` est conservé pour compatibilité de
        signature mais n'est plus utilisé (on ne cible pas une zone
        spécifique, on laisse le framework choisir).

        CDP est la seule méthode qui déclenche le framework du site.
        Mais le re-layout du stock entre deux drags déplace les
        boutons restants → si on capture les coords trop tôt, on
        clique au mauvais endroit. Solution : récupérer les coords
        via `getBoundingClientRect()` JS JUSTE avant d'émettre les
        events, et vérifier que `elementFromPoint()` retourne bien
        l'élément attendu (ou un de ses descendants).

        Cascade :
         1) CDP pointerdown/up avec coords fraîches + vérif elementFromPoint
         2) element.click() Selenium natif (fallback)
         3) _ghost_click standard (dernier recours)
        """
        if source is None:
            return False

        # Scroll + délai pour que le layout se stabilise APRÈS le
        # drag précédent
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center', "
                "behavior:'instant'});",
                source,
            )
            time.sleep(0.3)  # laisse le framework finir sa re-animation
        except (JavascriptException, WebDriverException,
                StaleElementReferenceException):
            pass

        # STRATÉGIE 1 : CDP avec coords FRESH + vérification cible
        try:
            # Récupère rect + vérifie que elementFromPoint au centre
            # pointe bien vers source (ou un de ses descendants).
            # Retourne {cx, cy, ok} où ok = True si la cible matche.
            info = self.driver.execute_script(
                """
                const el = arguments[0];
                const r = el.getBoundingClientRect();
                const cx = r.left + r.width / 2;
                const cy = r.top + r.height / 2;
                const hit = document.elementFromPoint(cx, cy);
                const ok = hit === el || (hit && el.contains(hit));
                return {cx: cx, cy: cy, ok: ok,
                        hit_tag: hit ? hit.tagName : null,
                        hit_text: hit ? (hit.textContent || '').slice(0,40) : ''};
                """,
                source,
            )
            if info and info.get("ok"):
                cx = info["cx"]
                cy = info["cy"]
                self.driver.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseMoved", "x": cx, "y": cy,
                     "button": "none", "buttons": 0},
                )
                time.sleep(0.05)
                self.driver.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {"type": "mousePressed", "x": cx, "y": cy,
                     "button": "left", "buttons": 1, "clickCount": 1},
                )
                time.sleep(0.08)
                self.driver.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseReleased", "x": cx, "y": cy,
                     "button": "left", "buttons": 0, "clickCount": 1},
                )
                time.sleep(0.4)
                print("   ✅ Drag classique via CDP pointerdown/up")
                return True
            else:
                # Le point central ne correspond plus à notre source —
                # un autre élément le recouvre. On logge et on tombe
                # sur le fallback.
                hit_tag = info.get("hit_tag", "?") if info else "?"
                hit_txt = (info.get("hit_text", "") or "")[:30] if info else ""
                print(f"   ⚠️ CDP cible incorrecte (elementFromPoint = "
                      f"<{hit_tag}> '{hit_txt}') — fallback")
        except (JavascriptException, WebDriverException,
                StaleElementReferenceException) as e:
            print(f"   ⚠️ CDP click : {str(e)[:70]}")

        # STRATÉGIE 2 : element.click() natif (fallback)
        try:
            source.click()
            time.sleep(0.3)
            print("   ✅ Drag classique via element.click()")
            return True
        except (ElementClickInterceptedException,
                ElementNotInteractableException,
                StaleElementReferenceException, WebDriverException) as e:
            print(f"   ⚠️ element.click : {str(e)[:70]}")

        # STRATÉGIE 3 : _ghost_click classique
        try:
            return bool(self._ghost_click(source))
        except (WebDriverException, Exception) as e:
            print(f"   ⚠️ _ghost_click : {str(e)[:70]}")
            return False

    def _cdp_drag_at(self, source, zone, tgt_x: float,
                     tgt_y: float) -> bool:
        """
        Drag via Chrome DevTools Protocol (CDP).

        POURQUOI CDP : les frameworks JS modernes (React, Vue) vérifient
        souvent `event.isTrusted`. Les events dispatchés via JS
        (MouseEvent/PointerEvent synthétiques) ont `isTrusted=false`
        et sont IGNORÉS silencieusement par ces frameworks.

        CDP `Input.dispatchMouseEvent` envoie des events au niveau du
        navigateur Chrome, avec `isTrusted=true` — indistinguables
        d'une vraie souris utilisateur. C'est la SEULE façon fiable
        de drag avec un framework custom strict.

        Séquence émise :
         1) mousePressed (button=left) au centre de source
         2) mouseMoved progressif vers (tgt_x, tgt_y) en ~8 étapes
         3) mouseReleased à (tgt_x, tgt_y)
        """
        if source is None or zone is None:
            return False
        try:
            src_rect = source.rect  # {x, y, width, height}
            src_x = src_rect["x"] + src_rect["width"] / 2
            src_y = src_rect["y"] + src_rect["height"] / 2
        except (StaleElementReferenceException, WebDriverException) as e:
            print(f"   ⚠️ CDP drag : source rect KO : {str(e)[:60]}")
            return False

        def _cdp(event_type: str, x: float, y: float,
                 button: str = "none", click_count: int = 0):
            self.driver.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {
                    "type": event_type,
                    "x": x,
                    "y": y,
                    "button": button,
                    "buttons": 1 if button == "left" else 0,
                    "clickCount": click_count,
                    "pointerType": "mouse",
                },
            )

        try:
            # 1) Hover initial (certains frameworks exigent pointerover)
            _cdp("mouseMoved", src_x, src_y)
            time.sleep(0.08)
            # 2) Appui du bouton gauche sur la source
            _cdp("mousePressed", src_x, src_y, "left", 1)
            time.sleep(0.15)
            # 3) Mouvement progressif vers la cible (8 étapes)
            steps = 8
            for i in range(1, steps + 1):
                mx = src_x + (tgt_x - src_x) * i / steps
                my = src_y + (tgt_y - src_y) * i / steps
                _cdp("mouseMoved", mx, my, "left", 0)
                time.sleep(0.03)
            # 4) Pause sur la cible (laisse le framework enregistrer
            #    le dragover + accepter le drop)
            time.sleep(0.15)
            # 5) Relâchement du bouton
            _cdp("mouseReleased", tgt_x, tgt_y, "left", 1)
            time.sleep(0.25)
            return True
        except WebDriverException as e:
            print(f"   ⚠️ CDP drag exception : {str(e)[:80]}")
            return False

    def _ghost_drag_accumulator(self, source, zone) -> bool:
        """
        Mode ACCUMULATEUR (reorder_sentence, reorder_chronological,
        anagramme de lettres).

        Sur ce site, un clic sur un `<button class="draggable-item">`
        le déplace dans la zone de réponse. On imite un VRAI click
        utilisateur avec pointerdown + pointerup séparés (pattern
        attendu par les frameworks draggable custom qui peuvent
        ignorer un `click` natif trop rapide).

        FILTRE ANTI-RE-CLIC : si la cible est DÉJÀ dans la zone de
        réponse (donc déjà déposée), on refuse de cliquer —
        recliquer dessus la ferait REMONTER au stock (toggle), ce
        qui est l'opposé de ce qu'on veut. Cas observé sur anagramme
        avec lettres doublées (SUDDENLY : 2×D → au 2ème D, le
        sélecteur trouvait le D déjà placé).

        Cascade :
         1) pointerdown + pointerup CDP (isTrusted=true, séparés)
         2) element.click() Selenium natif (fallback)
         3) _ghost_click standard (dernier recours)
        """
        if source is None:
            return False

        # Diagnostic : qu'est-ce qu'on s'apprête à cliquer exactement ?
        try:
            tag = safe_tag(source)
            cls = safe_attr(source, "class")[:80]
            data_id = safe_attr(source, "data-draggable-item-id")
            print(f"   🎯 Cible : <{tag} class='{cls}' "
                  f"data-draggable-item-id='{data_id}'>")
        except (StaleElementReferenceException, WebDriverException):
            pass

        # ── FILTRE ANTI-RE-CLIC : la cible est-elle DÉJÀ dans la zone ?
        # Si oui, cliquer = toggle = retour au stock → refus.
        if zone is not None:
            try:
                is_inside = self.driver.execute_script(
                    "return arguments[0].contains(arguments[1]);",
                    zone, source,
                )
                if is_inside:
                    print("   ⚠️ Cible DÉJÀ dans la zone de réponse — "
                          "clic refusé (éviterait un toggle/retrait)")
                    return False
            except (JavascriptException, StaleElementReferenceException,
                    WebDriverException):
                pass

        # Scroll pour que l'élément soit VRAIMENT dans le viewport
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center', "
                "behavior:'instant'});",
                source,
            )
            time.sleep(0.3)  # laisse le framework finir sa re-animation
        except (JavascriptException, WebDriverException,
                StaleElementReferenceException):
            pass

        # STRATÉGIE 1 : CDP avec coords FRESH + vérification cible
        # Récupère le rect via JS (toujours à jour) et vérifie que
        # elementFromPoint au centre pointe bien vers source.
        try:
            info = self.driver.execute_script(
                """
                const el = arguments[0];
                const r = el.getBoundingClientRect();
                const cx = r.left + r.width / 2;
                const cy = r.top + r.height / 2;
                const hit = document.elementFromPoint(cx, cy);
                const ok = hit === el || (hit && el.contains(hit));
                return {cx: cx, cy: cy, ok: ok,
                        hit_tag: hit ? hit.tagName : null,
                        hit_text: hit ? (hit.textContent || '').slice(0,40) : ''};
                """,
                source,
            )
            if info and info.get("ok"):
                cx = info["cx"]
                cy = info["cy"]
                self.driver.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseMoved", "x": cx, "y": cy,
                     "button": "none", "buttons": 0},
                )
                time.sleep(0.05)
                self.driver.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {"type": "mousePressed", "x": cx, "y": cy,
                     "button": "left", "buttons": 1, "clickCount": 1},
                )
                time.sleep(0.08)
                self.driver.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseReleased", "x": cx, "y": cy,
                     "button": "left", "buttons": 0, "clickCount": 1},
                )
                time.sleep(0.4)
                # Vérifie que la tuile est bien partie dans la réponse
                # (sinon un clic « perdu » décale tout l'ordre)
                placed = True
                if zone is not None:
                    for _ in range(3):
                        try:
                            placed = bool(self.driver.execute_script(
                                "return !document.contains(arguments[1]) || "
                                "arguments[0].contains(arguments[1]);",
                                zone, source))
                        except StaleElementReferenceException:
                            placed = True
                        except WebDriverException:
                            placed = True
                        if placed:
                            break
                        time.sleep(0.3)
                if placed:
                    print("   ✅ Clic accumulateur via CDP pointerdown/up")
                    return True
                print("   🔁 Tuile pas déplacée → nouveau clic")
            else:
                hit_tag = info.get("hit_tag", "?") if info else "?"
                hit_txt = (info.get("hit_text", "") or "")[:30] if info else ""
                print(f"   ⚠️ CDP cible incorrecte (elementFromPoint = "
                      f"<{hit_tag}> '{hit_txt}') — fallback")
        except (JavascriptException, WebDriverException,
                StaleElementReferenceException) as e:
            print(f"   ⚠️ CDP click : {str(e)[:70]}")

        # STRATÉGIE 2 : element.click() natif
        try:
            source.click()
            time.sleep(0.3)
            print("   ✅ Clic accumulateur via element.click()")
            return True
        except (ElementClickInterceptedException,
                ElementNotInteractableException,
                StaleElementReferenceException, WebDriverException) as e:
            print(f"   ⚠️ element.click : {str(e)[:70]}")

        # STRATÉGIE 3 : _ghost_click classique
        try:
            return bool(self._ghost_click(source))
        except (WebDriverException, Exception) as e:
            print(f"   ⚠️ _ghost_click : {str(e)[:70]}")
            return False

    # =================================================================
    #  APPEL GEMINI : rate limiter + rotation clés + retry
    # =================================================================
    def _get_aux_model(self, model_name: str):
        """
        Retourne (en cache) une instance GenerativeModel pour un modèle
        secondaire (ex: Flash-Lite pour l'arbitrage). Construite une seule
        fois puis réutilisée. Même clé API que le modèle principal. None
        si la construction échoue (l'appelant retombe alors sur Flash).
        """
        cache = getattr(self, "_aux_models", None)
        if cache is None:
            cache = {}
            self._aux_models = cache
        if model_name not in cache:
            try:
                cache[model_name] = _make_model(model_name)
            except Exception as e:
                print(f"   ⚠️ Modèle secondaire '{model_name}' indispo "
                      f"({str(e)[:50]}) → repli sur {MODEL_NAME}")
                return None
        return cache[model_name]

    def _call_gemini(self, prompt: str, image=None,
                     max_retries: int | None = None,
                     temperature: float = 0.0,
                     model_name: str | None = None) -> str | None:
        """
        Appelle Gemini avec rate-limit 4 appels/min et retries
        sur erreurs transitoires (429, 500, 503). Retourne le
        texte brut ou None si toutes les tentatives échouent.

        model_name : si fourni (ex: MODEL_ARBITRAGE = Flash-Lite),
        l'appel utilise ce modèle MOINS CHER le temps de CET appel
        seulement. L'image et le prompt (avec textes mémorisés) sont
        transmis normalement → vision + contexte préservés. Le prochain
        appel sans model_name repart automatiquement sur self.model
        (Flash). Repli sur Flash si le modèle secondaire échoue à
        s'initialiser.
        """
        if max_retries is None:
            max_retries = 3
        active_label = model_name or MODEL_NAME
        # Marqueur VISIBLE dans les logs : dès qu'on utilise un modèle
        # différent de Flash (vérifications, arbitrage…), on l'affiche en
        # clair entre crochets → facile à repérer/grep dans les logs.
        if model_name and model_name != MODEL_NAME:
            print(f"   🪶 ↪ modèle léger utilisé : [{active_label}]")

        for attempt in range(max_retries):
            now = time.time()

            # --- Purge des timestamps > 60s ---
            self._call_timestamps = [
                t for t in self._call_timestamps if now - t < 60
            ]

            # --- Clé AQ. (quota gratuit 5-15/min) : appels ESPACÉS
            # régulièrement (60 s / limite) plutôt qu'en rafale, pour ne
            # jamais dépasser la limite par minute de Google.
            if _REST_KEY and self._call_timestamps:
                gap = 60.0 / max(1, RPM_PER_KEY)
                since = now - self._call_timestamps[-1]
                if since < gap:
                    time.sleep(gap - since)
                    now = time.time()

            # --- Rate-limit : RPM_PER_KEY appels max / minute ---
            if len(self._call_timestamps) >= RPM_PER_KEY:
                wait = 60 - (now - self._call_timestamps[0]) + 1
                wait = max(wait, 1)
                wait = min(wait, 65)
                print(f"   ⏳ Rate-limit atteint → attente {wait:.0f}s...")
                time.sleep(wait)
                self._call_timestamps = [
                    t for t in self._call_timestamps if time.time() - t < 60
                ]

            # --- Garde-fou : pas de modèle → réessayer d'init ---
            if self.model is None:
                try:
                    if not _REST_KEY:
                        genai.configure(**(_GENAI_CONF
                                           or dict(api_key=API_KEYS[0])))
                    self.model = _make_model(MODEL_NAME)
                except Exception:
                    time.sleep(2)
                    continue

            # --- Sélection du modèle actif ---
            # Par défaut self.model (Flash). Si model_name est fourni
            # (ex: arbitrage Flash-Lite), on utilise un modèle secondaire
            # mis en cache, le temps de CET appel uniquement.
            if model_name and model_name != MODEL_NAME:
                active_model = self._get_aux_model(model_name)
                if active_model is None:        # init échouée → repli Flash
                    active_model = self.model
                    active_label = MODEL_NAME
            else:
                active_model = self.model

            # --- Appel proprement dit ---
            try:
                content = [prompt] if image is None else [prompt, image]
                self._call_timestamps.append(time.time())
                resp = active_model.generate_content(
                    content,
                    generation_config=genai.types.GenerationConfig(
                        temperature=temperature,
                        # gemini-2.5-flash consomme beaucoup de tokens
                        # en "thinking" interne avant d'émettre le texte
                        # visible. On monte à 12000 (+ format "actions"
                        # avant "raisonnement" + raisonnement court) pour
                        # garantir que le tableau "actions" sorte AVANT
                        # toute coupure de budget.
                        max_output_tokens=12000,
                    ),
                    # Timeout pour éviter un blocage infini sur un appel
                    # Gemini qui ne répond pas (quota saturé côté Google,
                    # incident temporaire, etc.). Sans ce garde-fou,
                    # generate_content peut rester pendu 5+ minutes.
                    # 90s (et non 45) : les anagrammes avec image font
                    # beaucoup de "thinking" interne et dépassaient 45s
                    # → 504. Plafond seulement : les appels rapides
                    # (QCM, drag) ne sont pas ralentis.
                    request_options={"timeout": 90},
                )
                # ── MESURE : tokens réellement consommés + coût estimé ──
                # usage_metadata est renvoyé par l'API ; on cumule sur la
                # session et on affiche une ligne compacte (la sortie =
                # candidates + thinking, c'est la partie chère). But :
                # voir d'un coup d'œil quel TYPE d'exo (ligne au-dessus)
                # coûte le plus, pour cibler les optimisations.
                try:
                    um = getattr(resp, "usage_metadata", None)
                    if um is not None:
                        t_in = int(getattr(um, "prompt_token_count", 0) or 0)
                        t_out = int(getattr(um,
                                    "candidates_token_count", 0) or 0)
                        t_think = int(getattr(um,
                                      "thoughts_token_count", 0) or 0)
                        # La bibliothèque google-generativeai embarquée ne
                        # connaît pas « thoughts_token_count » → toujours 0.
                        # Le total, lui, inclut la réflexion : on la déduit
                        # (sinon le coût affiché est largement sous-estimé).
                        t_total = int(getattr(um,
                                      "total_token_count", 0) or 0)
                        if not t_think and t_total > t_in + t_out:
                            t_think = t_total - t_in - t_out
                        p_in, p_out = PRICING.get(active_label,
                                                  PRICING[MODEL_NAME])
                        cost = (t_in * p_in
                                + (t_out + t_think) * p_out) / 1_000_000
                        self._usage["calls"] += 1
                        self._usage["in"] += t_in
                        self._usage["out"] += t_out
                        self._usage["think"] += t_think
                        self._usage["cost_usd"] += cost
                        tot = self._usage["cost_usd"]
                        print(f"   📊 Tokens : in={t_in} out={t_out} "
                              f"think={t_think} | session : "
                              f"{self._usage['calls']} appels ≈ "
                              f"{tot * USD_TO_EUR:.4f}€ (${tot:.4f})")
                except Exception:
                    pass
                raw = (resp.text or "").strip() if resp else ""
                if raw:
                    return raw
                # Réponse vide : retry
                print("   ⚠️ Réponse vide, retry...")
                time.sleep(1)

            except Exception as e:
                err = str(e).lower()
                _msg = re.search(r'"message":\s*"([^"]+)"', str(e))
                print(f"   ⚠️ [{active_label}] #{attempt+1} : "
                      f"{(_msg.group(1) if _msg else ' '.join(str(e).split()))[:200]}")
                if any(kw in err for kw in ("429", "resourceexhausted", "quota")):
                    # Quota atteint → attendre que la fenêtre RPM se
                    # libère. On marque 4 timestamps pour forcer
                    # l'attente au prochain tour.
                    self._call_timestamps = [time.time()] * RPM_PER_KEY
                    if (("perday" in err.replace(" ", "") or "per day" in err)
                            and model_name and model_name != MODEL_NAME):
                        # Quota épuisé sur un modèle d'appoint (ex. Flash
                        # pour les images) → on retombe sur le principal
                        print(f"   🔁 Quota du jour épuisé sur {active_label} "
                              f"→ repli sur {MODEL_NAME}")
                        model_name = None
                        active_label = MODEL_NAME
                        self._call_timestamps = []
                        continue
                    if "perday" in err.replace(" ", "") or "per day" in err:
                        nxt = _next_aq_model() if _REST_KEY else None
                        if nxt:
                            print(f"   🔁 Quota du jour épuisé sur "
                                  f"{active_label} → relais sur {nxt}")
                            self.model = _make_model(nxt)
                            self._aux_models = {}
                            self._call_timestamps = []
                            model_name = None
                            active_label = nxt
                            continue
                        print("   ⛔ Quota GRATUIT du jour épuisé sur tous "
                              "les modèles (remis à zéro vers 9 h, heure de "
                              "Paris). Arrête le bot ou passe sur « Clé "
                              "fournie ».")
                    print("   ⏸️  Quota atteint → attente 60s")
                    time.sleep(60)
                elif any(kw in err for kw in ("504", "deadline", "timed out",
                                              "timeout")):
                    # Timeout / 504 : l'appel a mis trop de temps (souvent
                    # un anagramme difficile = beaucoup de "thinking", ou
                    # surcharge temporaire côté Google). Backoff progressif
                    # (3s, 6s, 9s) pour laisser le serveur respirer plutôt
                    # que de le re-solliciter aussitôt.
                    wait = 3 * (attempt + 1)
                    print(f"   ⏳ Timeout/504 → backoff {wait}s")
                    time.sleep(wait)
                elif any(kw in err for kw in ("500", "internal", "503",
                                              "unavailable")):
                    # Erreur serveur temporaire
                    time.sleep(3)
                else:
                    # Autre erreur : petit backoff
                    time.sleep(1)

        return None

    # =================================================================
    #  PARSING ROBUSTE DE LA RÉPONSE IA
    # =================================================================
    @staticmethod
    def _extract_json(text: str) -> dict | None:
        if not text:
            return None
        clean = text.replace("```json", "").replace("```", "").strip()

        # Objet {...}
        s, e = clean.find("{"), clean.rfind("}")
        if s != -1 and e > s:
            try:
                obj = json.loads(clean[s:e+1])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass

        # Liste [...]
        s, e = clean.find("["), clean.rfind("]")
        if s != -1 and e > s:
            try:
                arr = json.loads(clean[s:e+1])
                if isinstance(arr, list):
                    return {"actions": arr}
            except json.JSONDecodeError:
                pass

        # SALVAGE : réponse TRONQUÉE (budget de tokens épuisé pendant le
        # raisonnement). Le JSON complet n'est pas parsable, mais si le
        # tableau "actions" a été émis AVANT la coupure (d'où le format qui
        # demande actions EN PREMIER), on le récupère par regex.
        m = re.search(r'"actions"\s*:\s*(\[[^\]]*\])', clean)
        if m:
            try:
                arr = json.loads(m.group(1))
                if isinstance(arr, list) and arr:
                    res: dict = {"actions": arr}
                    tm = re.search(r'"type"\s*:\s*"([^"]+)"', clean)
                    if tm:
                        res["type"] = tm.group(1)
                    print("   🩹 JSON tronqué → tableau 'actions' "
                          "récupéré par salvage")
                    return res
            except json.JSONDecodeError:
                pass

        return None

    @staticmethod
    def _normalize_actions_list(raw_actions) -> list[str]:
        """
        Normalise une liste d'actions venant de Gemini.

        Gemini peut parfois retourner des objets imbriqués au lieu
        de simples strings, ex: `[{"blank": 1, "word": "may have been"},
        {"blank": 2, "word": "must have had"}]`. Sans normalisation,
        un `str()` produit `"{'blank': 1, 'word': 'may have been'}"`
        qui ne matche aucun bouton du stock → "non trouvé".

        On gère trois cas :
         - string : on garde telle quelle
         - dict avec champ 'word'/'text'/'answer'/'value'/'content' :
           on extrait ce champ
         - autre (nombre, etc.) : on convertit via str() en dernier recours

        Retourne toujours une liste de strings (éventuellement vide).
        """
        if not isinstance(raw_actions, list):
            return []
        out: list[str] = []
        for a in raw_actions:
            if isinstance(a, str):
                out.append(a)
            elif isinstance(a, dict):
                for key in ("word", "text", "answer", "value", "content"):
                    v = a.get(key)
                    if isinstance(v, str) and v.strip():
                        out.append(v)
                        break
                # dict sans champ exploitable → on ignore silencieusement
            elif a is not None:
                s = str(a).strip()
                if s and not s.startswith("{"):
                    out.append(s)
        return out

    def _fallback_match(self, text: str,
                        known_options: list[str]) -> dict | None:
        """Si le JSON est inparsable, cherche les options dans le brut."""
        if not known_options:
            return None
        lower = text.lower()
        found = []
        for opt in known_options:
            pos = lower.find(opt.lower())
            if pos != -1:
                found.append((pos, opt))
        if found:
            found.sort(key=lambda x: x[0])
            return {"actions": [o for _, o in found]}

        words = re.findall(r"[a-záéíóúüñàèìòùâêîôûäëïöüß]+", lower)
        fuzzy: list[str] = []
        for opt in known_options:
            for w in words:
                if difflib.SequenceMatcher(None, opt.lower(), w).ratio() > 0.8:
                    if opt not in fuzzy:
                        fuzzy.append(opt)
                    break
        if fuzzy:
            return {"actions": fuzzy}

        for nav in ("Next", "Continue", "Move on", "Suivant"):
            if nav.lower() in lower:
                return {"actions": [nav]}
        return None

    # =================================================================
    #  VALIDATORS PYTHON (filet de sécurité après IA)
    # =================================================================
    def _validate_actions_python(
        self, actions: list[str], code_options: list[str],
        exercise_type: str, nb_dz: int, is_anagram: bool,
        allow_multi: bool = False,
    ) -> tuple[list[str], list[str]]:
        """
        Règles :
         - Reorder : TOUS les éléments du stock obligatoires
         - Drag drop : len(actions) == nb_dz
         - QCM : exactement 1 réponse
         - Skip : jamais autorisé
        """
        problems: list[str] = []
        actions = list(actions or [])

        # -- Filtre Skip + Validate : ligne de défense finale --
        # IMPORTANT : on EXCLUT du filtre les mots qui sont présents
        # dans le stock scanné (code_options). Certains mots comme
        # "check", "finish", "submit" peuvent être à la fois :
        #  - des étiquettes de boutons de l'UI (à filtrer)
        #  - des MOTS LÉGITIMES d'un exo reorder_sentence (à garder)
        # Si le mot fait partie du stock, c'est un mot d'exo → on garde.
        stock_keys = {norm_key(o) for o in (code_options or [])}
        filtered = [
            a for a in actions
            if norm_key(a) in stock_keys
            or (not is_skip_word(a) and not is_validate_button_word(a))
        ]
        if len(filtered) != len(actions):
            dropped = [a for a in actions
                       if norm_key(a) not in stock_keys
                       and (is_skip_word(a) or is_validate_button_word(a))]
            if dropped:
                problems.append(f"Skip/Validate retirés : {dropped}")
        actions = filtered

        if not actions:
            return actions, problems + ["Liste d'actions vide après filtrage"]

        is_drag = exercise_type in (
            "drag_drop", "reorder_sentence", "reorder_chronological",
        ) or nb_dz > 0

        # -- 1. Capitalisation (jamais pour drag/reorder/anagramme) --
        # UNIQUEMENT si le trou est en DÉBUT de phrase. Avant, on mettait
        # une majuscule systématiquement → 'You sound ___' devenait
        # 'Misled' (faux) alors que le mot au milieu d'une phrase doit
        # rester en minuscule. On regarde le texte juste avant le champ.
        if (not is_anagram and not is_drag and actions and actions[0]
                and len(actions[0]) > 1 and actions[0][0].islower()):
            before = self._text_before_first_blank()
            at_sentence_start = (
                before is not None
                and (before == "" or before[-1] in ".!?:;")
            )
            if at_sentence_start:
                actions[0] = actions[0][0].upper() + actions[0][1:]
                problems.append(
                    f"Capitalisation (début de phrase) → '{actions[0]}'")

        # -- 2. Exhaustivité stricte pour reorder --
        if code_options and not is_anagram and exercise_type != "drag_drop":
            a_norm = [norm_key(a) for a in actions]
            o_norm = [norm_key(o) for o in code_options]
            is_reorder = (
                exercise_type in ("reorder_sentence", "reorder_chronological")
                or (len(code_options) > 1 and nb_dz == 0
                    and exercise_type not in (
                        "qcm", "image_choice", "navigation",
                        "carousel", "transcript", "text_input",
                    ))
            )
            if is_reorder:
                missing = [o for o in code_options if norm_key(o) not in a_norm]
                extra   = [a for a in actions
                           if norm_key(a) not in o_norm
                           and a not in ("Next", "Suivant",
                                         "Continue", "Continuer")]
                if missing:
                    problems.append(f"REORDER_INCOMPLET:manquants={missing}")
                    print(f"   ⚠️ Manquants : {missing}")
                if extra:
                    # ⚠️ PRUDENCE : si l'IA ajoute UN mot qui n'est PAS
                    # dans le stock scanné, c'est soit :
                    #  (a) une hallucination de l'IA → retirer le mot
                    #  (b) un mot que le scan DOM a RATÉ (bug HTML/
                    #      framework) → garder le mot car l'IA voit
                    #      l'image et a raison
                    #
                    # Heuristique : un mot "en trop" est probablement
                    # VRAI (vu dans l'image) si :
                    #   - c'est un mot court (≤ 12 chars) — typique
                    #     des reorder ; les hallucinations sont rarement
                    #     des mots courts ponctuels
                    #   - il n'y a qu'UN SEUL extra (ajout isolé ≠
                    #     hallucination en cascade)
                    #   - aucun des éléments du stock n'est manquant
                    #     (l'IA a utilisé TOUT le stock + ajouté 1)
                    ia_probably_right = (
                        len(extra) == 1
                        and len(extra[0]) <= 12
                        and not missing  # tout le stock est utilisé
                    )
                    # Le scan du stock a pu rater une tuile (hors écran
                    # dans un carrousel…) : si le texte existe bien sur
                    # une tuile de la page, on le garde.
                    if not ia_probably_right:
                        try:
                            in_dom = self.driver.execute_script("""
                                const want = arguments[0].map(
                                    t => t.replace(/\\s+/g, ' ').trim());
                                const got = new Set([...document
                                    .querySelectorAll('[data-draggable-item-id],'
                                        + '.draggable-item,[data-sortable-item]')]
                                    .map(e => (e.textContent || '')
                                        .replace(/\\s+/g, ' ').trim()));
                                return want.every(w => got.has(w));
                            """, [str(x) for x in extra])
                        except Exception:  # noqa: BLE001
                            in_dom = False
                        if in_dom:
                            ia_probably_right = True
                    if ia_probably_right:
                        problems.append(
                            f"En trop CONSERVÉ (stock probablement "
                            f"incomplet) : {extra}"
                        )
                        print(f"   🛟 '{extra[0]}' conservé : "
                              f"stock scanné probablement incomplet, "
                              f"l'IA voit l'image et l'a inclus")
                    else:
                        actions = [a for a in actions
                                   if norm_key(a) in o_norm
                                   or a in ("Next", "Suivant",
                                            "Continue", "Continuer")]
                        problems.append(f"En trop retiré : {extra}")

        # -- 3. Drag drop : len(actions) == nb_dz --
        if exercise_type == "drag_drop" and nb_dz > 0 and code_options:
            if len(actions) > nb_dz:
                actions = actions[:nb_dz]
                problems.append(f"Tronqué à {nb_dz}")
            elif len(actions) < nb_dz:
                problems.append(f"DRAG_INCOMPLET:{len(actions)}/{nb_dz} trous")
                print(f"   ⚠️ Drag incomplet : {len(actions)}/{nb_dz} trous")

            # -- 3a. BIJECTION : si #tuiles == #trous, chaque tuile sert
            # exactement une fois. Si une tuile est répétée et une autre
            # omise, on répare : on remplace l'occurrence EN TROP (la
            # dernière) par la tuile manquante. Filet pour le cas où les
            # deux passes sont d'accord sur un doublon (l'arbitrage ne les
            # départage pas).
            if len(code_options) == nb_dz and len(actions) == nb_dz:
                stock_norm = [norm_key(o) for o in code_options]
                act_norm = [norm_key(a) for a in actions]
                from collections import Counter
                missing = [o for o, on in zip(code_options, stock_norm)
                           if on not in act_norm]
                counts = Counter(act_norm)
                dups = [k for k, c in counts.items() if c > 1]
                if missing and dups:
                    # Remplace la DERNIÈRE occurrence de chaque doublon par
                    # une tuile manquante (ordre d'apparition).
                    miss_iter = list(missing)
                    for i in range(len(actions) - 1, -1, -1):
                        if not miss_iter:
                            break
                        k = norm_key(actions[i])
                        if counts.get(k, 0) > 1:
                            repl = miss_iter.pop(0)
                            problems.append(
                                f"BIJECTION : doublon '{actions[i]}' "
                                f"(pos {i+1}) → '{repl}'")
                            print(f"   🔧 Bijection : '{actions[i]}' en "
                                  f"double → '{repl}'")
                            actions[i] = repl
                            counts[k] -= 1

        # -- 3b. QCM : exactement 1 réponse (sauf multi-réponses) --
        # Si la page autorise plusieurs réponses (checkboxes / mention
        # "One or many answers possible"), on NE tronque PAS.
        if exercise_type == "qcm" and len(actions) > 1 and not allow_multi:
            print(f"   ⚠️ QCM : {len(actions)} actions → tronqué à 1")
            actions = actions[:1]
            problems.append("QCM tronqué à 1")
        elif exercise_type == "qcm" and len(actions) > 1 and allow_multi:
            print(f"   ☑️  QCM multi-réponses : {len(actions)} réponses "
                  f"conservées")

        # -- 4. Normalisation : remet les textes EXACTS du stock --
        # Pour les reorder, le stock peut contenir plusieurs variantes
        # du même mot avec différences de :
        #   - casse : ['you', 'You'] (début/milieu de phrase)
        #   - ponctuation : ['fire', 'fire,'] (fin de proposition vs fin)
        #   - les deux : ['the', 'The.']
        # On consomme chaque variante UNE SEULE FOIS pour garantir que
        # toutes les variantes du stock sont utilisées (chacune correspond
        # à un slot différent dans la phrase). Sans cette consommation,
        # un Gemini qui retourne 'fire' deux fois cliquerait 2× le même
        # bouton, laissant 'fire,' inutilisé.
        if code_options and not is_anagram:
            # Groupes de variantes par norm_key (multiset)
            variants_by_key: dict[str, list[str]] = {}
            for o in code_options:
                k = norm_key(o)
                variants_by_key.setdefault(k, []).append(o)

            # Compteur des variantes RESTANTES (sera décrémenté à
            # chaque consommation pour éviter de prendre 2× le même)
            from collections import Counter
            remaining: Counter = Counter(code_options)

            def _starts_new_sentence(idx: int, current_actions: list) -> bool:
                """
                True si l'élément à `idx` est en début de phrase :
                soit position 0, soit après un mot terminant par
                une ponctuation forte (!, ?, .).
                """
                if idx == 0:
                    return True
                if idx - 1 < len(current_actions):
                    prev = current_actions[idx - 1]
                    if prev and prev.rstrip()[-1:] in (".", "!", "?"):
                        return True
                return False

            def _pick_variant(key: str, position: int,
                              current: list, fallback: str) -> str:
                """
                Choisit la variante du stock adaptée à la position
                ET disponible (non encore consommée).

                Priorités, par ordre de préférence :
                 1. Si début de phrase (pos 0 ou après ./!/?) :
                    variante CAPITALISÉE encore disponible
                 2. Sinon : variante MINUSCULE encore disponible
                 3. Si aucune préférée disponible : n'importe quelle
                    variante encore disponible
                 4. Si tout est consommé : fallback (le texte AI
                    original, sera repéré comme manquant en aval)

                Sous-règle ponctuation : à position de fin de phrase
                (avant un mot terminant par ! ? .), on préfère la
                variante AVEC ponctuation (ex: 'fire,'). Ailleurs,
                la variante SANS ponctuation.
                """
                vars_ = variants_by_key.get(key, [])
                # Filtrer aux variantes non consommées
                avail = [v for v in vars_ if remaining.get(v, 0) > 0]
                if not avail:
                    return fallback

                # Détection : début de phrase ?
                start = _starts_new_sentence(position, current)

                # Détection : fin de proposition (le prochain mot
                # commence par une majuscule = nouvelle phrase à venir,
                # OU on est à la dernière position) → préfère ponctué
                # Pour rester simple, on regarde juste si la variante
                # ponctuée existe et qu'on est avancé dans la phrase.

                cap_avail = [v for v in avail if v and v[0].isupper()]
                low_avail = [v for v in avail if v and v[0].islower()]
                punct_avail = [v for v in avail
                               if v and v.rstrip()[-1:] in ",.!?;:"]
                no_punct_avail = [v for v in avail
                                  if v and v.rstrip()[-1:] not in ",.!?;:"]

                # Logique combinée
                if start:
                    # Début de phrase : majuscule, sans ponctuation
                    pref = [v for v in cap_avail if v in no_punct_avail]
                    if pref:
                        return pref[0]
                    if cap_avail:
                        return cap_avail[0]
                else:
                    # Milieu de phrase : minuscule
                    # Si le fallback (ce que l'IA a écrit) a une
                    # ponctuation, on respecte sa préférence
                    fallback_has_punct = (
                        fallback and fallback.rstrip()[-1:] in ",.!?;:"
                    )
                    if fallback_has_punct:
                        pref = [v for v in low_avail if v in punct_avail]
                        if pref:
                            return pref[0]
                    pref = [v for v in low_avail if v in no_punct_avail]
                    if pref:
                        return pref[0]
                    if low_avail:
                        return low_avail[0]

                # Sinon : n'importe quelle variante restante
                return avail[0]

            corrected: list[str] = []
            for idx, a in enumerate(actions):
                key = norm_key(a)
                if key in variants_by_key:
                    chosen = _pick_variant(key, idx, corrected, a)
                    # Décrément du compteur de variantes restantes
                    if remaining.get(chosen, 0) > 0:
                        remaining[chosen] -= 1
                    if chosen != a:
                        problems.append(
                            f"Variante : '{a}' → '{chosen}' (pos {idx})",
                        )
                    corrected.append(chosen)
                elif a in ("Next", "Suivant", "Continue", "Continuer"):
                    corrected.append(a)
                else:
                    best = difflib.get_close_matches(
                        a, code_options, n=1, cutoff=self.FUZZY_MATCH_CUTOFF,
                    )
                    if best and best[0] != a:
                        problems.append(f"Corrigé : '{a}'→'{best[0]}'")
                        corrected.append(best[0])
                    else:
                        corrected.append(a)
            actions = corrected

        # -- 5. RÈGLE MAJUSCULE = PREMIÈRE POSITION (TRÈS restreinte) ---
        # Cas légitime : stock ['you','can','We',...] où le SEUL mot
        # capitalisé est un PRONOM SUJET ("We") → il ouvre la phrase.
        # ⚠️ NE PAS forcer pour un nom commun/propre capitalisé : sur
        # Site, "Embassy", "Parliament", "Jack"... sont capitalisés
        # mais peuvent être OBJET/complément en milieu/fin de phrase
        # (bug observé : "Embassy" déplacé à tort en 1er, alors que la
        # phrase est "...contact my Embassy."). On ne force donc QUE si le
        # mot capitalisé est un pronom sujet reconnu, et hors question.
        SUBJECT_PRONOUNS_CAP = {
            # EN
            "We", "You", "They", "He", "She", "It",
            # FR
            "Je", "Tu", "Il", "Elle", "Nous", "Vous", "Ils", "Elles", "On",
            # ES
            "Yo", "Él", "Ella", "Nosotros", "Vosotros", "Ellos", "Ellas",
            "Usted", "Ustedes",
            # DE
            "Ich", "Du", "Er", "Wir", "Ihr",
            # PT
            "Eu", "Ele", "Ela", "Nós", "Vós", "Eles", "Elas", "Você",
            # IT
            "Io", "Lui", "Lei", "Noi", "Voi", "Loro", "Egli",
        }
        has_question = any(
            (o or "").strip() in ("?", "¿") for o in code_options
        )
        if (code_options and not is_anagram and not has_question
                and exercise_type in (
                    "reorder_sentence", "reorder_chronological",
                )
                and len(actions) >= 2):
            cap_words = [o for o in code_options if o and o[0].isupper()]
            # On ne force QUE si l'unique mot capitalisé est un pronom sujet
            if (len(cap_words) == 1
                    and cap_words[0] in SUBJECT_PRONOUNS_CAP):
                expected_first = cap_words[0]
                if actions[0] != expected_first and expected_first in actions:
                    idx = actions.index(expected_first)
                    actions = ([expected_first]
                               + actions[:idx]
                               + actions[idx+1:])
                    problems.append(
                        f"Pronom sujet '{expected_first}' déplacé en 1ère "
                        f"position",
                    )

        return actions, problems

    # =================================================================
    #  AUTO-CRITIQUE : Gemini relit sa propre réponse
    # =================================================================
    def _self_critique(
        self, image, page_text: str, options_str: str,
        exercise_type: str, nb_dz: int, actions: list[str],
        code_options: list[str],
    ) -> list[str]:
        """Gemini cherche activement ses propres erreurs. Renvoie la version
        corrigée si besoin, sinon la réponse d'origine."""
        if not actions:
            return actions

        phrase = " ".join(actions)
        try:
            stock_list = json.loads(options_str) if options_str.startswith("[") \
                else list(code_options)
        except json.JSONDecodeError:
            stock_list = list(code_options)

        rules = CRITIQUE_RULES.get(self.langue, CRITIQUE_RULES["es"])
        # Textes mémorisés (transcript/dialogue d'une page précédente) :
        # nécessaires pour critiquer une réponse de compréhension.
        lesson_block = self._build_lesson_texts_block()

        prompt = f"""Tu es un correcteur C1 en {self.lang_name}. Analyse cette réponse et cherche les erreurs.

PHRASE PROPOSÉE : {phrase}
ÉLÉMENTS DU STOCK : {json.dumps(stock_list, ensure_ascii=False)}
{lesson_block}CONTEXTE : {page_text[:800]}

{rules}

Si tu trouves une erreur → corrige et explique.
Si tout est correct → renvoie exactement la même réponse.

{{"verdict": "OK" ou "CORRIGÉ", "erreur_trouvée": "description ou vide", "actions": {json.dumps(actions, ensure_ascii=False)}}}
JSON UNIQUEMENT. "actions" = textes EXACTS du stock, jamais de préfixe."""

        print("   🔍 Auto-critique...")
        raw = self._call_gemini(prompt, image, max_retries=2, temperature=0)
        if not raw:
            return actions

        result = self._extract_json(raw)
        if not result or "actions" not in result:
            return actions

        verdict = result.get("verdict", "OK")
        erreur  = result.get("erreur_trouvée", "") or ""
        normalized = self._normalize_actions_list(result["actions"])
        corrected = [clean_ai(a) for a in normalized if clean_ai(a).strip()]

        if verdict == "OK":
            print("   ✅ Auto-critique : réponse confirmée")
        else:
            print(f"   🔧 Auto-critique : CORRIGÉ — {erreur[:100]}")
            print(f"   📋 Corrigé : {corrected}")

        return corrected if corrected else actions

    # =================================================================
    #  VÉRIFICATION DÉDIÉE TEXT_INPUT (forme gouvernée + collocation)
    # =================================================================
    def _verify_text_input(self, image, page_text: str,
                           actions: list[str]) -> list[str]:
        """
        Relit le(s) mot(s) tapé(s) dans un texte à trous et corrige les
        deux erreurs les plus fréquentes : la FORME VERBALE gouvernée par
        le mot précédent (mind+ing, to+base, modal+base, prép+ing) et la
        COLLOCATION (eradicate poverty, pas terminate poverty). Texte
        LIBRE : pas de stock, on ne contraint pas à des options.
        """
        if not actions:
            return actions
        # Textes mémorisés (le passage de gauche peut être sur une page
        # précédente) + mots en gras (souvent la réponse vocabulaire).
        lesson_block = self._build_lesson_texts_block()
        bold_hint = self._bold_hint_block()
        prompt = f"""Tu es un correcteur natif C1-C2 en {self.lang_name}. Un texte à trous a été complété.

{lesson_block}TEXTE (le trou est dans cette phrase) :
{page_text[:1000]}

{bold_hint}MOT(S) PROPOSÉ(S) pour le(s) trou(s), dans l'ordre : {json.dumps(actions, ensure_ascii=False)}

Vérifie TRÈS soigneusement, dans cet ordre :
0) MOT DU TEXTE : si la consigne demande un mot lié au texte (vocabulaire, définition, synonyme) et qu'un mot en GRAS du texte complète correctement le trou, c'est très probablement LUI la réponse attendue.
1) FORME VERBALE gouvernée par le mot juste AVANT le trou :
   - mind/enjoy/avoid/finish/keep/suggest/consider/can't help/look forward to/be used to + VERBE-ING (gérondif). Ex : "Would you mind ___ questions" → "answering" (pas "answer").
   - want/decide/hope/need/would like/manage/refuse + TO + base.
   - modal (will/would/can/must/should) + base sans "to".
   - préposition (to/of/about/in/for) + VERBE-ING.
2) COLLOCATION : le mot est-il celui qu'un NATIF emploie dans CETTE combinaison exacte ? Ex : "fighting to ___ poverty" → "eradicate" (pas "terminate"/"end").
3) Accord (nombre/genre), conjugaison, orthographe, accents, casse.
4) Respect d'une éventuelle consigne (synonyme/antonyme/définition).

Si une réponse est INCORRECTE, remplace-la par le mot EXACT à taper. Sinon garde-la telle quelle.
Réponds en JSON UNIQUEMENT, "actions" en premier, MÊME nombre d'éléments et MÊME ordre :
{{"actions": {json.dumps(actions, ensure_ascii=False)}, "raison": "court"}}"""

        raw = self._call_gemini(prompt, image, max_retries=2, temperature=0,
                                model_name=MODEL_VERIF_TEXT)
        if not raw:
            return actions
        result = self._extract_json(raw)
        if not result or "actions" not in result:
            return actions
        corrected = [
            clean_ai(a) for a in self._normalize_actions_list(result["actions"])
            if clean_ai(a).strip()
        ]
        if not corrected:
            return actions
        if corrected != actions:
            print(f"   🔧 Vérif text_input : {actions} → {corrected}")
        else:
            print("   ✅ Vérif text_input : confirmé")
        return corrected

    def _verify_multi_select(self, image, page_text: str,
                             options: list[str],
                             actions: list[str]) -> list[str]:
        """
        Re-vérifie un QCM à PLUSIEURS réponses (ce type saute la
        double-passe → un seul avis sinon). Re-juge CHAQUE option
        indépendamment pour corriger les deux erreurs récurrentes :
        trop d'options cochées (sur-sélection) et pas assez
        (sous-sélection).

        La réponse finale est forcée à être un SOUS-ENSEMBLE du stock
        (chaque proposition est re-mappée sur le texte EXACT d'une option
        via norm_key ; toute proposition hors-stock = hallucination
        ignorée). Filet : si la vérif renvoie du vide ou rien de valide,
        on garde la sélection initiale (on ne dégrade jamais).
        """
        if not actions or not options:
            return actions
        prompt = f"""Tu es un correcteur natif C1-C2 en {self.lang_name}. C'est un QCM à PLUSIEURS réponses possibles : il faut cocher TOUTES les bonnes, ni plus ni moins.

CONTEXTE / QUESTION (lis bien la consigne ET le texte) :
{page_text[:1500]}

OPTIONS (choisis EXCLUSIVEMENT parmi celles-ci, texte EXACT) :
{json.dumps(options, ensure_ascii=False)}

Sélection actuelle à vérifier : {json.dumps(actions, ensure_ascii=False)}

Méthode OBLIGATOIRE — examine CHAQUE option une par une :
- Pour chaque option, demande-toi : « est-elle réellement correcte selon la consigne et le texte ? » (vrai / faux).
- GARDE toutes les options vraies (il peut y en avoir 1, 2, 3…).
- RETIRE toute option fausse cochée à tort (anti-sur-sélection).
- AJOUTE toute option correcte oubliée (anti-sous-sélection).
- Ne te contente pas de confirmer : vérifie vraiment chaque ligne.

Réponds en JSON UNIQUEMENT, "actions" en premier = la liste EXACTE des options correctes (copiées à l'identique du stock, ordre indifférent) :
{{"actions": ["option_exacte_1", "option_exacte_2"], "raison": "court"}}"""

        raw = self._call_gemini(prompt, image, max_retries=2, temperature=0,
                                model_name=MODEL_VERIF_QCM)
        if not raw:
            return actions
        result = self._extract_json(raw)
        if not result or "actions" not in result:
            return actions
        proposed = [
            clean_ai(a)
            for a in self._normalize_actions_list(result["actions"])
            if clean_ai(a).strip()
        ]
        if not proposed:
            return actions
        # Re-mappe chaque proposition sur le TEXTE EXACT d'une option du
        # stock (via norm_key) ; ignore toute proposition hors-stock.
        opt_by_key: dict = {}
        for o in options:
            opt_by_key.setdefault(norm_key(o), o)
        final: list[str] = []
        seen: set = set()
        for p in proposed:
            k = norm_key(p)
            if k in opt_by_key and k not in seen:
                final.append(opt_by_key[k])
                seen.add(k)
        if not final:
            # Rien de valide → on conserve la sélection initiale (filet).
            return actions
        if (set(norm_key(x) for x in final)
                != set(norm_key(x) for x in actions)):
            print(f"   🔧 Vérif QCM multi : {actions} → {final}")
        else:
            print("   ✅ Vérif QCM multi : confirmé")
        return final

    # =================================================================
    #  PASSE 2 INDÉPENDANTE (temperature différente)
    # =================================================================
    def _resolve_independent(
        self, image, page_text: str, options_str: str,
        options: list[str], nb_dz: int, nb_ti: int,
        nb_images: int, single_letters: list[str], is_anagram: bool,
    ) -> dict | None:
        grammar_hint = GRAMMAR_SHORT.get(self.langue, GRAMMAR_SHORT["es"])
        # Les transcripts/dialogues mémorisés (souvent sur une page d'INTRO
        # précédente) sont indispensables aux questions de COMPRÉHENSION :
        # sans eux, la passe 2 devine à l'aveugle et diverge de la passe 1.
        lesson_block = self._build_lesson_texts_block()

        prompt = f"""Correcteur officiel C1 en {self.lang_name}. Résous cet exercice avec précision absolue.

Règles C1 {self.lang_name} : {grammar_hint}

{lesson_block}PAGE : {page_text[:2000]}
{self._bold_hint_block()}STOCK : {options_str}
Trous:{nb_dz} | Texte:{nb_ti} | Images:{nb_images}

TYPE: navigation/carousel/transcript/drag_drop/qcm/reorder_sentence/reorder_chronological/text_input/image_choice

▶ drag_drop: mode(subj/cond/ind/inf)+temps+accord → exactement {nb_dz} réponse(s). SI "Match/Associez/Reliez" (2 colonnes A→B) = APPARIEMENT par le SENS : A fixe, renvoie les B dans l'ordre alignant chaque B sur le A de la même ligne (bijection, aucune définition réutilisée)
▶ qcm: l'erreur C1 est subtile (préposition/mode/accord/faux-ami). Si "best sums up/which sentence summarizes/idée principale" → choisis l'option cohérente avec TOUT le texte ; rejette les distracteurs qui contredisent le texte ou ne sont vrais qu'en partie
▶ reorder_sentence: syntaxe C1 stricte, TOUS {len(options)} éléments obligatoires. Ponctuation finale (. ! ?) TOUJOURS en dernier. ESPAGNOL : une tuile qui COMMENCE par « ¿ » (ou « ¡ ») ouvre une question (exclamation) qui se FERME par la tuile terminée par « ? » (« ! ») — la tuile « ¿… » vient TOUJOURS avant la tuile « …? », et ce qui est entre les deux forme la question. Chaque tuile est utilisée EXACTEMENT autant de fois qu'elle apparaît dans le stock. Si "." ou "!" (pas de "?") → DÉCLARATIF : ordre normal sujet-verbe, AUCUNE inversion (ne fronte pas is/will/do). Si "?" → QUESTION : inversion sujet/auxiliaire (EN: do/will/can/is… ou wh- EN TÊTE). Une majuscule (surtout nom propre) ne force PAS la 1ère position dans une question
▶ reorder_chronological: cohérence discursive, TOUS {len(options)} éléments
▶ text_input: ÉCRIS le mot à taper. Si consigne "word that means X/qui signifie X/synonyme"→SYNONYME de X (pas X), "opposite/contraire"→ANTONYME. Le mot doit s'insérer naturellement (nature+accord+casse). Forme+accents+accords exacts
▶ image_choice: IMG_1=gauche IMG_2=milieu IMG_3=droite

{{"type":"le_type","actions":["r1","r2"],"raisonnement":"court"}}
JSON UNIQUEMENT. "actions" = textes EXACTS du stock UNIQUEMENT.
INTERDIT dans actions : "click_radio_button:X", "select:X", "click_X" ou tout préfixe.
Jamais Validate/Valider/Submit.
navigation→{{"type":"navigation","actions":["Next"]}}"""

        raw = self._call_gemini(prompt, image, temperature=0.4)
        if not raw:
            return None
        result = self._extract_json(raw)
        if not result:
            return None
        if "actions" in result:
            normalized = self._normalize_actions_list(result["actions"])
            stock_keys = {norm_key(o) for o in (options or [])}
            result["actions"] = [
                clean_ai(a) for a in normalized
                if clean_ai(a).strip()
                and (norm_key(clean_ai(a)) in stock_keys
                     or not is_validate_button_word(clean_ai(a)))
            ]
        return result

    # =================================================================
    #  ARBITRE C1 — choisit entre A et B, retry si les deux incomplets
    # =================================================================
    def _arbitrate(
        self, image, page_text: str, options_str: str, nb_dz: int,
        exercise_type: str, actions_a: list[str], actions_b: list[str],
    ) -> list[str]:
        try:
            stock_list = json.loads(options_str) if options_str.startswith("[") else []
        except json.JSONDecodeError:
            stock_list = []

        a_norm = [norm_key(x) for x in actions_a]
        b_norm = [norm_key(x) for x in actions_b]

        # Si les deux incomplets pour reorder → retry direct
        is_reorder = exercise_type in ("reorder_sentence", "reorder_chronological")
        if is_reorder and stock_list:
            a_missing = [o for o in stock_list if norm_key(o) not in a_norm]
            b_missing = [o for o in stock_list if norm_key(o) not in b_norm]
            if a_missing and b_missing:
                print("   ⚠️ A et B incomplets → retry C1 direct")
                retry_prompt = f"""Correcteur C1 {self.lang_name}. Remets ces {len(stock_list)} éléments dans le bon ordre syntaxique.

Stock COMPLET à utiliser : {options_str}
Contexte : {page_text[:1000]}

RÈGLE ABSOLUE : utilise les {len(stock_list)} éléments, PAS UN DE MOINS, PAS UN DE PLUS.
Ordre syntaxique C1 strict : pronoms clitiques avant verbe, adv. temps en début/fin.

{{"type":"{exercise_type}","actions":{json.dumps(stock_list, ensure_ascii=False)}}}
→ Remets dans le BON ordre. JSON uniquement."""
                raw = self._call_gemini(retry_prompt, image, max_retries=3,
                                        model_name=self._arb_model())
                if raw:
                    rj = self._extract_json(raw)
                    if rj and "actions" in rj:
                        acts = [clean_ai(a) for a in rj["actions"]
                                if clean_ai(a).strip()]
                        acts_norm = [norm_key(x) for x in acts]
                        if all(norm_key(o) in acts_norm for o in stock_list):
                            print(f"   ✅ Retry C1 complet : {acts}")
                            return acts

        # Transcripts/dialogues mémorisés : indispensables pour trancher
        # une question de compréhension (le texte est souvent sur une page
        # d'intro précédente, pas sur la page du QCM).
        lesson_block = self._build_lesson_texts_block()

        # ── ARBITRAGE QCM (COMPRÉHENSION) ───────────────────────────────
        # Pour un QCM (souvent Vrai/Faux ou "meilleure réponse"), le critère
        # n'est PAS la grammaire — les deux options sont grammaticalement
        # correctes — mais la COMPRÉHENSION du texte. Le juge grammatical
        # générique est inadapté. On utilise un juge ANCRÉ DANS LE TEXTE :
        # localiser la phrase pertinente, la citer, puis trancher.
        if exercise_type == "qcm":
            allowed = stock_list if stock_list else []
            example_opt = allowed[0] if allowed else "OPTION_EXACTE"
            qcm_prompt = f"""Tu es un correcteur de COMPRÉHENSION (écrite/orale) en {self.lang_name}. Deux réponses divergent à un QCM — tranche en t'appuyant STRICTEMENT sur le texte.

{lesson_block}TEXTE DE LA PAGE : {page_text[:2000]}

QUESTION + OPTIONS : {options_str}
RÉPONSE A : {json.dumps(actions_a, ensure_ascii=False)}
RÉPONSE B : {json.dumps(actions_b, ensure_ascii=False)}

MÉTHODE :
1. Reformule l'affirmation EXACTE à juger (pour un Vrai/Faux : quelle phrase est testée ?). ⚠️ Ne te laisse PAS détourner par une question rhétorique présente DANS le texte : juge bien l'affirmation de l'EXERCICE, pas une phrase du texte.
2. Repère la preuve dans le texte et mets ta citation dans "raison".
3. Décide d'après le texte uniquement (une affirmation partiellement vraie = FAUSSE ; attention paraphrases / négations / quantités).

⚠️ FORMAT CRITIQUE — "actions" doit contenir EXACTEMENT UNE des options autorisées ci-dessous, copiée à l'identique. JAMAIS une phrase du texte, JAMAIS une citation, JAMAIS "Skip"/"Next".
OPTIONS AUTORISÉES pour "actions" : {json.dumps(allowed, ensure_ascii=False)}

{{"raison":"ta citation + ton analyse ici","actions":["{example_opt}"]}}
JSON UNIQUEMENT."""
            print(f"   ⚖️ Arbitrage QCM (compréhension, ancré texte) "
                  f"[{self._arb_model()}]...")
            raw = self._call_gemini(qcm_prompt, image, max_retries=3,
                                    model_name=self._arb_model())
            chosen = None
            if raw:
                rj = self._extract_json(raw)
                if rj and "actions" in rj:
                    acts = [
                        clean_ai(a)
                        for a in self._normalize_actions_list(rj["actions"])
                        if clean_ai(a).strip()
                    ]
                    if stock_list:
                        by_key: dict = {}
                        for o in stock_list:
                            by_key.setdefault(norm_key(o), o)
                        # STOCK UNIQUEMENT : on REJETTE toute valeur hors
                        # options (ex: une citation renvoyée par erreur
                        # dans "actions" → cause de boucle précédente).
                        valid = [by_key[norm_key(a)] for a in acts
                                 if norm_key(a) in by_key]
                        if valid:
                            raison = str(rj.get("raison", ""))[:110]
                            print(f"   ⚖️ Verdict QCM : {valid[0]} — "
                                  f"{raison}")
                            chosen = valid[:1]
                    elif acts:
                        chosen = acts[:1]
            if chosen:
                return chosen
            # Arbitrage sans option valide → on retient la réponse A si
            # c'est une option du stock (cas fréquent : A juste, B = bruit
            # comme 'Skip'), sinon B, sinon A telle quelle.
            if stock_list:
                ak = {norm_key(o): o for o in stock_list}
                for src, label in ((actions_a, "A"), (actions_b, "B")):
                    v = [ak[norm_key(x)] for x in src if norm_key(x) in ak]
                    if v:
                        print(f"   ⚖️ Arbitrage QCM sans option valide → "
                              f"réponse {label} retenue ({v[0]})")
                        return v[:1]
            print("   ⚖️ Arbitrage QCM inexploitable → réponse A")
            return actions_a[:1] if actions_a else actions_a

        prompt = f"""Tu es un JUGE LINGUISTIQUE C1 en {self.lang_name}. Deux réponses divergent — choisis la plus correcte.

Type : {exercise_type} | Stock : {options_str} | Trous : {nb_dz}
{lesson_block}Contexte : {page_text[:1500]}

RÉPONSE A : {json.dumps(actions_a, ensure_ascii=False)}
RÉPONSE B : {json.dumps(actions_b, ensure_ascii=False)}

CRITÈRES C1 (par ordre de priorité) :
1. EXHAUSTIVITÉ : pour reorder, les {len(stock_list) if stock_list else '?'} éléments doivent tous être présents
2. GRAMMATICALITÉ C1 : mode (subj/cond/ind), temps, accords, régimes prépositionnels
3. IDIOMATICITÉ : ordre naturel pour un natif C1 de {self.lang_name}
4. COHÉRENCE SÉMANTIQUE : le sens est-il correct dans le contexte ?

ANALYSE :
- Phrase A complète : {' '.join(actions_a)}
- Phrase B complète : {' '.join(actions_b)}
- Laquelle est grammaticalement supérieure au niveau C1 ?
- Si les deux ont des erreurs → construis la meilleure version possible avec le stock

{{"choix":"A/B/SYNTHESE","raison":"justification grammaticale C1","actions":["final1","final2"]}}

⚠️ "actions" DOIT être une liste de STRINGS PURES — JAMAIS d'objets imbriqués.
✅ CORRECT : "actions": ["may have been", "must have had", "could have avoided"]
❌ INTERDIT : "actions": [{{"blank":1,"word":"may have been"}}, ...]
JSON UNIQUEMENT."""

        # Si la passe 1 venait de Flash-Lite, l'arbitre est Flash : un
        # modèle léger ne doit pas départager sa propre réponse.
        arb_model = self._arb_model()
        print(f"   ⚖️ Arbitrage C1 en cours [{arb_model}]...")
        raw = self._call_gemini(prompt, image, max_retries=3,
                                model_name=arb_model)
        if not raw:
            print("   ⚠️ Arbitrage échoué → garde A")
            return actions_a

        result = self._extract_json(raw)
        if not result or "actions" not in result:
            print("   ⚠️ JSON invalide → garde A")
            return actions_a

        choix  = result.get("choix", "?")
        raison = result.get("raison", "") or ""
        print(f"   ⚖️ Verdict C1 : {choix} — {raison[:120]}")

        # ── COHÉRENCE verdict ↔ résultat ──────────────────────────────
        # Si le juge choisit EXPLICITEMENT A ou B, on renvoie CETTE
        # réponse — PAS le champ "actions". Gemini remplit parfois
        # "actions" avec l'ordre de l'AUTRE réponse tout en mettant
        # choix="A" (incohérence observée : verdict A mais actions =
        # ordre de B → on posait B à tort, à l'encontre du raisonnement
        # du juge). Le champ "actions" libre n'est exploité que pour une
        # vraie SYNTHÈSE (choix="SYNTHESE"), où le juge reconstruit un
        # ordre qui n'est ni A ni B.
        choix_u = str(choix).strip().upper()
        if choix_u == "A":
            return actions_a
        if choix_u == "B":
            return actions_b

        # Normalisation : Gemini peut retourner des dicts au lieu de strings
        normalized = self._normalize_actions_list(result["actions"])
        acts = [clean_ai(a) for a in normalized if clean_ai(a).strip()]

        # ── GARDE-FOU : Gemini doit retourner UNE LISTE DE MOTS du
        # stock, pas les PHRASES CONCATÉNÉES. Bug observé sur reorder
        # avec doublons : verdict='A' mais actions=['phrase A entière',
        # 'phrase B entière'] au lieu des mots ordonnés. Résultat : le
        # validator suivant voit que TOUT le stock est manquant et
        # déclenche un retry forcé qui choisit l'ordre B (suboptimal).
        #
        # Si le nombre d'actions ne matche pas le stock, ou si une
        # action contient un espace ET le stock est composé de mots
        # uniques, c'est probablement le bug ci-dessus → on retombe
        # sur actions_a ou actions_b selon le verdict.
        if stock_list and acts:
            stock_norm = [norm_key(o) for o in stock_list]
            acts_norm = [norm_key(a) for a in acts]
            # Combien d'actions correspondent à un mot du stock ?
            matching = sum(1 for a in acts_norm if a in stock_norm)
            mismatch_ratio = matching / max(len(acts), 1)

            # Heuristique : si moins de 50% des actions sont dans le
            # stock OU si le nombre d'actions est très différent du
            # stock, l'arbitre a renvoyé des phrases au lieu de mots.
            if mismatch_ratio < 0.5 or abs(len(acts) - len(stock_list)) >= 2:
                fallback = (actions_a if choix == "A"
                            else actions_b if choix == "B"
                            else actions_a)
                print(f"   ⚠️ Arbitrage : actions ne matchent pas le stock "
                      f"({matching}/{len(acts)} mots reconnus) — fallback "
                      f"sur réponse {choix or 'A'}")
                return fallback

        return acts if acts else actions_a

    # =================================================================
    #  IMAGE CHOICE
    # =================================================================
    def _solve_image_radio(self, page_text: str, names: list) -> dict | None:
        """QCM à images : télécharge CHAQUE image d'option, les assemble
        côte à côte avec un grand numéro, et demande à Flash (avec les
        textes de la leçon) laquelle correspond. 2 avis indépendants ;
        s'ils divergent, un 3e départage."""
        try:
            srcs = self.driver.execute_script("""
                const out = {};
                for (const lab of document.querySelectorAll('label')) {
                    const t = (lab.innerText || '').trim();
                    const img = lab.querySelector('img');
                    if (img && t) out[t] = img.currentSrc || img.src;
                }
                return out;
            """) or {}
        except WebDriverException:
            return None
        import urllib.request
        from PIL import ImageDraw
        tiles = []
        for n, name in enumerate(names, 1):
            url = next((u for t, u in srcs.items() if name.strip() in t), None)
            if not url:
                return None
            try:
                raw = urllib.request.urlopen(urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0"}),
                    timeout=15).read()
                im = Image.open(io.BytesIO(raw)).convert("RGB")
            except Exception:  # noqa: BLE001
                return None
            h = 360
            im = im.resize((max(1, int(im.width * h / im.height)), h))
            canvas = Image.new("RGB", (im.width, h + 60), "white")
            canvas.paste(im, (0, 60))
            d = ImageDraw.Draw(canvas)
            d.rectangle([0, 0, 70, 56], fill=(124, 92, 255))
            try:
                d.text((22, 8), str(n), fill="white", font_size=40)
            except TypeError:                # ancien Pillow
                d.text((28, 22), str(n), fill="white")
            tiles.append(canvas)
        W = sum(t.width for t in tiles) + 20 * (len(tiles) - 1)
        board = Image.new("RGB", (W, max(t.height for t in tiles)), "white")
        x = 0
        for t in tiles:
            board.paste(t, (x, 0))
            x += t.width + 20
        lesson = self._build_lesson_texts_block()
        clean_txt = "\n".join(l for l in page_text.splitlines()
                              if not re.search(r"\.(jpe?g|png|webp)$", l, re.I))
        prompt = f"""{lesson}EXERCICE ({self.lang_name}) — choisir la BONNE IMAGE.
TEXTE DE LA PAGE (consigne / phrase) :
{clean_txt[:1500]}

L'image jointe montre les {len(names)} options, numérotées de 1 à {len(names)} (grand numéro en haut à gauche de chaque image).
1) Repère le mot ou la phrase exacte que l'image doit illustrer.
2) Décris en quelques mots CHAQUE image.
3) Choisis celle qui illustre le MIEUX ce mot/cette phrase (sens littéral et, s'il y en a, ce que disent les textes de la leçon).

JSON uniquement : {{"cible": "...", "images": ["1: ...", "2: ..."], "choix": 1}}"""
        # Clé AQ. (mode économe) : les questions d'IMAGES méritent le vrai
        # Flash (quota 20/j, ~1 question image par leçon) plutôt que Lite.
        img_model = MODEL_NAME
        if _REST_KEY:
            img_model = next((n for n in _AQ_CHAIN if "lite" not in n),
                             MODEL_NAME)
        votes = []
        for k in range(3):
            if k == 2 and len(set(votes)) <= 1:
                break
            raw = self._call_gemini(prompt, board, max_retries=3,
                                    model_name=img_model)
            rj = self._extract_json(raw or "") or {}
            try:
                v = int(str(rj.get("choix", "")).strip())
            except ValueError:
                v = None
            if v and 1 <= v <= len(names):
                votes.append(v)
                print(f"   🖼️ Avis {k + 1} : image {v} — cible « "
                      f"{str(rj.get('cible', ''))[:50]} »")
        if not votes:
            return None
        best = max(set(votes), key=votes.count)
        print(f"   🖼️ QCM image → n°{best} ({names[best - 1]})")
        return {"exercise_type": "qcm", "actions": [names[best - 1]]}

    def _solve_image_choice(
        self, image, page_text: str, image_elements: list,
    ) -> dict:
        nb = len(image_elements)
        print(f"   🖼️ IMAGE CHOICE — {nb} images détectées")

        prompt = f"""Tu es un expert en langues. Tu vois un exercice avec {nb} images numérotées.

=== CONSIGNE DE LA PAGE ===
{page_text[:1500]}

=== TON TRAVAIL ===
Regarde le screenshot. Tu vois {nb} images disposées sur la page.
Numérote-les de gauche à droite : image 1, image 2, image 3...

Pour CHAQUE image :
- Décris brièvement ce qu'elle représente (1 ligne)

Ensuite :
- Lis la CONSIGNE EXACTE de l'exercice
- Quelle image correspond LE MIEUX à la consigne ?

Réponds UNIQUEMENT en JSON :
{{"description_images": ["image 1 : ...", "image 2 : ...", "image 3 : ..."], "choix": 1, "raison": "explication courte"}}

- "choix" = le NUMÉRO de l'image (1, 2 ou 3...), PAS "IMG_1"
- JSON uniquement, rien avant, rien après"""

        raw = self._call_gemini(prompt, image, max_retries=3)
        if not raw:
            print("   ⚠️ Image choice : pas de réponse → IMG_1")
            return {"actions": ["IMG_1"]}

        print(f"   📨 Image choice : {raw[:200]}")
        rj = self._extract_json(raw)

        # Extraction safe du numéro
        num: int | None = None
        if rj and "choix" in rj:
            try:
                num = int(str(rj["choix"]).strip())
            except (ValueError, TypeError):
                num = None
            raison = rj.get("raison", "") or ""
            descs  = rj.get("description_images", []) or []
            if isinstance(descs, list):
                for i, d in enumerate(descs, 1):
                    print(f"      img{i} : {d}")
            print(f"   🖼️ Choix : image {num} — {raison[:120]}")

        # Fallback : premier chiffre dans la réponse brute
        if num is None:
            m = re.search(r'\b([1-9])\b', raw)
            if m:
                try:
                    num = int(m.group(1))
                    print(f"   🔢 Fallback numéro extrait : {num}")
                except ValueError:
                    num = None

        if num is None:
            print("   ⚠️ Image choice illisible → IMG_1")
            return {"actions": ["IMG_1"]}

        num = max(1, min(num, nb))
        return {"actions": [f"IMG_{num}"]}

    # =================================================================
    #  ANAGRAMME DE LETTRES — solveur Python + IA fallback
    # =================================================================
    def _answer_word_pattern(self, nb_letters: int) -> list[int] | None:
        """Longueurs des mots d'après les CASES de réponse : GlobalExam
        laisse un espace entre les groupes de cases (« DOS MUJERES » =
        3 cases, espace, 7 cases → [3, 7])."""
        try:
            zones = self._get_drop_zones() or []
            if len(zones) != nb_letters or nb_letters < 2:
                return None
            rects = self.driver.execute_script(
                "return arguments[0].map(e => { const r = "
                "e.getBoundingClientRect(); return [r.left, r.top, "
                "r.width, r.height]; });", zones) or []
        except WebDriverException:
            return None
        if len(rects) != nb_letters:
            return None
        rects = sorted(rects, key=lambda r: (round(r[1] / 10), r[0]))
        gaps = []
        for a, b in zip(rects, rects[1:]):
            if abs(a[1] - b[1]) < a[3] / 2:
                gaps.append(b[0] - (a[0] + a[2]))
        normal = sorted(gaps)[len(gaps) // 2] if gaps else 0
        pattern, cur = [], 1
        for a, b in zip(rects, rects[1:]):
            same_row = abs(a[1] - b[1]) < a[3] / 2
            gap = b[0] - (a[0] + a[2])
            if not same_row or gap > max(normal * 1.8, normal + 12):
                pattern.append(cur)
                cur = 1
            else:
                cur += 1
        pattern.append(cur)
        return pattern

    def _solve_letter_anagram(
        self, image, page_text: str, single_letters: list[str], nb_images: int,
    ) -> dict:
        available    = sorted(o.upper() for o in single_letters)
        available_lc = sorted(o.lower() for o in single_letters)
        nb_lettres   = len(available)

        lang      = self.langue
        lang_name = self.lang_name
        print(f"   🌐 Langue : {lang_name} [{lang}]")

        checker = _DictCache.get_spellchecker(lang)

        # ─── VOCABULAIRE DE L'ACTIVITÉ (prioritaire, gratuit) ─────────
        # Le code de la page liste les expressions de l'activité (« No,
        # gracias », « Un lago »…). Si l'une d'elles a EXACTEMENT les
        # mêmes tuiles (lettres + ponctuation, accents ignorés) → c'est la
        # réponse, sans appel IA. Gère aussi les tuiles de ponctuation
        # (« , ») que les dictionnaires ne connaissent pas.
        try:
            from collections import Counter as _Cn
            k = lambda ch: _strip_accents_keep_n(ch).upper()
            tiles_key = _Cn(k(t) for t in single_letters)
            pattern_v = self._answer_word_pattern(nb_lettres)
            hits = []
            # Sources : vocabulaire de l'activité + toutes les lignes de
            # texte vues sur le site pendant la session (ex. l'option
            # « ¿Cerveza? No, gracias » d'un exo précédent). On teste
            # chaque suite de mots consécutifs (« No, gracias »).
            sources = list(self._activity_vocab()) + list(
                getattr(self, "_seen_lines", {}).keys())
            nw = len(pattern_v) if pattern_v else 0
            seen_v = set()
            # Ponctuation absente des tuiles (« ¿Fatal? » pour les
            # tuiles F-A-T-A-L) → ignorée
            tile_punct = {t for t in single_letters if not t.isalnum()}
            for line in sources:
                ws = ["".join(c for c in w if c.isalnum() or c in tile_punct)
                      for w in line.split()]
                ws = [w for w in ws if w]
                # Sans structure connue : UN seul mot (sinon « a abrir »
                # était pris pour ARRIBA)
                spans = ([(i, i + nw) for i in range(len(ws) - nw + 1)]
                         if nw else [(i, i + 1) for i in range(len(ws))])
                for i, j in spans:
                    v = " ".join(ws[i:j])
                    if v in seen_v:
                        continue
                    seen_v.add(v)
                    chars = [c for c in v if not c.isspace()]
                    if len(chars) != nb_lettres:
                        continue
                    if _Cn(k(c) for c in chars) != tiles_key:
                        continue
                    if pattern_v and len(pattern_v) > 1 and [
                            len(w) for w in ws[i:j]] != list(pattern_v):
                        continue
                    hits.append((v, chars))
            if hits:
                v, chars = hits[0]
                pool = {}
                for t in single_letters:
                    pool.setdefault(k(t), []).append(t)
                acts = []
                for c in chars:
                    lst = pool[k(c)]
                    # même lettre ACCENTUÉE d'abord (Á ≠ A)
                    j = next((i for i, t in enumerate(lst)
                              if t.upper() == c.upper()), 0)
                    acts.append(lst.pop(j))
                print(f"   📚 Anagramme résolu grâce au texte du site : "
                      f"« {v} » (sans appel IA)")
                return {"actions": acts}
        except Exception as e:  # noqa: BLE001
            print(f"   ⚠️ Vocabulaire activité : {str(e)[:60]}")

        # ─── DÉCISION DE PRIORITÉ ────────────────────────────────────
        # Si une image est associée (anagramme "illustré"), l'image
        # donne un indice DÉCISIF (ex: crabe → CRAB, pas CARB). Dans
        # ce cas on met l'IA en priorité car seule elle "voit"
        # l'image ; le solveur Python ne connaît pas le contexte
        # visuel et pourrait choisir le premier mot valide du
        # dictionnaire (souvent incorrect).
        #
        # S'il n'y a PAS d'image (anagramme pur), le solveur Python
        # reste prioritaire car il est rapide et fiable.
        has_image_hint = nb_images > 0
        if has_image_hint:
            print(f"   🖼️  Image(s) détectée(s) ({nb_images}) → "
                  f"IA prioritaire pour exploiter l'indice visuel")

        def _python_solve() -> tuple[str, str] | None:
            """Retourne (mot, mode) ou None. mode = 'exact' | 'superset+N'."""
            from collections import Counter
            target = sorted(available_lc)
            target_counter = Counter(available_lc)
            words = _DictCache.get_wordlist(lang, top_n=300_000)
            if not words:
                return None
            print(f"   📚 Solveur Python : {len(words)} mots [{lang}]")

            # A) Match EXACT (rapide, cas normal)
            for word in words:
                w = word.lower().strip()
                if len(w) != nb_lettres:
                    continue
                if sorted(list(w)) == target:
                    print(f"   ✅ Solveur Python (exact) : {w.upper()}")
                    return (w, "exact")

            # B) Fallback SUPERSET : mot plus long contenant TOUTES les
            #    lettres détectées + 1 ou 2 de plus (cas : lettre doublée
            #    perdue lors du scan, ex: TICKETS vu comme CEIKST).
            #    On se limite à +2 pour éviter de trouver n'importe quoi.
            for extra in (1, 2):
                target_len = nb_lettres + extra
                for word in words:
                    w = word.lower().strip()
                    if len(w) != target_len:
                        continue
                    wc = Counter(w)
                    # Contient TOUTES les lettres disponibles (superset)
                    if all(wc[ch] >= cnt for ch, cnt in target_counter.items()):
                        print(f"   ⚠️ Solveur Python (superset+{extra}) : "
                              f"{w.upper()} — possible doublon perdu "
                              f"dans le scan DOM")
                        return (w, f"superset+{extra}")
            return None

        # ─── Si PAS d'image : solveur Python en priorité ─────────────
        if not has_image_hint:
            try:
                python_result = _python_solve()
            except Exception as e:
                print(f"   ⚠️ Solveur Python erreur : {e}")
                python_result = None

            if python_result:
                python_word, mode = python_result
                word_upper = python_word.upper()
                # Mode exact : on renvoie les lettres du mot (qui == stock trié)
                if mode == "exact" and sorted(list(word_upper)) == available:
                    actions_out = list(word_upper)
                    print(f"   📋 Final anagramme (Python) : {actions_out}")
                    return {"actions": actions_out}
                # Mode superset : on renvoie les lettres du mot,
                # l'exécution cliquera les lettres disponibles ; si
                # une lettre manque (ex: 2 T détectés différemment),
                # la boucle IA prendra le relais.
                if mode.startswith("superset"):
                    # Vérifie qu'on peut au moins cliquer toutes les
                    # lettres uniques
                    from collections import Counter as _Cnt
                    if _Cnt(word_upper) - _Cnt(available) == _Cnt():
                        # Toutes les lettres du mot sont bien dispo
                        actions_out = list(word_upper)
                        print(f"   📋 Final anagramme (superset) : "
                              f"{actions_out}")
                        return {"actions": actions_out}
                    else:
                        print("   ⚠️ Superset trouvé mais lettres "
                              "insuffisantes au clic → passe à l'IA")
            else:
                # python_result == None : pas de mot trouvé → IA
                print("   ⚠️ Solveur Python n'a rien trouvé → "
                      "passe à l'IA")

        # ─── BOUCLE IA ────────────────────────────────────────────────
        # Si image présente : l'IA DOIT identifier l'objet/scène avant
        # de proposer un mot. Sinon, c'est juste un anagramme classique.
        if has_image_hint:
            print("   🤖 Anagramme illustré → IA avec indice visuel")
        else:
            print("   🤖 Solveur Python insuffisant → boucle IA")
        best_word: str | None = None
        feedback = ""

        # ── Structure (mots) + candidats du dictionnaire ──────────────
        pattern = self._answer_word_pattern(nb_lettres)
        if pattern and len(pattern) > 1:
            print(f"   🧩 Cases de réponse : {len(pattern)} mots de "
                  f"{pattern} lettres")
        candidates: list[str] = []
        try:
            words_dict = _DictCache.get_wordlist(lang, top_n=300_000)
            if words_dict:
                candidates = anagram_phrase_candidates(
                    available, words_dict, pattern=pattern)
        except Exception as e:  # noqa: BLE001
            print(f"   ⚠️ Recherche d'expressions : {str(e)[:60]}")
        if candidates:
            print(f"   📚 Candidats dictionnaire : {candidates[:6]}")
        pattern_line = ""
        if pattern and len(pattern) > 1:
            pattern_line = (
                f"🧩 STRUCTURE IMPOSÉE (lue sur les cases de réponse) : "
                f"{len(pattern)} MOTS, de {', '.join(map(str, pattern))} "
                f"lettres, DANS CET ORDRE.")
        cand_line = ""
        if candidates:
            cand_line = (
                "📚 Réponses possibles trouvées dans le dictionnaire avec "
                "EXACTEMENT ces lettres : " + " | ".join(candidates[:10]) +
                ". Choisis celle qui correspond à l'image / au contexte "
                "(ou une autre UNIQUEMENT si aucune ne convient).")

        for attempt in range(3):
            print(f"   🔠 Anagramme — tentative {attempt+1}/3")

            fb_line = (f"⚠️ ATTENTION, retour du vérificateur : {feedback}"
                       if feedback else "")
            # Instruction image RENFORCÉE : l'indice visuel est décisif
            if nb_images > 0:
                img_hint = (
                    "🖼️ INDICE VISUEL CRITIQUE : une image accompagne "
                    "l'exercice. Le mot à trouver EST CE QUE L'IMAGE "
                    "REPRÉSENTE (objet, animal, action, lieu, etc.). "
                    "Identifie d'abord ce que montre l'image, PUIS "
                    "cherche le mot qui le nomme en utilisant les "
                    "lettres disponibles. Sans cette étape, tu risques "
                    "de choisir un mot valide mais FAUX."
                )
            else:
                img_hint = ""

            prompt = f"""Tu es un expert en langues. Tu dois reconstituer la RÉPONSE — un MOT RÉEL ou une COURTE EXPRESSION (2 à 3 mots) — en {lang_name}, à partir des lettres données.

LETTRES DISPONIBLES : {', '.join(available)} ({nb_lettres} lettres)
{pattern_line}
{cand_line}
{fb_line}
{img_hint}
Contexte textuel : {page_text[:400]}

RÈGLES STRICTES :
1. La réponse (mot ou expression) doit exister en {lang_name}
2. Tu dois utiliser EXACTEMENT ces {nb_lettres} lettres, une seule fois chacune
3. Aucune lettre de plus, aucune lettre de moins
4. ⚠️ Si AUCUN mot unique n'utilise les {nb_lettres} lettres, c'est presque sûrement une EXPRESSION de 2-3 mots (souvent un infinitif « to … », une expression idiomatique ou une exclamation) qui décrit l'image / la situation. L'ESPACE entre les mots n'est PAS une lettre (ne le compte pas dans les {nb_lettres}), mais une apostrophe ' ou un trait d'union - EST une lettre du stock.
4bis. ⚠️ ORDRE DES MOTS : respecte l'ordre NATUREL de l'expression. Pour un infinitif, « to » se place AVANT le verbe → écris « TO LOUNGE », JAMAIS « LOUNGE TO ». Vérifie que ton expression se lit correctement en {lang_name}.
5. Si une image est présente : la réponse DOIT correspondre à ce qu'elle montre
6. Vérifie lettre par lettre avant de répondre (espaces exclus)

Réflexion étape par étape :
a) {"Je décris l image : qu est-ce qui est représenté / quelle situation ?" if nb_images > 0 else "Analyse du contexte textuel"}
b) Lettres dispo : {', '.join(available)}
c) {"Quel mot OU quelle courte expression décrit l image avec ces lettres ?" if nb_images > 0 else f"Quel mot OU quelle courte expression de {nb_lettres} lettres en {lang_name} ?"}
d) Je vérifie chaque candidat lettre par lettre (sans compter les espaces)
e) Je choisis la réponse qui {"MATCHE L IMAGE" if nb_images > 0 else "est la plus probable"}

Réponds UNIQUEMENT en JSON (le champ "mot" peut contenir une expression avec espaces ; "actions" = les LETTRES dans l'ordre, SANS espace) :
{{"langue": "{lang}", "mot": "LA REPONSE", "image_decrite": "description courte de l image ou N/A", "actions": ["L","E","T","T","R","E","S"]}}"""

            raw = self._call_gemini(prompt, image, max_retries=3)
            if not raw:
                feedback = "Pas de réponse reçue."
                continue

            rj = self._extract_json(raw)
            if not rj:
                feedback = "JSON invalide."
                continue

            # Extraction du mot
            word = ""
            if rj.get("mot"):
                word = str(rj["mot"]).strip().upper()
            elif rj.get("actions"):
                acts = rj["actions"]
                if isinstance(acts, list) and acts:
                    if len(acts) == 1 and len(str(acts[0])) > 1:
                        word = str(acts[0]).upper()
                    else:
                        word = "".join(
                            str(a).upper() for a in acts
                            if len(str(a).strip()) == 1
                        )
            if not word:
                feedback = "Pas de mot dans la réponse."
                continue

            # Log de ce que l'IA a "vu" sur l'image (si présent)
            img_desc = rj.get("image_decrite", "")
            if img_desc and img_desc not in ("N/A", ""):
                print(f"   👁️  Image décrite : {str(img_desc)[:80]}")

            print(f"   🔠 Proposé : {word}")

            # La réponse peut être une EXPRESSION en plusieurs mots
            # (ex: "YOU'RE KIDDING") : l'espace est un séparateur visuel,
            # PAS une tuile (il n'y a pas de tuile espace dans le stock).
            # On le retire pour valider et placer les lettres. L'apostrophe
            # et le trait d'union, eux, SONT des tuiles → on les garde.
            word_letters = word.replace(" ", "")

            # Vérif 1 : lettres exactes (espaces ignorés)
            if (pattern and len(pattern) > 1
                    and [len(w) for w in word.split()] != pattern
                    and sorted(list(word_letters)) == available):
                feedback = (
                    f"'{word}' ne respecte pas la structure des cases : il "
                    f"faut {len(pattern)} mots de "
                    f"{', '.join(map(str, pattern))} lettres, dans cet ordre.")
                print(f"   ⚠️ Structure {[len(w) for w in word.split()]} ≠ "
                      f"cases {pattern}")
                continue

            if sorted(list(word_letters)) != available:
                feedback = (
                    f"Lettres incorrectes : tu as proposé "
                    f"{sorted(list(word_letters))} mais les lettres "
                    f"disponibles sont {available}. "
                    f"Utilise EXACTEMENT ces lettres : {', '.join(available)}"
                )
                print(f"   ⚠️ Lettres incorrectes : "
                      f"{sorted(list(word_letters))} ≠ {available}")
                continue

            # (Plus de garde 'isalpha' ici : la vérif lettres-exactes
            # ci-dessus garantit déjà que word_letters == stock. Un
            # isalpha rejetterait à tort une apostrophe ou un trait
            # d'union qui sont des tuiles parfaitement légitimes.)

            # Vérif 2 : mots réels (pyspellchecker). On vérifie CHAQUE
            # mot de la réponse, en sautant ceux qui contiennent une
            # apostrophe ou un trait d'union (contractions type "you're",
            # mots composés : le dico les juge mal → faux négatifs). Pour
            # un mot simple ça vérifie le mot ; pour une expression
            # ("to lounge") ça vérifie "to" ET "lounge". Garde-fou contre
            # une suite de lettres anagramme-valide mais qui n'est pas une
            # vraie réponse (ex: "OUTGELON", ou une expression bidon).
            #
            # EXCEPTION — anagramme ILLUSTRÉ : s'il y a une image, on fait
            # confiance à Gemini. La Vérif 1 ci-dessus garantit déjà que les
            # lettres sont EXACTEMENT celles du stock (anagramme valide,
            # vérifié en Python), et l'image lui donne le sens du mot. Le
            # dico ne ferait que rejeter des mots réels qu'il ne connaît pas
            # (ex. « MUJERES » en espagnol → poussait vers un mauvais « ES
            # MUJER »). On saute donc la Vérif 2 et on accepte la 1re
            # proposition. Le dico reste actif pour les anagrammes SANS
            # image (aucun autre indice pour trancher).
            if checker is not None and not has_image_hint:
                words_to_check = [
                    w.lower() for w in word.split()
                    if w and "'" not in w and "-" not in w
                ]
                try:
                    unknown = (checker.unknown(words_to_check)
                               if words_to_check else set())
                except Exception:
                    unknown = set()
                if unknown:
                    correction = None
                    if " " not in word:           # suggestion utile si 1 mot
                        try:
                            correction = checker.correction(word.lower())
                        except Exception:
                            correction = None
                    feedback = (
                        f"'{word}' n'est pas reconnu en {lang} "
                        f"(mot(s) douteux : {', '.join(sorted(unknown))}). "
                        f"{f'Peut-être {correction} ? ' if correction else ''}"
                        f"IMPORTANT : si aucun MOT UNIQUE réel n'utilise "
                        f"ces lettres, la réponse est très probablement une "
                        f"EXPRESSION de 2-3 mots (ex: un infinitif « to … » "
                        f"ou une expression idiomatique) qui décrit l'image. "
                        f"L'espace ne compte pas comme lettre."
                    )
                    print(f"   ⚠️ Mot inconnu : {word} "
                          f"(douteux : {sorted(unknown)})")
                    if best_word is None:
                        best_word = word
                    continue

            print(f"   ✅ Anagramme validé : {word}")
            actions_out = list(word_letters)   # tuiles réelles (sans espace)
            print(f"   📋 Final anagramme : {actions_out}")
            return {"actions": actions_out}

        # ─── FALLBACK après 3 tentatives IA échouées ──────────────────
        # Si l'IA n'a produit aucun mot validé, on tente le solveur
        # Python (même avec image : mieux vaut un mot valide au pif
        # qu'un échec total). Sinon on utilise best_word (non dico)
        # ou en dernier recours les lettres dans l'ordre alphabétique.
        if has_image_hint:
            # On n'a pas encore tenté le solveur Python (passé en 2nd)
            print("   🔁 IA anagramme épuisée → tentative Python en "
                  "secours")
            try:
                python_result = _python_solve()
            except Exception as e:
                print(f"   ⚠️ Solveur Python erreur : {e}")
                python_result = None
            if python_result:
                pw, pmode = python_result
                pw_upper = pw.upper()
                if pmode == "exact" and sorted(list(pw_upper)) == available:
                    actions_out = list(pw_upper)
                    print(f"   📋 Final anagramme (Python fallback) : "
                          f"{actions_out}")
                    return {"actions": actions_out}
                elif pmode.startswith("superset"):
                    from collections import Counter as _Cnt
                    if _Cnt(pw_upper) - _Cnt(available) == _Cnt():
                        actions_out = list(pw_upper)
                        print(f"   📋 Final anagramme (Python superset "
                              f"fallback) : {actions_out}")
                        return {"actions": actions_out}

        if candidates:
            best = candidates[0]
            print(f"   📚 Secours dictionnaire : {best}")
            return {"actions": list(best.replace(" ", ""))}

        if best_word:
            print(f"   ⚠️ Fallback anagramme (non validé dico) : {best_word}")
            return {"actions": list(best_word.replace(" ", ""))}

        print("   💀 Anagramme échoué → ordre alphabétique")
        return {"actions": available}

    # =================================================================
    #  ANAGRAMME DE PHRASES — vérif Python + score confiance
    # =================================================================
    def _solve_phrase_anagram(
        self, image, page_text: str, elements: list[str], nb_images: int,
    ) -> dict:
        stock = list(elements)
        nb_elements = len(stock)
        stock_str = json.dumps(stock, ensure_ascii=False)

        lang      = self.langue
        lang_name = self.lang_name
        print(f"   🌐 Langue : {lang_name} [{lang}]")

        best_actions: list[str] | None = None
        best_score   = 0
        feedback     = ""

        stock_norm = [norm_key(e) for e in stock]
        stock_map  = {norm_key(e): e for e in stock}

        for attempt in range(3):
            print(f"   🔀 Phrase anagramme — tentative {attempt+1}/3")

            fb_line  = (f"⚠️ RETOUR DU VÉRIFICATEUR : {feedback}"
                        if feedback else "")
            img_hint = ("🖼️ REGARDE L IMAGE — indice visuel."
                        if nb_images > 0 else "")

            prompt = f"""Tu es un PROFESSEUR DE LANGUES expert en {lang_name}.
Tu dois remettre ces éléments dans le bon ordre pour former une phrase correcte.

ÉLÉMENTS À ORDONNER ({nb_elements} au total) :
{stock_str}

{fb_line}
{img_hint}
Contexte : {page_text[:500]}

MÉTHODE OBLIGATOIRE :
1. Identifie la langue : {lang_name}
2. Identifie le SUJET, le VERBE PRINCIPAL, les COMPLÉMENTS
3. Construis la phrase selon l'ordre naturel de la langue :
   - Espagnol/Français : (Adv. temps) + Sujet + Verbe + COD + COI + (Adv. manière)
   - Anglais : Sujet + (auxiliaire) + Verbe + COD + compléments
   ⚠️ PONCTUATION FINALE (. ! ?) : TOUJOURS en DERNIÈRE position, jamais
     au milieu. Elle indique le TYPE de phrase :
     • "." ou "!" (et pas de "?") → DÉCLARATIVE/exclamative : ordre normal
       sujet-verbe, AUCUNE inversion sujet/auxiliaire (ne fronte pas
       is/will/do…).
       Ex : ['is','the','.','will','raw','material supply','steady',
       'except','which','not happen,','a coup,'] → "The raw material
       supply is steady, except a coup, which will not happen." (is/will
       restent après leur sujet).
     • "?" → QUESTION : INVERSION sujet/auxiliaire.
       - Anglais yes/no : auxiliaire/modal (do/does/did/will/would/can/
         could/is/are/was/were/have/has/should...) EN TÊTE, avant le sujet.
         Ex : ['Jack and Amelia','will','the ideas presented','by Emily',
         'convince','?'] → "Will the ideas presented by Emily convince Jack
         and Amelia?" (sujet = "the ideas presented by Emily").
       - Anglais wh- : mot interrogatif (what/who/where/when/why/which/how)
         en 1er, puis auxiliaire, puis sujet.
       - Un nom propre capitalisé (Jack, Emily) n'ouvre PAS la question
         juste parce qu'il a une majuscule : c'est l'auxiliaire/le wh- qui
         ouvre.
4. RÈGLE ABSOLUE : utilise les {nb_elements} éléments, PAS UN DE MOINS, PAS UN DE PLUS
5. Relis la phrase — est-elle naturelle pour un locuteur natif ?
6. Donne un SCORE DE CONFIANCE de 1 à 10 :
   - 10 : certain à 100%, grammaire parfaite
   - 7-9 : très probable
   - 4-6 : incertain, il pourrait y avoir un meilleur ordre
   - 1-3 : doute sérieux

Réponds UNIQUEMENT en JSON :
{{"langue": "{lang}", "phrase": "la phrase complète reconstituée", "actions": ["elem1", "elem2", ...], "score": 8, "raison": "explication courte de l'ordre choisi"}}"""

            raw = self._call_gemini(prompt, image, max_retries=3)
            if not raw:
                feedback = "Pas de réponse reçue."
                continue

            rj = self._extract_json(raw)
            if not rj or "actions" not in rj:
                feedback = "JSON invalide ou champ 'actions' absent."
                continue

            proposed = [str(x) for x in rj["actions"]]
            try:
                score = int(rj.get("score", 0))
            except (ValueError, TypeError):
                score = 0
            raison  = rj.get("raison", "") or ""
            phrase  = rj.get("phrase", " ".join(proposed))
            print(f"   🔀 Proposé (score={score}/10) : {phrase}")
            print(f"   💭 Raison : {raison[:100]}")

            # ── Vérif Python exhaustivité ────────────────────────────
            prop_norm = [norm_key(p) for p in proposed]
            missing = [e for e in stock if norm_key(e) not in prop_norm]
            extra   = [p for p in proposed if norm_key(p) not in stock_norm]

            if missing or extra:
                fb_parts = []
                if missing:
                    fb_parts.append(f"éléments MANQUANTS : {missing}")
                if extra:
                    fb_parts.append(f"éléments en TROP : {extra}")
                feedback = (
                    f"Problème d'exhaustivité — {' | '.join(fb_parts)}. "
                    f"Tu DOIS utiliser exactement ces {nb_elements} éléments : "
                    f"{stock_str}"
                )
                print(f"   ⚠️ Exhaustivité KO : manquants={missing} extras={extra}")

                # Correction Python immédiate
                proposed = [p for p in proposed if norm_key(p) in stock_norm]
                for m in missing:
                    proposed.append(m)
                print(f"   🔧 Corrigé Python → {proposed}")
                score = max(1, score - 3)

            # Normalise vers textes EXACTS
            proposed = [stock_map.get(norm_key(p), p) for p in proposed]

            # ── Score de confiance ───────────────────────────────────
            if score >= 7:
                print(f"   ✅ Score {score}/10 ≥ 7 → phrase validée")
                best_actions = proposed
                best_score   = score
                break
            else:
                if not (missing or extra):
                    feedback = (
                        f"Score trop bas ({score}/10). Raison : '{raison}'. "
                        f"Trouve un meilleur ordre pour : {stock_str}"
                    )
                print(f"   ⚠️ Score {score}/10 < 7 → nouvelle tentative")
                if score > best_score:
                    best_score   = score
                    best_actions = proposed

        # ── Filet final ──────────────────────────────────────────────
        if best_actions:
            best_norm = [norm_key(e) for e in best_actions]
            missing_final = [e for e in stock if norm_key(e) not in best_norm]
            extra_final   = [p for p in best_actions if norm_key(p) not in stock_norm]
            if missing_final:
                best_actions.extend(missing_final)
                print(f"   🔧 Filet final : rajouté {missing_final}")
            if extra_final:
                best_actions = [p for p in best_actions if norm_key(p) in stock_norm]
                print(f"   🔧 Filet final : retiré extras {extra_final}")
            best_actions = [stock_map.get(norm_key(p), p) for p in best_actions]
            print(f"   📋 Final phrase anagramme (score={best_score}/10) : "
                  f"{best_actions}")
            return {"actions": best_actions}

        print("   💀 Phrase anagramme échouée → ordre original")
        return {"actions": stock}

    # =================================================================
    #  ORCHESTRATEUR PRINCIPAL — 2 PASSES
    # =================================================================
    def ask_gemini(
        self, image, context_text: str = "",
        code_options: list[str] | None = None,
        image_elements: list | None = None,
    ) -> dict | None:
        if code_options is None:
            code_options = []
        if image_elements is None:
            image_elements = []
        set_current_stock(code_options)
        self._p1_lite = False

        print(f"🧠 ═══ ANALYSE [{self._get_status()}] ═══")

        page_text = self._get_page_text()[:3000]
        options = list(code_options)
        nb_images = len(image_elements)
        if nb_images > 0:
            options.extend([f"IMG_{i+1}" for i in range(nb_images)])

        # nb_dz avec retry — rendu parfois asynchrone
        nb_dz = len(self._get_drop_zones())
        if nb_dz == 0 and code_options:
            time.sleep(0.5)
            nb_dz = len(self._get_drop_zones())
        nb_ti = len(self._get_text_inputs())
        options_str = json.dumps(options, ensure_ascii=False)

        single_letters = [o for o in options if len(o.strip()) == 1]
        multi_words    = [o for o in options
                          if len(o.strip()) > 1 and not o.startswith("IMG_")]
        img_tokens     = [o for o in options if o.startswith("IMG_")]

        # ── DÉTECTION IMAGE_CHOICE en priorité ────────────────────────
        is_image_choice = (
            nb_images >= 2
            and len(img_tokens) >= 2
            and len(multi_words) == 0
            and len(single_letters) == 0
        )

        # Séparation : lettres (anagramme) vs phrases
        is_letter_anagram = (
            not is_image_choice
            and len(options) > 1
            and len(single_letters) / max(len(options), 1) > 0.7
        )

        phrase_elements = [o for o in multi_words if " " in o.strip()]

        # ── DÉTECTION QCM-RADIO (priorité sur phrase_anagram) ─────────
        # Si les options sont des LABELS DE RADIOS/CHECKBOXES visibles,
        # c'est un QCM à choix unique (même si les options sont des
        # phrases longues). On ne doit PAS l'envoyer au pipeline
        # phrase_anagram qui retournerait toutes les phrases à ordonner.
        try:
            visible_radio_labels = (
                self.driver.execute_script(JS_SCAN_RADIO_LABELS) or []
            )
            nb_radio_labels = len(visible_radio_labels)
        except WebDriverException:
            visible_radio_labels = []
            nb_radio_labels = 0

        is_radio_qcm = (
            not is_image_choice
            and not is_letter_anagram
            and nb_dz == 0 and nb_ti == 0
            and len(options) >= 2
            and nb_radio_labels >= max(2, int(len(options) * 0.8))
        )

        # ── VÉRIFICATION TEXTUELLE : les textes des radios doivent
        # correspondre aux options du stock. Sinon ce sont des radios
        # d'un autre contexte (menu, reste d'exo précédent, etc.) et
        # l'exercice courant n'est PAS un QCM radio.
        # Cas visé : reorder chronologique qui a 0 dropzone + plusieurs
        # phrases + radios parasites sur la page.
        if is_radio_qcm:
            def _match_key(s: str) -> str:
                s = (s or "").lower().strip()
                # Enlève ponctuation + normalise espaces
                s = re.sub(r"[^\w\s]", "", s, flags=re.UNICODE)
                s = re.sub(r"\s+", " ", s).strip()
                return s

            options_keys = {_match_key(o) for o in options if o}
            options_keys.discard("")
            matching = 0
            for t in visible_radio_labels:
                tk = _match_key(t)
                if not tk:
                    continue
                # Match exact
                if tk in options_keys:
                    matching += 1
                    continue
                # Match tolérant (ponctuation, espaces)
                close = difflib.get_close_matches(
                    tk, list(options_keys), n=1, cutoff=0.85,
                )
                if close:
                    matching += 1

            min_matches = max(2, int(len(options) * 0.6))
            if matching < min_matches:
                print(f"   🔍 QCM radio écarté : seulement "
                      f"{matching}/{len(options)} textes radios alignés "
                      f"avec les options (seuil {min_matches}) "
                      f"— c'est probablement un reorder/autre")
                is_radio_qcm = False
            else:
                print(f"   📻 QCM radio confirmé : "
                      f"{matching}/{len(options)} textes alignés — "
                      f"UNE SEULE réponse attendue")

        # ── QCM MULTI-RÉPONSES (checkboxes / "One or many answers") ──
        # Indépendant de is_radio_qcm : un QCM peut être à choix multiple
        # qu'il passe par le pipeline radio ou par le prompt principal.
        is_multi_select = self._is_multi_select_qcm()
        if is_multi_select:
            print("   ☑️  QCM MULTI-RÉPONSES détecté "
                  "(checkboxes / 'one or many') — plusieurs réponses permises")

        is_phrase_anagram = (
            not is_letter_anagram
            and not is_image_choice
            and not is_radio_qcm              # QCM radio a priorité
            and len(phrase_elements) > 1
            and len(phrase_elements) == len(multi_words)
            and nb_dz == 0 and nb_ti == 0
            and len(img_tokens) == 0
        )
        is_anagram = is_letter_anagram

        # ── Pipelines dédiés ──────────────────────────────────────────
        # QCM dont les options sont des IMAGES (« M5-S10-3.jpg ») : chaque
        # image est envoyée séparément, numérotée, avec 2 avis + départage.
        if (len(code_options) >= 2 and all(re.search(
                r"\.(jpe?g|png|webp|gif)$", str(o).strip(), re.I)
                for o in code_options)):
            res_img = self._solve_image_radio(page_text, list(code_options))
            if res_img:
                return res_img
        if is_image_choice:
            return self._solve_image_choice(image, page_text, image_elements)
        if is_letter_anagram:
            return self._solve_letter_anagram(
                image, page_text, single_letters, nb_images,
            )
        if is_phrase_anagram:
            return self._solve_phrase_anagram(
                image, page_text, multi_words, nb_images,
            )

        # =============================================================
        #  PASSE 1 — RÉSOLUTION C1
        # =============================================================
        grammar_rules = GRAMMAR_C1.get(self.langue, GRAMMAR_C1["es"])

        # Transcript mémorisé sur la page d'intro : on le fournit pour
        # que les questions de compréhension ("not mentioned", "what did
        # he say", etc.) puissent être résolues même si le texte source
        # n'est plus à l'écran.
        # D'abord, capter le dialogue/texte éventuellement visible (ajout
        # à l'accumulateur, avec dédup). Puis construire le bloc regroupant
        # TOUS les textes mémorisés de la leçon (transcripts + dialogues).
        try:
            self._update_reading_passage()
        except Exception:
            pass
        lesson_block = self._build_lesson_texts_block()

        # Règle de cardinalité du QCM : mono-réponse par défaut, mais
        # multi-réponses si la page le permet (checkboxes / mention
        # "One or many answers possible").
        if is_multi_select:
            qcm_rule = (
                "⚠️ QCM MULTI-RÉPONSES (la page indique \"One or many "
                "answers possible\" / cases à cocher) : il peut y avoir "
                "PLUSIEURS bonnes réponses. Mets dans \"actions\" TOUTES "
                "les options correctes (1, 2, 3… selon le cas), copiées "
                "EXACTEMENT. N'en oublie aucune et n'en ajoute aucune de "
                "fausse."
            )
        else:
            qcm_rule = (
                "⚠️ RÈGLE ABSOLUE : QCM = exactement UNE SEULE réponse "
                "dans \"actions\" → [\"une_seule\"]"
            )

        prompt = f"""Tu es un CORRECTEUR OFFICIEL de certifications linguistiques niveau C1-C2 en {self.lang_name} (DELE, DELF, Cambridge C1, Goethe C1, CAPLE, CELI). Tu résous les exercices avec une précision absolue.

Langue : {self.lang_name.upper()} [{self.langue}]

{lesson_block}=== TEXTE DE LA PAGE ===
{page_text[:2500]}

{self._bold_hint_block()}=== STOCK / OPTIONS ===
{options_str}
Trous : {nb_dz} | Champs texte : {nb_ti} | Images : {nb_images}

=== RÉFÉRENTIEL GRAMMATICAL C1 — {self.lang_name.upper()} ===
{grammar_rules}

=== MÉTHODE D'ANALYSE ===

━━━ PHASE 1 — IDENTIFICATION ━━━
• Consigne exacte :
• Type : navigation / carousel / transcript / drag_drop / qcm / reorder_sentence / reorder_chronological / text_input / image_choice

━━━ PHASE 2 — RÉSOLUTION C1 ━━━

▶ DRAG_DROP — Pour chaque trou :
  a) Phrase complète avec ___ pour les trous
  b) Analyse morphosyntaxique de chaque trou :
     → Mode requis (indicatif/subjonctif/conditionnel/infinitif) — justifie selon le référentiel C1
     → Temps verbal — justifie (aspect perfectif/imperfectif, antériorité, hypothèse)
     → Accord (genre/nombre/personne)
     → Régime prépositionnel si applicable
  c) Teste CHAQUE option — laquelle est grammaticalement et sémantiquement parfaite ?
  d) {nb_dz} trou(s) → {nb_dz} réponse(s) exactement
  ⚠️ BIJECTION : si le nombre de tuiles du stock ÉGALE le nombre de trous,
     chaque tuile sert EXACTEMENT UNE FOIS — JAMAIS deux fois la même, et
     aucune oubliée. Avant de répondre, vérifie que tes {nb_dz} réponses
     sont {nb_dz} tuiles DISTINCTES couvrant tout le stock. Ex : stock
     ['stored','fly','sail','shipped'] / 4 trous → utilise les 4 (ne répète
     pas 'fly', n'oublie pas 'sail').
  ⚠️ CAS PARTICULIER — MATCHING / APPARIEMENT (consigne "Match … to …",
     "Match the … to their …", "Associez", "Reliez", "Relie", "Empareja",
     "Ordnen Sie zu", "Abbina", deux colonnes A → B reliées par des flèches) :
     • Ce N'EST PAS de la grammaire : c'est un appariement par le SENS
       (un verbe/terme de la colonne A ↔ sa DÉFINITION/synonyme en colonne B).
     • La colonne A (gauche) est FIXE, dans son ordre d'affichage. Tu dois
       fournir les éléments de la colonne B dans l'ORDRE qui aligne chaque
       B sur le A de la MÊME LIGNE : réponse 1 = la définition du 1er terme
       de A, réponse 2 = celle du 2e, etc. ({nb_dz} lignes → {nb_dz} réponses).
     • Procède ligne par ligne : pour CHAQUE terme de A, identifie sa
       définition exacte en B, puis range les B dans cet ordre. Vérifie
       qu'aucune définition n'est utilisée deux fois (bijection stricte).
     • Exemple : A=[cope with, abide by, yearn for, vent something, resort to]
       B disponibles=[deal with, obey a decision or a rule, strongly desire,
       release anger, turn to in desperation]
       → cope with=deal with ; abide by=obey a decision or a rule ;
         yearn for=strongly desire ; vent something=release anger ;
         resort to=turn to in desperation.
       actions (dans l'ordre de A) = ["deal with","obey a decision or a rule",
       "strongly desire","release anger","turn to in desperation"].

▶ QCM — Analyse différentielle C1 :
  {qcm_rule}
  ⚠️ INDICES DE QCM (ne PAS confondre avec reorder_sentence) :
    • Consigne avec "Click", "Select", "Choose", "Pick the correct", "Which of",
      "What is", "Quelle est", "Selecciona", "Wähle", "Scegli", "Escolha"
    • Les options peuvent être des PHRASES ENTIÈRES, pas forcément des mots courts
    • Si plusieurs phrases sont proposées comme CHOIX → QCM (PAS reorder)
    • reorder_sentence a pour but de REMETTRE DANS L'ORDRE une phrase éclatée ;
      QCM a pour but de CHOISIR la bonne phrase parmi plusieurs variantes
  Si "erreur" → l'erreur est souvent SUBTILE : une préposition, un mode, un accord de genre
  Si "not mentioned" / "n'a pas été mentionné" → cherche LA/LES option(s)
     ABSENTE(S) du texte lu/entendu (sur un QCM multi-réponses, il peut y
     en avoir plusieurs : renvoie-les TOUTES). Appuie-toi sur le TRANSCRIPT
     mémorisé ci-dessus s'il est fourni.
  Si "best sums up" / "which sentence summarizes" / "idée principale" /
     "meilleur résumé" → choisis l'option cohérente avec la GLOBALITÉ du
     texte (la situation d'ensemble, pas un détail). MÉFIE-TOI des
     distracteurs : une option peut sembler plausible mais (a) CONTREDIRE
     le texte, ou (b) n'être vraie qu'en PARTIE. Vérifie chaque option
     contre le texte mémorisé : si elle affirme un fait que le texte
     dément (ex : "les ouvriers sont retournés au travail" alors que le
     texte dit que la grève s'aggrave), élimine-la. La bonne réponse doit
     être compatible avec TOUS les éléments du texte.
  Si "bonne réponse" → élimination par la grammaire et le sens
  Analyse chaque option une par une ; sur QCM mono-réponse mets UNIQUEMENT
  la meilleure, sur QCM multi-réponses mets TOUTES les correctes dans actions

▶ REORDER_SENTENCE ({len(options)} éléments) :
  ⚠️ VÉRIFICATION CRITIQUE : avant de répondre, COMPTE les éléments
     visibles dans l'IMAGE (chaque boîte/mot numéroté). Si tu vois N
     boîtes dans l'image mais le stock listé contient M < N éléments,
     un mot a été manqué par le scan → signale-le dans "raisonnement"
     avec "STOCK_INCOMPLET: il manque <mot>" et utilise quand même
     UNIQUEMENT les mots du stock fourni.
  ⚠️ CASSE / MAJUSCULES — règle stricte :
     Le stock peut contenir DEUX variantes de casse pour un même mot
     (ex: ['you', 'You', 'we', 'We']). Choisis ALORS la bonne :
     - Variante MAJUSCULE INITIALE (ex: 'You', 'We', 'They') →
       UNIQUEMENT en début de phrase OU après un point/question/
       exclamation interne (ex: "Run! We will...").
     - Variante MINUSCULE (ex: 'you', 'we') → partout ailleurs.
     Choisir la mauvaise variante = réponse INCORRECTE même si
     l'ordre est juste. Vérifie chaque position avant de finaliser.
  Applique l'ordre canonique C1 de la langue :
  - ⚠️ DÉCLARATIF vs INTERROGATIF — REGARDE LA PONCTUATION FINALE DU STOCK :
    • Si le stock contient un fragment "." (ou "!") et PAS de "?", la
      phrase est DÉCLARATIVE (ou exclamative) → ordre NORMAL Sujet-Verbe,
      AUCUNE inversion sujet/auxiliaire. Ne fronte PAS "is"/"will"/"do"…
      comme dans une question. Le "." (ou "!") va en DERNIÈRE position.
      Ex stock = ['is','the','.','will','raw','material supply','steady',
      'except','which','not happen,','a coup,']
      → "The raw material supply is steady, except a coup, which will not
         happen." (sujet "The raw material supply" + "is steady", puis la
         subordonnée relative "which will not happen" ; "will" et "is"
         restent APRÈS leur sujet — PAS en tête).
      → FAUX : "Is the … will …" (fronter l'auxiliaire = forme
         interrogative, INCORRECTE ici car la phrase finit par ".").
    • La forme interrogative (inversion) ne s'applique QUE si un "?" est
      présent dans le stock (voir ci-dessous).
    • Le fragment de ponctuation finale (. ! ?) se place TOUJOURS en
      dernier ; ne le place jamais au milieu de la phrase.
  - ⚠️ ORDRE DES PROPOSITIONS : la PROPOSITION PRINCIPALE (sujet + verbe
    conjugué) vient d'ABORD ; une subordonnée de but/conséquence/relative
    introduite par "so that"/"because"/"which"/"that"/"pour que"/"parce
    que" vient APRÈS l'élément dont elle dépend, jamais en tête d'une
    déclarative. Ex : ['Bryan','will','be','joining','my','meeting','this
    afternoon,','so','that','we can','coordinate','everything','.']
    → "Bryan will be joining my meeting this afternoon, so that we can
       coordinate everything." (principale "Bryan will be joining…" en
       premier, PUIS "so that we can coordinate everything").
    → FAUX : "We can coordinate everything so that … will be joining…"
       (subordonnée mise avant la principale).
  - ⚠️ STRUCTURE INTERROGATIVE — PRIORITÉ ABSOLUE (avant la règle de
    majuscule), UNIQUEMENT si le stock contient "?" (ou "¿"/"؟") : la
    phrase est une QUESTION. Le "?" va TOUJOURS en DERNIÈRE position, et il
    faut l'INVERSION sujet/auxiliaire — ne te contente JAMAIS d'ordonner
    comme une affirmation puis de coller "?" à la fin.
    • ANGLAIS, question fermée (yes/no) : l'AUXILIAIRE ou le MODAL
      (do/does/did/will/would/can/could/shall/should/is/are/was/were/
      have/has/had/am) se place EN TÊTE, AVANT le sujet.
      Ex stock = ['Jack and Amelia','will','the ideas presented',
      'by Emily','convince','?']
      → "Will the ideas presented by Emily convince Jack and Amelia?"
        (sujet = "the ideas presented by Emily", PAS "Jack and Amelia").
      → FAUX : "Jack and Amelia will convince the ideas presented by Emily?"
        (ordre déclaratif + "?" = structure interrogative incorrecte).
    • ANGLAIS, question en wh- (what/who/whom/whose/which/where/when/
      why/how) : mot en wh- en 1er, puis auxiliaire, puis sujet, puis verbe.
    • La règle "majuscule = 1ère position" NE s'applique PAS si elle
      contredit l'inversion : dans une question, c'est l'auxiliaire ou le
      mot interrogatif qui OUVRE la phrase (sa casse sera gérée par le
      site), et un nom propre capitalisé ('Jack', 'Emily', 'Jack and
      Amelia') reste à sa place grammaticale (sujet/objet), PAS forcément
      en tête.
  - Pronoms clitiques : position STRICTEMENT définie (voir référentiel)
  - Adverbes de temps : début de phrase ou fin
  - Négation : position fixe selon la langue
  - ⚠️ CASSE / MAJUSCULES — NON FIABLE sur cette plateforme, NE T'Y FIE PAS :
    → C'est la GRAMMAIRE et le SENS qui déterminent l'ordre des mots,
      PAS la majuscule. Sur ce site, le 1er fragment est SOUVENT donné en
      MINUSCULE, et des mots capitalisés (noms propres, "I", mais aussi des
      noms communs comme "Embassy", "Parliament", "Internet") apparaissent
      EN PLEIN MILIEU de la phrase.
    → Une phrase qui COMMENCE par un fragment en minuscule est tout à fait
      NORMALE. Ne place donc PAS d'office un mot capitalisé en 1ère
      position.
    → Un mot capitalisé ne va en 1ère position QUE si la grammaire l'exige
      (sujet d'une affirmation, ex. "We"/"Je"/"I" sujet en tête). Sinon il
      reste à sa place grammaticale (objet, complément, milieu...).
    → Ex (le piège classique) : stock = ['Embassy','in','the','wake',
      'of the','election, I','decided','to','contact','my','.']
      → "In the wake of the election, I decided to contact my Embassy."
        ("in" ouvre la phrase BIEN qu'en minuscule ; "Embassy" est l'OBJET
        en fin ; "I" est sujet AU MILIEU). 
      → FAUX : "Embassy in the wake … contact my ." (mettre "Embassy" en
        1er juste parce qu'il a une majuscule).
    → Cas où la majuscule aide quand même : si le SEUL mot capitalisé est
      un pronom sujet évident ("We"/"I"/"Je") et que la grammaire en fait
      le sujet de tête, alors oui il ouvre la phrase.
  - MARQUEURS ESPAGNOLS : ¡ et ¿ indiquent le DÉBUT d'une exclamation/question
    → L'élément commençant par ¡ ou ¿ doit être EN PREMIÈRE POSITION
    → Ex: "¡No te metas..." → ¡No est forcément le 1er mot
  - TOUS les {len(options)} éléments obligatoires, dans l'ordre syntaxique parfait
  - JAMAIS inventer un mot qui n'est PAS dans le stock (même si la
    phrase naturelle en contiendrait un de plus : reste fidèle au stock)
  Phrase finale : un locuteur natif C1 la dirait-il naturellement ?

▶ REORDER_CHRONOLOGICAL ({len(options)} éléments) :
  Logique discursive : chaque réplique/événement doit découler naturellement du précédent
  Connecteurs temporels/logiques comme indices d'ordre

▶ TEXT_INPUT :
  Tu dois ÉCRIRE le(s) mot(s) à taper dans le(s) champ(s). Mets dans
  "actions" UNIQUEMENT le(s) mot(s)/expression(s) à saisir, rien d'autre.
  1) LIS D'ABORD LA CONSIGNE — elle définit la contrainte :
     • "a word that means X" / "qui signifie X" / "synonyme de X" /
       "un mot pour X" → écris un SYNONYME de X (PAS X lui-même si un
       synonyme plus idiomatique existe) qui s'insère naturellement.
       Ex : consigne "means: possible", phrase "...will attract ___ new
       customers" → "potential" (clients potentiels), pas "possible".
     • "the opposite of X" / "le contraire de X" / "antonyme" → ANTONYME.
     • "another word for" / "in other words" → reformulation équivalente.
     • Sinon (pas de contrainte lexicale) → simple complétion grammaticale.
  2) LE MOT DOIT S'INSÉRER PARFAITEMENT dans la phrase :
     • bonne nature (nom/adjectif/verbe/adverbe) selon le trou ;
     • bon nombre/genre, bonne forme conjuguée, orthographe + accents
       exacts ;
     • un natif C1 écrirait-il ce mot ici ? (idiomaticité).
  3) ⚠️ FORME VERBALE GOUVERNÉE par le mot qui précède (cause fréquente
     d'erreur) — regarde le mot juste AVANT le trou :
     • mind / enjoy / avoid / finish / keep / suggest / consider / can't
       help / look forward to / be used to + VERBE-ING (gérondif).
       Ex : "Would you mind ___ questions" → "answering" (PAS "answer").
     • want / decide / hope / need / would like / manage / refuse + TO +
       base. Ex : "decided ___ leave" → "to leave".
     • modal (will/would/can/must/should) + base verbale (sans "to").
     • préposition (to/of/about/in/for...) + VERBE-ING en anglais.
     Identifie le déclencheur AVANT de choisir la forme.
  4) ⚠️ COLLOCATION (cause fréquente d'erreur sur les synonymes) : choisis
     le mot que les NATIFS emploient RÉELLEMENT dans CETTE combinaison
     précise, pas n'importe quel synonyme du dictionnaire.
     Ex : "fighting to ___ poverty" → "eradicate" (collocation standard
     "eradicate poverty"), PAS "terminate"/"end"/"finish" (mauvaise
     collocation même si sens proche). "make a decision", "do homework",
     "heavy rain"... : respecte la collocation usuelle.
  5) Respecte la CASSE attendue (majuscule si début de phrase).
  6) Un champ → une seule réponse ; N champs → N réponses dans l'ordre.
  Forme morphologique exacte + orthographe + accents + accords.

▶ IMAGE_CHOICE :
  IMG_1=gauche, IMG_2=milieu, IMG_3=droite — une seule réponse

━━━ PHASE 3 — TRIPLE VÉRIFICATION C1 ━━━
□ GRAMMATICALITÉ : conforme au référentiel C1 ci-dessus ?
□ ADÉQUATION : répond exactement à la consigne ?
□ IDIOMATICITÉ : un natif C1 utiliserait cette formulation ?
□ EXHAUSTIVITÉ : tous les éléments du stock utilisés (reorder) ?
□ PRÉCISION : textes copiés mot pour mot depuis le stock ?

=== FORMAT DE SORTIE ===
⚠️ TA RÉPONSE DOIT COMMENCER DIRECTEMENT PAR {{ ET SE TERMINER PAR }}.
⚠️ AUCUN TEXTE AVANT, AUCUN TEXTE APRÈS. PAS DE ```, PAS DE ━━━ PHASE.
⚠️ ORDRE DES CHAMPS IMPÉRATIF : "type" puis "actions" puis "raisonnement".
   Mets "actions" AVANT "raisonnement" pour que la réponse reste exploitable
   même si elle est coupée. Le "raisonnement" doit être COURT : 1 phrase
   maximum (≤ 30 mots). Ne déroule PAS les 3 phases en détail.

{{"type": "le_type", "actions": ["réponse1", "réponse2"], "raisonnement": "1 phrase courte"}}
JSON UNIQUEMENT. Règles absolues pour "actions" :
- Copie le texte EXACT du stock, rien d'autre
- INTERDIT : "click_radio_button:X", "select:X", "click_X", "drag:X" ou tout préfixe d'action
- CORRECT : ["compañero"] | INCORRECT : ["click_radio_button:compañero"] ou ["click_radio_button_2"]
- Jamais Validate/Valider/Submit dans actions
navigation→{{"type":"navigation","actions":["Next"]}}
carousel→{{"type":"carousel","special":"CAROUSEL"}}
transcript→{{"type":"transcript","transcript_action":true}}"""

        # ── VISION CONDITIONNELLE ─────────────────────────────────────
        # On n'envoie la capture d'écran à la passe 1 QUE si utile : exo
        # avec images-réponses (nb_images) ou image de contenu (/media/).
        # Sinon (exo 100 % texte : QCM de transcript, reorder, drag de
        # mots), on l'omet → ~4400 tokens d'entrée en moins. Le texte de
        # page + le stock + les transcripts (déjà dans le prompt) suffisent
        # — la passe 2, qui n'a jamais l'image, le prouve. "always" force.
        p1_image = image
        if (VISION_MODE == "auto" and not nb_images
                and not self._exercise_has_image()):
            p1_image = None
            print("   👁️  Vision conditionnelle : exo texte → capture NON "
                  "envoyée (−tokens)")

        # ── EXERCICE FACILE ? → passe 1 sur Flash-Lite ───────────────
        # Tout est décidé AVANT l'appel, d'après l'analyse de la page :
        # pas d'image, pas de champ texte, pas de QCM multi-réponses,
        # aucun texte de leçon à comprendre (transcript, lecture), et
        # des réponses courtes.
        p1_model = None
        if LITE_FOR_EASY and code_options:
            words = [len(str(o).split()) for o in code_options]
            simple_screen = (
                p1_image is None and not nb_images and not img_tokens
                and nb_ti == 0 and not is_multi_select
                and not lesson_block.strip()
            )
            easy_qcm = (is_radio_qcm and 2 <= len(code_options) <= 6
                        and max(words) <= 3)
            easy_reorder = (not is_radio_qcm and nb_dz <= 1
                            and 2 <= len(code_options) <= 6
                            and max(words) <= 2)
            if simple_screen and (easy_qcm or easy_reorder):
                p1_model = MODEL_EASY
                self._p1_lite = True
                print(f"   🪶 Exercice facile "
                      f"({'QCM court' if easy_qcm else 'reorder court'}) "
                      f"→ passe 1 sur Flash-Lite, vérification sur Flash")

        raw = self._call_gemini(prompt, p1_image, model_name=p1_model)
        if not raw:
            return None
        print(f"   📨 Passe 1 : {raw[:200]}")

        result = self._extract_json(raw)
        if not result:
            # La réponse n'était pas parsable (souvent : tronquée car le
            # raisonnement a épuisé le budget). Avant de tomber sur le
            # fallback positionnel — qui devine l'ordre depuis le texte
            # tronqué et donne un ordre FAUX pour un drag_drop/reorder —
            # on RELANCE une fois en exigeant un JSON compact, actions en
            # premier, SANS raisonnement.
            print("   ⚠️ JSON introuvable (réponse tronquée ?) → "
                  "relance concise JSON-only")
            concise = (
                prompt
                + "\n\n=== RÉPONDS MAINTENANT ===\n"
                  "UNIQUEMENT le JSON, sur UNE ligne, \"actions\" EN "
                  "PREMIER, et \"raisonnement\":\"\" (VIDE). N'écris RIEN "
                  "d'autre. Format : "
                  "{\"type\":\"...\",\"actions\":[...],\"raisonnement\":\"\"}"
            )
            raw2 = self._call_gemini(concise, p1_image, temperature=0.2)
            if raw2:
                print(f"   📨 Relance : {raw2[:160]}")
                result = self._extract_json(raw2)
            if not result:
                print("   ⚠️ Relance infructueuse — bascule sur "
                      "fallback_match")
                result = self._fallback_match(raw, options)
                if not result:
                    # Diagnostic : pourquoi même le fallback rate ?
                    print(f"   ❌ Fallback échoué — réponse brute "
                          f"(fin) : ...{raw[-300:]!r}")
                    return None
                else:
                    print(f"   ✅ Fallback : trouvé {result.get('actions')}")

        t = result.get("type", "")
        if t == "carousel" or result.get("special") == "CAROUSEL":
            return {"special": "CAROUSEL"}
        if t == "transcript" or result.get("transcript_action"):
            return {"transcript_action": True}

        if t == "navigation":
            # PROTECTION : normalement si le stock n'est pas vide, ce
            # n'est pas une navigation. MAIS sur les pages de feedback
            # ("Well done!", "Nice work!"), le stock de l'exo précédent
            # peut rester affiché. On autorise donc la navigation si :
            #  - Les actions sont des mots de navigation (Next, Finish,
            #    Continue, etc.) qui ne correspondent PAS à une option
            #    du stock.
            nav_actions = result.get("actions") or ["Next"]
            nav_words_set = {
                "next", "suivant", "continue", "continuer",
                "finish", "terminer", "move on",
                "move on to the next", "next activity",
                "see results", "consulter les résultats",
            }
            stock_lower = {str(o).lower().strip() for o in (code_options or [])}
            action_is_nav = (
                len(nav_actions) == 1
                and clean_ai(nav_actions[0]).lower().strip() in nav_words_set
                and clean_ai(nav_actions[0]).lower().strip() not in stock_lower
            )

            if code_options and not action_is_nav:
                print(f"   ⚠️ Type 'navigation' refusé : stock non vide "
                      f"({code_options[:2]}...) → forcé en QCM")
                result["type"] = "qcm"
            else:
                if "actions" not in result:
                    result["actions"] = ["Next"]
                if code_options and action_is_nav:
                    print(f"   ➡️  Navigation confirmée : action "
                          f"'{nav_actions[0]}' n'est pas dans le stock "
                          f"(probable page 'Well done!')")
                return {
                    "actions": result["actions"],
                    "exercise_type": "navigation",
                }

        if "raisonnement" in result:
            print(f"   💭 {str(result['raisonnement'])[:250]}")
        if "actions" not in result:
            return None

        # Nettoyage + filtre validate/skip (tolérance dicts Gemini)
        # On protège les mots présents dans le stock : "check", "finish",
        # etc. peuvent être des mots d'exo légitimes.
        normalized = self._normalize_actions_list(result["actions"])
        stock_keys = {norm_key(o) for o in (code_options or [])}
        result["actions"] = [
            clean_ai(a) for a in normalized
            if clean_ai(a).strip()
            and (norm_key(clean_ai(a)) in stock_keys
                 or (not is_validate_button_word(clean_ai(a))
                     and not is_skip_word(clean_ai(a))))
        ]

        # =============================================================
        #  PASSE 2 — Résolution indépendante
        # =============================================================
        exercise_type = result.get("type", "")
        actions_a = result["actions"]

        # ── OVERRIDE QCM-RADIO ─────────────────────────────────────────
        # Si le DOM indique N radio-labels ≈ N options, c'est un QCM.
        # On force le type même si Gemini a proposé "reorder_sentence"
        # ou autre, et on tronque à 1 action immédiatement.
        if is_radio_qcm:
            if exercise_type not in ("qcm", "image_choice"):
                print(f"   🔧 Type '{exercise_type}' → forcé en 'qcm' "
                      f"(QCM radio détecté au DOM)")
                exercise_type = "qcm"
            if len(actions_a) > 1 and not is_multi_select:
                print(f"   🔧 QCM radio : {len(actions_a)} réponses → 1 seule")
                actions_a = actions_a[:1]
            elif len(actions_a) > 1 and is_multi_select:
                print(f"   ☑️  QCM radio MULTI : {len(actions_a)} réponses "
                      f"conservées")

        skip_double_pass_multi = (exercise_type == "qcm" and is_multi_select)

        # ── DÉTECTION MATCHING (appariement colonne A → colonne B) ──────
        # Un matching est un drag_drop où l'ORDRE des réponses = l'affectation
        # aux zones (ligne A1→zone1...). L'auto-critique et l'arbitrage sont
        # conçus pour RÉORDONNER une phrase : appliqués à un matching, ils
        # détruisent l'alignement (bug observé : une réponse correcte
        # réordonnée par l'auto-critique). On saute donc le second-guessing
        # pour le matching — la passe 1 (avec la consigne d'appariement) est
        # fiable.
        raison_a = str(result.get("raisonnement", "")).lower()
        page_low = (page_text or "").lower()
        MATCH_PAGE_KW = (
            "match the", "match each", "match column", "match terms",
            "column a", "column b", "to their definition",
            "associez", "reliez", "relie ", "reliez les",
            "empareja", "relaciona", "ordnen sie zu", "zuordnen", "abbina",
            "associa", "relacione",
        )
        MATCH_RAISON_KW = ("matching exercise", "match column",
                           "this is a matching", "appariement", "matching")
        matching_kw = (any(k in page_low for k in MATCH_PAGE_KW)
                       or any(k in raison_a for k in MATCH_RAISON_KW))
        is_matching = (exercise_type == "drag_drop" and matching_kw)

        # Un exercice avec des ZONES DE DROP est un drag_drop, même si
        # Gemini l'a étiqueté "fill_in_the_blank" / "text_input"
        # (étiquetage incohérent observé). Dans ce cas on FORCE la
        # double-passe (sauf appariement ou multi-select) : c'est un
        # placement de mots où une inversion est possible, et qui passait
        # sinon sans AUCUN second avis (bug observé :
        # "accordingly"/"Consequently" inversés sur un fill_in_the_blank).
        force_dp_drop = (
            (nb_dz or 0) > 0
            and exercise_type not in self.NEEDS_DOUBLE_PASS
            and not matching_kw
        )
        needs_dp = (exercise_type in self.NEEDS_DOUBLE_PASS) or force_dp_drop
        # Passe 1 faite par Flash-Lite → second avis Flash OBLIGATOIRE.
        if getattr(self, "_p1_lite", False) and exercise_type != "navigation":
            needs_dp = True

        skip_double_pass = skip_double_pass_multi or is_matching
        if getattr(self, "_p1_lite", False) and exercise_type != "navigation":
            skip_double_pass = False
        if needs_dp and not skip_double_pass:
            if force_dp_drop:
                print("   🔁 Zones de drop détectées (type mal étiqueté) "
                      "→ double-passe forcée")
            print("   🔁 Passe 2 indépendante (temp=0.4)...")
            result_b = self._resolve_independent(
                image, page_text, options_str, options,
                nb_dz, nb_ti, nb_images, single_letters, is_anagram,
            )

            if result_b and "actions" in result_b:
                actions_b = result_b["actions"]
                print(f"   📋 A = {actions_a}")
                print(f"   📋 B = {actions_b}")
                a_norm = [norm_key(x) for x in actions_a]
                b_norm = [norm_key(x) for x in actions_b]
                # ── PASSE 2 HORS-STOCK SUR UN QCM (ex: "Skip") ─────────
                # Sur un QCM, une réponse valide est forcément une option
                # RÉELLE du stock. Quand la passe 2 renvoie une valeur
                # hors-stock ("Skip" = esquive), ce n'est pas un avis mais
                # une ABSTENTION : elle ne doit PAS déclencher un arbitrage
                # capable d'écraser une passe 1 correcte (bug observé sur
                # les Vrai/Faux : A=True correct, B=Skip → arbitrage →
                # False faux). On ignore la passe 2 et on garde la passe 1.
                qcm_b_hors_stock = (
                    exercise_type == "qcm" and bool(code_options)
                    and not all(
                        bn in {norm_key(o) for o in code_options}
                        for bn in b_norm
                    )
                )
                if qcm_b_hors_stock:
                    print("   ⚠️ Passe 2 hors-stock (ex: 'Skip') → ignorée, "
                          "passe 1 retenue (pas d'arbitrage)")
                    actions = actions_a
                elif a_norm == b_norm:
                    # Deux passes INDÉPENDANTES (temp 0 puis temp 0.4)
                    # tombent sur la même réponse → signal déjà très fort.
                    # On retient directement, SANS auto-critique : ça
                    # économise 1 appel Gemini par question (cas le plus
                    # fréquent). Les garde-fous déterministes
                    # (_validate_actions_python : bijection, exhaustivité,
                    # doublons) tournent ensuite SANS appel API, donc la
                    # sécurité n'est pas perdue.
                    print("   ✅ A == B : deux passes d'accord → réponse "
                          "retenue (auto-critique sautée, −1 appel)")
                    actions = actions_a
                else:
                    # ── PRÉ-FILTRE BIJECTION ───────────────────────────
                    # Quand #tuiles == #trous (drag_drop), la bonne réponse
                    # est une PERMUTATION du stock : chaque tuile servie
                    # EXACTEMENT une fois. Si A ou B viole ça (doublon /
                    # omission) et pas l'autre, on tranche SANS arbitrage —
                    # l'arbitrage peut sinon préférer la version invalide
                    # (bug observé : A=['fly',...,'fly'] choisi au lieu de
                    # B correcte qui utilisait 'sail').
                    decided = False
                    if (exercise_type == "drag_drop" and not is_matching
                            and nb_dz >= 2
                            and len(code_options) == nb_dz):
                        stock_ms = sorted(norm_key(o) for o in code_options)
                        a_valid = sorted(a_norm) == stock_ms
                        b_valid = sorted(b_norm) == stock_ms
                        if a_valid and not b_valid:
                            print("   ✅ A = permutation valide du stock "
                                  "(B invalide : doublon/omission) → A")
                            actions = actions_a
                            decided = True
                        elif b_valid and not a_valid:
                            print("   ✅ B = permutation valide du stock "
                                  "(A invalide : doublon/omission) → B")
                            actions = actions_b
                            decided = True
                    # Ponctuation / majuscules : si une seule des deux
                    # réponses respecte « ¿ … ? », « ¡ … ! », le point
                    # final et la majuscule de début → c'est elle.
                    if (not decided and exercise_type in (
                            "reorder_sentence", "reorder")):
                        va = self._punct_violations(actions_a, code_options)
                        vb = self._punct_violations(actions_b, code_options)
                        if va != vb:
                            pick_a = len(va) < len(vb)
                            print(f"   ✒️ Ponctuation/majuscules : "
                                  f"{'A' if pick_a else 'B'} respecte mieux "
                                  f"les règles ({len(va)} vs {len(vb)} "
                                  f"faute(s)) → retenue")
                            actions = actions_a if pick_a else actions_b
                            decided = True
                    if not decided:
                        print("   ⚠️ A ≠ B : arbitrage C1...")
                        actions = self._arbitrate(
                            image, page_text, options_str, nb_dz,
                            exercise_type, actions_a, actions_b,
                        )
            else:
                print("   ⚠️ Passe 2 échouée → auto-critique sur A")
                actions = self._self_critique(
                    image, page_text, options_str, exercise_type,
                    nb_dz, actions_a, code_options,
                )
        else:
            actions = actions_a
            if is_matching:
                print("   🧩 Matching détecté → double-passe/auto-critique "
                      "sautée (l'ordre = affectation aux zones, passe 1 "
                      "fiable)")

        # Vérification dédiée TEXT_INPUT (le type n'a pas de double-passe) :
        # relecture ciblée forme verbale gouvernée + collocation, qui sont
        # les erreurs récurrentes observées (answer→answering,
        # terminate→eradicate).
        # ⚠️ UNIQUEMENT pour un VRAI champ à taper (nb_dz == 0). Si des
        # ZONES DE DROP sont présentes, c'est un DRAG mal étiqueté
        # "text_input" par Gemini (ex: 5 tuiles → 5 blancs) : la vérif
        # text_input, conçue pour UN seul mot tapé, massacrerait la
        # réponse multi-mots (bug observé : 5 mots réduits à 1, boucle
        # infinie). Le drag est déjà couvert par la double-passe forcée +
        # le validateur Python.
        if exercise_type == "text_input" and actions and nb_dz == 0:
            actions = self._verify_text_input(image, page_text, actions)
        # « Rewrite the word(s) in the correct order » : contrôle
        # déterministe contre les lettres mélangées affichées (même si
        # l'IA a mal étiqueté l'exercice, ex. « reorder_sentence »).
        # (le champ peut être DÉSACTIVÉ tant que l'audio n'est pas écouté :
        # on ne dépend donc pas de nb_ti ; la fonction ne fait rien hors de
        # l'exercice « rewrite the word in the correct order »).
        if actions and nb_dz == 0:
            actions = self._fix_unscramble(page_text, actions)
        # Phrase à remettre dans l'ordre déjà VUE sur le site (option d'un
        # exo précédent, vocabulaire de l'activité) → on suit cet ordre.
        if (actions and code_options and exercise_type in (
                "reorder_sentence", "reorder", "drag_drop", "sorting")):
            actions = self._reorder_from_seen(code_options, actions)
        if actions and exercise_type in ("reorder_sentence", "reorder"):
            errs = self._punct_violations(actions)
            if errs:
                # Réparation sûre : tuile de ponctuation seule → à la fin
                lone = [a for a in actions if str(a) in (".", "?", "!")]
                if len(lone) == 1 and str(actions[-1]) != lone[0]:
                    fixed = [a for a in actions if a is not lone[0]] + lone
                    if len(self._punct_violations(fixed)) < len(errs):
                        print(f"   ✒️ « {lone[0]} » déplacé en fin de "
                              f"phrase")
                        actions = fixed
                        errs = self._punct_violations(actions)
                if errs:
                    print(f"   ✒️ Attention ponctuation/majuscules : "
                          f"{'; '.join(errs[:3])}")

        # Vérification dédiée QCM MULTI-RÉPONSES (ce type saute aussi la
        # double-passe → sinon un seul avis, d'où les « trop / pas assez »
        # fréquents). Relecture qui re-juge CHAQUE option : retire les
        # fausses (anti-sur-sélection) et rajoute les correctes oubliées
        # (anti-sous-sélection). +1 appel, uniquement sur les QCM multi.
        if exercise_type == "qcm" and is_multi_select and actions:
            actions = self._verify_multi_select(
                image, page_text, code_options, actions,
            )

        # =============================================================
        #  FILET PYTHON FINAL + RETRY FORCÉ
        # =============================================================
        actions, problems = self._validate_actions_python(
            actions, code_options, exercise_type, nb_dz, is_anagram,
            allow_multi=is_multi_select,
        )
        if problems:
            print(f"   🔧 Corrections Python : {problems}")

        needs_forced_retry = any(
            "REORDER_INCOMPLET" in p or "DRAG_INCOMPLET" in p
            for p in problems
        )
        if needs_forced_retry and code_options:
            actions = self._forced_retry(
                image, page_text, exercise_type, nb_dz, code_options, actions,
            )

        # Capitalisation finale — seulement pour les exercices non-drag
        is_drag_exercise = exercise_type in (
            "drag_drop", "reorder_sentence", "reorder_chronological",
        ) or nb_dz > 0
        # Jamais pour un champ texte : _validate_actions_python a déjà
        # décidé (majuscule seulement si le trou est en début de phrase).
        # Avant, ce bloc remettait la majuscule → bug 'You sound Misled'.
        is_text_field = exercise_type == "text_input" or nb_ti > 0
        if (not is_drag_exercise and not is_text_field
                and actions and actions[0]
                and len(actions[0]) > 1 and actions[0][0].islower()):
            actions[0] = actions[0][0].upper() + actions[0][1:]
            print(f"   🔧 Capitalisation finale : {actions[0]}")

        print(f"   📋 FINAL : {actions}")
        return {
            "actions": actions,
            "exercise_type": exercise_type,
            "actions_raw": actions_a,  # trace de ce que Gemini a proposé
        }

    def _forced_retry(
        self, image, page_text: str, exercise_type: str, nb_dz: int,
        code_options: list[str], current_actions: list[str],
    ) -> list[str]:
        """Retry avec contrainte ABSOLUE : tous les éléments utilisés."""
        print("   🔄 Retry forcé — stock complet imposé à l'IA...")
        prompt = f"""Correcteur C1 {self.lang_name}. RÈGLE ABSOLUE : utilise TOUS les éléments du stock.

Stock COMPLET ({len(code_options)} éléments) : {json.dumps(code_options, ensure_ascii=False)}
Contexte : {page_text[:800]}
Type : {exercise_type}
{"Nombre de trous : " + str(nb_dz) if nb_dz > 0 else ""}

CONTRAINTE ABSOLUE : ta réponse doit contenir EXACTEMENT {len(code_options) if exercise_type != 'drag_drop' else nb_dz} élément(s).
Pour reorder : utilise les {len(code_options)} éléments dans le bon ordre syntaxique C1.
Pour drag_drop : sélectionne exactement {nb_dz} éléments du stock, un par trou.

INTERDIT : omettre un élément, ajouter un élément hors-stock, mettre "skip" ou "Next".

{{"type":"{exercise_type}","actions":{json.dumps(code_options if exercise_type != 'drag_drop' else code_options[:max(nb_dz,1)], ensure_ascii=False)}}}
→ Remets dans le BON ordre / sélectionne les bons. JSON uniquement."""

        raw = self._call_gemini(prompt, image, max_retries=3, temperature=0)
        if not raw:
            return current_actions

        rj = self._extract_json(raw)
        if not rj or "actions" not in rj:
            return current_actions

        forced = [clean_ai(a) for a in rj["actions"] if clean_ai(a).strip()]
        fa_norm = [norm_key(a) for a in forced]
        co_norm = [norm_key(o) for o in code_options]

        if exercise_type != "drag_drop":
            if all(o in fa_norm for o in co_norm):
                print(f"   ✅ Retry forcé réussi : {forced}")
                return forced
            # Rajoute les manquants à la fin
            still_missing = [
                o for o in code_options if norm_key(o) not in fa_norm
            ]
            if still_missing:
                forced.extend(still_missing)
                print(f"   🔧 Retry partiel, rajout final : {forced}")
            return forced

        # drag_drop : on veut exactement nb_dz actions
        if len(forced) == nb_dz:
            print(f"   ✅ Retry drag forcé réussi : {forced}")
            return forced
        return current_actions

    # =================================================================
    #  RECHERCHE D'UN ÉLÉMENT PAR TEXTE (utilisé pour clic/drag)
    # =================================================================
    def _find_element_by_text(self, text: str):
        """
        Trouve un élément dont le texte correspond au target.
        Exclut les éléments déjà utilisés (cache).

        MATCHING TOLÉRANT : l'apostrophe droite ("'") et l'apostrophe
        typographique ("'") sont considérées équivalentes. Les espaces
        multiples sont normalisés. Les comparaisons se font sur des
        clés normalisées via `_norm_apos_key`.
        """
        target = clean_ai(text).strip()
        if not target:
            return None

        # Clé normalisée : apostrophes uniformisées, espaces réduits,
        # casse ignorée — utilisée pour toutes les comparaisons.
        def _norm_apos_key(s: str) -> str:
            if not s:
                return ""
            s = s.lower().strip()
            # Unifier toutes les apostrophes en droite
            for apos in ("\u2019", "\u2018", "\u02BC", "`", "\u00B4"):
                s = s.replace(apos, "'")
            # Normaliser espaces
            s = re.sub(r"\s+", " ", s)
            # Retire ponctuation finale (matche 'later' avec 'later,') —
            # SAUF pour une tuile de ponctuation seule (« , » ≠ « . » ≠ « ! »)
            if any(ch.isalnum() for ch in s):
                while s and s[-1] in ",.!?;:":
                    s = s[:-1]
            return s.strip()

        target_key = _norm_apos_key(target)
        # Tolérance adaptative selon la longueur du target
        len_tolerance = 5 if len(target) >= 6 else 2

        # ── EXCLUSION DES ÉLÉMENTS DANS LA ZONE DE RÉPONSE ─────────────
        # Bug observé : quand on a cliqué un "D" du stock, le site le
        # déplace dans la zone de réponse. Au clic suivant sur "D",
        # _find_element_by_text trouvait LE D DÉJÀ PLACÉ (dans la zone
        # user-answer-container) et le recliquait → le site l'enlevait
        # (re-clic = toggle). On exclut donc les éléments qui sont
        # descendants d'une zone de réponse connue.
        #
        # ATTENTION : sur ce site, `exam-answer-container` désigne le
        # STOCK (les boutons à cliquer), PAS la zone de réponse.
        # La zone de réponse est `user-answer-container`. Il ne faut
        # PAS filtrer sur `exam-answer-container` sinon on exclut
        # tous les boutons du stock comme "déjà placés".
        #
        # Patterns de VRAIES zones de réponse :
        #  - data-name="user-answer-container" (reorder phrase)
        #  - classe "ordering-drop-zone" / "drop-zone"
        #  - classe "custom-dashed-border*" (wrapper de zone)
        answer_zone_xpath = (
            "//*[@data-name='user-answer-container'] | "
            "//*[contains(@class, 'ordering-drop-zone')] | "
            "//div[contains(@class, 'drop-zone')] | "
            "//div[contains(@class, 'custom-dashed-border')]"
        )
        answer_zones_elements = []
        try:
            answer_zones_elements = self.driver.find_elements(
                By.XPATH, answer_zone_xpath,
            )
        except WebDriverException:
            pass

        def _is_in_answer_zone(el) -> bool:
            """Vrai si `el` est descendant d'une zone de réponse."""
            if not answer_zones_elements:
                return False
            for az in answer_zones_elements:
                try:
                    if self.driver.execute_script(
                        "return arguments[0].contains(arguments[1]);",
                        az, el,
                    ):
                        return True
                except (JavascriptException, StaleElementReferenceException,
                        WebDriverException):
                    continue
            return False

        def _cache_ok(el) -> bool:
            """
            Vrai si `el` n'est pas déjà marqué comme utilisé (safe)
            ET n'est pas déjà dans une zone de réponse (déjà placé).
            """
            # Exclusion 1 : déjà cliqué/utilisé dans ce cycle
            for c in self.used_elements_cache:
                try:
                    if el == c:
                        return False
                except (StaleElementReferenceException, WebDriverException):
                    continue
            # Exclusion 2 : déjà déposé dans la zone de réponse
            if _is_in_answer_zone(el):
                return False
            return True

        # Pour les XPath : on génère TROIS variantes de target avec
        # 3 formes d'apostrophe (droite, typographique, sans apostrophe)
        variants = {target}
        if "'" in target:
            variants.add(target.replace("'", "\u2019"))
            variants.add(target.replace("'", ""))
        if "\u2019" in target:
            variants.add(target.replace("\u2019", "'"))
            variants.add(target.replace("\u2019", ""))

        # ── PRIORITÉ 0 : navigation (Next, Finish, Continue...) ──────
        # Pour les mots courts de navigation, chercher UNIQUEMENT un
        # <button>/<a>/<div role=button> dont le texte contient le
        # mot cible. Évite les matchs parasites (ex: bouton "Zoom"
        # sur les pages avec image) et le risque que P3 fuzzy
        # attribue un mauvais élément.
        NAV_TARGETS = {
            "next", "suivant", "continue", "continuer",
            "finish", "terminer", "move on",
            "move on to the next", "next activity",
            "see results", "consulter les résultats",
        }
        if target_key in NAV_TARGETS:
            lo = target_key
            xp = (
                f"//button[contains(translate(., "
                f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                f"'abcdefghijklmnopqrstuvwxyz'), '{lo}')] | "
                f"//a[contains(translate(., "
                f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                f"'abcdefghijklmnopqrstuvwxyz'), '{lo}')] | "
                f"//div[@role='button' and contains(translate(., "
                f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                f"'abcdefghijklmnopqrstuvwxyz'), '{lo}')]"
            )
            try:
                for el in self.driver.find_elements(By.XPATH, xp):
                    if not safe_is_displayed(el):
                        continue
                    if not _cache_ok(el):
                        continue
                    txt = safe_text(el).strip().lower()
                    # Filtre : on veut un bouton dont le texte CONTIENT
                    # le mot (pas juste un parent englobant). En plus,
                    # on évite les boutons parasites comme "Zoom".
                    if lo not in txt:
                        continue
                    # Rejette si le texte est suspect (contient "zoom"
                    # alors qu'on cherche "next", etc.)
                    PARASITES = {"zoom", "exit", "close", "fermer",
                                 "skip", "passer", "sauter", "help",
                                 "aide", "info", "menu", "back"}
                    if any(p in txt for p in PARASITES) and lo not in PARASITES:
                        continue
                    self.used_elements_cache.append(el)
                    return el
            except WebDriverException:
                pass

        # ── PRIORITÉ 0 bis : texte EXACT, majuscules ignorées ─────────
        # Avant toute recherche « contient » : bug observé, « Ama » cliquait
        # « amar » et « Está » cliquait « están » (le mot cherché est inclus
        # dans une autre option). Un élément dont le texte est EXACTEMENT
        # la réponse (casse ignorée) passe toujours en premier.
        try:
            exact_cands = self.driver.execute_script("""
                const norm = s => (s || '').toLowerCase()
                    .replace(/[\u2019\u2018\u02BC`\u00B4]/g, "'")
                    .replace(/\\s+/g, ' ').trim()
                    .replace(/([\\p{L}\\p{N}])[,.!?;:]+$/u, '$1').trim();
                const want = arguments[0];
                const out = [];
                for (const el of document.querySelectorAll(
                        'label, button, span, li, p, [role=button], '
                        + '[data-draggable-item-id], [data-sortable-item]')) {
                    const t = el.innerText;
                    if (!t || t.length > want.length + 6) continue;
                    if (norm(t) !== want) continue;
                    const r = el.getBoundingClientRect();
                    if (!r.width || !r.height) continue;
                    // le plus haut élément cliquable portant ce texte
                    const up = el.closest('label, button, [role=button], '
                        + '[data-draggable-item-id], [data-sortable-item]');
                    const inner = el.querySelector('label, button');
                    out.push(up && norm(up.innerText) === want ? up
                             : (inner && norm(inner.innerText) === want
                                ? inner : el));
                }
                const rank = e => /^(LABEL|BUTTON)$/.test(e.tagName) ? 0
                    : (e.matches('[data-draggable-item-id], '
                        + '[data-sortable-item], [role=button]') ? 1 : 2);
                return [...new Set(out)].sort((a, b) => rank(a) - rank(b));
            """, target_key) or []
        except WebDriverException:
            exact_cands = []
        for el in exact_cands:
            try:
                if not _cache_ok(el):
                    continue
            except (StaleElementReferenceException, WebDriverException):
                continue
            self.used_elements_cache.append(el)
            return el

        # ── PRIORITÉ 1 : label avec texte exact (QCM) ─────────────────
        for variant in variants:
            # Échappement XPath : on utilise concat() si besoin
            if "'" in variant and '"' in variant:
                continue  # cas pathologique, skip
            quote = '"' if "'" in variant else "'"
            esc = quote + variant + quote
            for xp in (
                f"//label[normalize-space(.)={esc}]",
                f"//label[.//span[normalize-space(text())={esc}]]",
                f"//label[contains(normalize-space(.), {esc})]",
            ):
                try:
                    for el in self.driver.find_elements(By.XPATH, xp):
                        if not safe_is_displayed(el):
                            continue
                        if not _cache_ok(el):
                            continue
                        txt = safe_text(el)
                        if (_norm_apos_key(txt) == target_key
                                or target_key in _norm_apos_key(txt)):
                            self.used_elements_cache.append(el)
                            return el
                except WebDriverException:
                    continue

        def _resolve_clickable_ancestor(el):
            """
            Remonte l'arbre DOM vers un ancêtre CLIQUABLE si `el` est
            un noeud texte passif (span sans handler).

            Cas typique observé sur le site :
              <button class="draggable-item" data-draggable-item-id="...">
                  <span class="text-left font-medium">texte</span>
              </button>

            `_find_element_by_text` trouve le `<span>` en priorité 2
            (match exact sur text()) mais le handler de clic est sur
            le `<button>` parent. Sans ce helper, le clic sur le span
            ne déclenche rien.

            Retourne l'ancêtre cliquable ou l'élément original si
            rien de mieux trouvé.
            """
            if el is None:
                return None
            try:
                tag = safe_tag(el)
                # Si déjà un bouton/lien/label avec handler probable : OK
                if tag in ("button", "a", "label"):
                    return el
                cls = safe_attr(el, "class").lower()
                if any(kw in cls for kw in (
                    "draggable-item", "clickable", "cursor-pointer",
                    "btn", "button-",
                )):
                    return el
                # Sinon remonter max 4 niveaux pour trouver un ancêtre
                # cliquable (draggable-item, button, role='button')
                cur = el
                for _ in range(4):
                    try:
                        parent = cur.find_element(By.XPATH, "./..")
                    except (NoSuchElementException,
                            StaleElementReferenceException,
                            WebDriverException):
                        return el
                    p_tag = safe_tag(parent)
                    p_cls = safe_attr(parent, "class").lower()
                    p_role = safe_attr(parent, "role").lower()
                    p_data = safe_attr(parent, "data-draggable-item-id")
                    is_clickable = (
                        p_tag in ("button", "a", "label")
                        or "draggable-item" in p_cls
                        or "clickable" in p_cls
                        or "cursor-pointer" in p_cls
                        or p_role == "button"
                        or bool(p_data)
                    )
                    if is_clickable:
                        return parent
                    cur = parent
            except (StaleElementReferenceException, WebDriverException):
                pass
            return el

        # ── PRIORITÉ 2 : span/div avec texte exact ───────────────────
        for variant in variants:
            if "'" in variant and '"' in variant:
                continue
            quote = '"' if "'" in variant else "'"
            esc = quote + variant + quote
            for xp in (
                f"//span[contains(@class, 'font-medium') and "
                f"normalize-space(text())={esc}]",
                f"//span[normalize-space(text())={esc}]",
                f"//div[normalize-space(text())={esc}]",
                f"//*[text()={esc}]",
            ):
                try:
                    for el in self.driver.find_elements(By.XPATH, xp):
                        if not safe_is_displayed(el):
                            continue
                        if not _cache_ok(el):
                            continue
                        txt = safe_text(el)
                        if len(txt) <= len(target) + len_tolerance:
                            # Remonte vers le bouton/lien cliquable si
                            # le span n'a pas de handler.
                            resolved = _resolve_clickable_ancestor(el)
                            if not _cache_ok(resolved):
                                # Ancêtre déjà utilisé → on garde le span
                                resolved = el
                            self.used_elements_cache.append(resolved)
                            return resolved
                except WebDriverException:
                    continue

        # ── PRIORITÉ 3 : fuzzy match sur label/span/div/p/li ─────────
        try:
            best_score, best_el = 0.0, None
            candidates = self.driver.find_elements(
                By.XPATH, "//label | //span | //div | //p | //li",
            )
            for el in candidates:
                if not safe_is_displayed(el):
                    continue
                raw = safe_text(el)
                if not raw or len(raw) > 300:
                    continue
                # Normalisation apostrophes pour la comparaison fuzzy
                raw_key = _norm_apos_key(raw)
                score = difflib.SequenceMatcher(
                    None, target_key, raw_key,
                ).ratio()
                if target_key in raw_key:
                    score += 0.15
                # Bonus forte similarité (diffère seulement par
                # ponctuation/apostrophe)
                if target_key == raw_key:
                    score = 1.0
                if safe_tag(el) == "label":
                    score += 0.1
                if score > best_score:
                    best_score, best_el = score, el
            if (best_el is not None
                    and best_score > self.ELEMENT_FUZZY_THRESHOLD
                    and _cache_ok(best_el)):
                resolved = _resolve_clickable_ancestor(best_el)
                if not _cache_ok(resolved):
                    resolved = best_el
                self.used_elements_cache.append(resolved)
                return resolved
        except WebDriverException:
            pass

        # ── PRIORITÉ 4 : contains fallback ───────────────────────────
        for variant in variants:
            if "'" in variant and '"' in variant:
                continue
            quote = '"' if "'" in variant else "'"
            esc = quote + variant + quote
            for xp in (
                f"//label[contains(., {esc})]",
                f"//span[contains(., {esc})]",
                f"//div[contains(., {esc})]",
                f"//*[contains(text(), {esc})]",
            ):
                try:
                    for el in self.driver.find_elements(By.XPATH, xp):
                        if not safe_is_displayed(el):
                            continue
                        if not _cache_ok(el):
                            continue
                        txt = safe_text(el)
                        if len(txt) < len(target) + 50:
                            resolved = _resolve_clickable_ancestor(el)
                            if not _cache_ok(resolved):
                                resolved = el
                            self.used_elements_cache.append(resolved)
                            return resolved
                except WebDriverException:
                    continue

        return None

    def _find_draggable_item_by_text(self, text: str):
        """
        Recherche STRICTE d'un draggable-item par son texte visible.

        Cette méthode existe pour éviter le piège du mode accumulateur
        (anagrammes, reorder) : `_find_element_by_text` peut matcher
        des boutons Skip/Exit/Menu qui contiennent par hasard la
        lettre ou le mot cherché dans leur sous-arbre (icône, tooltip,
        aria-label). Bug observé sur anagramme "SUDDENLY" : la lettre
        "S" a matché un `<button class='group ... overflow-hidden
        rounded-'>` qui était en fait le bouton Skip.

        On restreint donc la recherche aux éléments qui ONT la signature
        d'un draggable-item du stock :
         - classe contenant 'draggable-item'
         - `data-draggable-item-id` NON vide
         - pas dans la zone de réponse (pour pouvoir re-cliquer une
           lettre doublée dans le stock, pas celle déjà déposée)

        Retourne None si aucun vrai draggable-item ne correspond.
        """
        target = clean_ai(text).strip()
        if not target:
            return None

        def _norm(s: str, keep_case: bool = False) -> str:
            if not s:
                return ""
            s = s.strip() if keep_case else s.lower().strip()
            for apos in ("\u2019", "\u2018", "\u02BC", "`", "\u00B4"):
                s = s.replace(apos, "'")
            s = re.sub(r"\s+", " ", s)
            # Retire la ponctuation finale pour matcher 'later' avec
            # 'later,' (le stock utilise des mots-tokens avec
            # ponctuation incluse, l'IA retourne souvent sans) — SAUF
            # tuile de ponctuation seule : « , » ≠ « . » ≠ « ! » (bug :
            # le « , » cliquait le « ! »)
            if any(ch.isalnum() for ch in s):
                while s and s[-1] in ",.!?;:":
                    s = s[:-1]
            return s.strip()

        target_key = _norm(target)
        # Clé SENSIBLE à la casse : quand le stock contient à la fois
        # « Un » et « un », on doit prendre la bonne tuile. Bug observé :
        # 'Un perro y un teléfono' → le bot a pris « un » (1re tuile qui
        # matchait sans casse) en tête de phrase et « Un » au milieu.
        target_key_cs = _norm(target, keep_case=True)
        exact_case_match = None

        # Zones de réponse connues (pour filtrer les items déjà déposés).
        # NOTE : sur ce site, `exam-answer-container` désigne le STOCK,
        # PAS la zone de réponse. Ne PAS l'inclure dans le filtre
        # sinon tous les boutons du stock seraient exclus comme
        # "déjà placés".
        answer_zone_xpath = (
            "//*[@data-name='user-answer-container'] | "
            "//*[contains(@class, 'ordering-drop-zone')] | "
            "//div[contains(@class, 'drop-zone')] | "
            "//div[contains(@class, 'custom-dashed-border')]"
        )
        try:
            answer_zones = self.driver.find_elements(
                By.XPATH, answer_zone_xpath,
            )
        except WebDriverException:
            answer_zones = []

        def _is_in_answer_zone(el) -> bool:
            for az in answer_zones:
                try:
                    if self.driver.execute_script(
                        "return arguments[0].contains(arguments[1]);",
                        az, el,
                    ):
                        return True
                except (JavascriptException,
                        StaleElementReferenceException,
                        WebDriverException):
                    continue
            return False

        # On cherche explicitement les button.draggable-item avec
        # data-draggable-item-id NON vide.
        try:
            candidates = self.driver.find_elements(
                By.XPATH,
                # Système 1 : button.draggable-item[data-draggable-item-id]
                "//button[contains(@class, 'draggable-item') "
                "and @data-draggable-item-id "
                "and string-length(@data-draggable-item-id) > 0] "
                # Système 2 : div[data-sortable-item] (SortableJS des exos
                # « écoute + choisis l'orthographe », classe fill-blank-word)
                "| //div[@data-sortable-item "
                "and string-length(@data-sortable-item) > 0] "
                # Système 3 : div.item.sorting-letter[data-answer-id]
                # (dragula des anagrammes de lettres « épelle le mot »)
                "| //div[@data-answer-id "
                "and contains(concat(' ', normalize-space(@class), ' '), "
                "' sorting-letter ')]",
            )
        except WebDriverException:
            return None

        # Log diagnostic : afficher la liste de candidats trouvés + leur état
        diag_lines = []
        match = None
        for btn in candidates:
            try:
                if not safe_is_displayed(btn):
                    diag_lines.append("(invisible)")
                    continue
                # Exclusion : déjà utilisé
                already_used = False
                for c in self.used_elements_cache:
                    try:
                        if btn == c:
                            already_used = True
                            break
                    except (StaleElementReferenceException,
                            WebDriverException):
                        continue
                # Exclusion : dans la zone de réponse
                in_answer = _is_in_answer_zone(btn)
                # Texte
                txt = safe_text(btn)
                did = (safe_attr(btn, "data-draggable-item-id")
                       or safe_attr(btn, "data-sortable-item"))
                flags = []
                if already_used: flags.append("USED")
                if in_answer: flags.append("IN_ANSWER")
                flag_str = f" [{','.join(flags)}]" if flags else ""
                matches = (_norm(txt) == target_key
                           and not already_used and not in_answer)
                mark = " ← MATCH" if matches else ""
                diag_lines.append(
                    f"'{txt[:40]}' (id={did}){flag_str}{mark}",
                )
                if matches and match is None:
                    match = btn
                if (matches and exact_case_match is None
                        and _norm(txt, keep_case=True) == target_key_cs):
                    exact_case_match = btn
            except (StaleElementReferenceException,
                    WebDriverException) as e:
                diag_lines.append(f"(stale: {str(e)[:30]})")
                continue

        print(f"   🔎 Recherche stricte '{target[:40]}' dans "
              f"{len(candidates)} candidate(s) :")
        for i, line in enumerate(diag_lines):
            print(f"      {i+1}. {line}")

        # Priorité à la tuile de MÊME casse ; sinon repli sans casse.
        if exact_case_match is not None:
            match = exact_case_match
        if match is not None:
            self.used_elements_cache.append(match)
            return match
        return None

    # =================================================================
    #  RÉFLEXES (navigation automatique)
    # =================================================================
    @staticmethod
    def _is_element_active(el) -> bool:
        """
        Un élément est « actif » (donc cliquable utilement) s'il n'est pas :
         - disabled (attribut HTML)
         - aria-disabled="true"
         - de classe CSS 'disabled', 'inactive', 'pointer-events-none',
           'opacity-0', 'opacity-50', 'cursor-not-allowed'

        Évite de cliquer des boutons Next/Continue visibles mais inactifs
        avant résolution de l'exercice courant.
        """
        if safe_attr(el, "disabled"):
            return False
        ad = safe_attr(el, "aria-disabled").lower()
        if ad in ("true", "1"):
            return False
        cls = safe_attr(el, "class").lower()
        inactive_markers = (
            "disabled", "inactive", "pointer-events-none",
            "opacity-0", "opacity-50", "cursor-not-allowed",
            "is-disabled", "btn-disabled",
        )
        if any(kw in cls for kw in inactive_markers):
            return False
        return True

    # Motifs textuels de feedback (6 langues)
    _FEEDBACK_TEXT_RE = re.compile(
        r"\b(?:Correct|Incorrect|Almost|Well\s+done|Great\s+job|"
        r"Good\s+job|Nice\s+work|"
        r"Bravo|Félicitations|Presque|Bien\s+joué|Bonne\s+réponse|"
        r"Mauvaise\s+réponse|"
        r"Congratulations|"
        r"¡?Correcto|¡?Incorrecto|¡?Muy\s+bien|¡?Casi|¡?Excelente|"
        r"Richtig|Falsch|Sehr\s+gut|Gut\s+gemacht|"
        r"Corretto|Sbagliato|Molto\s+bene|Quasi|Bravo|"
        r"Você\s+acertou|Você\s+errou|Muito\s+bem|"
        r"You\s+have\s+provided\s+all\s+the\s+correct|"
        r"You\s+have\s+not\s+provided|No\s+worries|"
        r"No\s+response|Sin\s+respuesta|Pas\s+de\s+réponse|"
        r"Keine\s+Antwort|Sem\s+resposta|Nessuna\s+risposta|"
        r"Move\s+on\s+to\s+the\s+next)\b",
        re.IGNORECASE,
    )

    def _has_pending_exercise(self) -> bool:
        """
        Détecte si un exercice est EN COURS sur la page (donc qu'on
        ne doit PAS cliquer un bouton de progression).

        Un exo est "en cours" si l'un des signaux suivants est présent :
         1. Au moins un <button class="draggable-item"> cliquable
            (stock actif d'un drag/drop/reorder/anagram)
         2. Au moins une radio/checkbox visible, active, NON cochée
            ET PAS dans une zone de feedback/correction
         3. Au moins un <input type="text"> / <textarea> visible et vide
            ET PAS dans une zone de feedback/correction

        L'exclusion zone-feedback est cruciale : les pages "Almost!" /
        "Correction" peuvent contenir des inputs/radios résiduels du
        DOM qui sont visuellement masqués mais que Selenium considère
        encore "displayed". Sans ce filtre, _has_pending_exercise
        retourne True à tort sur une page de correction.

        Utilisé par :
         - P0 de _check_reflexes (éviter skip quand feedback apparait
           dans un mot du stock genre "Bravo"/"Quasi")
         - Garde-fou post-scan (éviter skip sur un QCM radio dont les
           labels matchent la regex feedback par hasard)
         - _pass_dashboard (garde-fou global)

        Retourne True si un exo attend une action de l'utilisateur.
        """
        def _in_correction_zone(el) -> bool:
            """Vérifie si l'élément est dans une zone de feedback."""
            try:
                return bool(self.driver.execute_script(
                    """
                    var el = arguments[0];
                    var p = el.parentElement;
                    var bad = ['result','feedback','correction','no-worries',
                               'almost-','your-responses','user-responses',
                               'swal','alert','toast','modal','well-done',
                               'success-message','error-message'];
                    while (p) {
                        var cls = (p.className || '').toString().toLowerCase();
                        for (var i = 0; i < bad.length; i++) {
                            if (cls.indexOf(bad[i]) >= 0) return true;
                        }
                        // data-name particulier des zones réponse fermées
                        var dn = p.getAttribute && p.getAttribute('data-name');
                        if (dn === 'correction-container'
                            || dn === 'feedback-container') return true;
                        p = p.parentElement;
                    }
                    return false;
                    """,
                    el,
                ))
            except (JavascriptException, StaleElementReferenceException,
                    WebDriverException):
                return False

        # 1) Draggable-items actifs ?
        try:
            for btn in self.driver.find_elements(
                By.XPATH,
                "//button[contains(@class, 'draggable-item') "
                "and @data-draggable-item-id "
                "and string-length(@data-draggable-item-id) > 0]",
            ):
                if safe_is_displayed(btn):
                    if _in_correction_zone(btn):
                        continue
                    return True
        except WebDriverException:
            pass

        # 2) Radios/checkboxes visibles et non cochés ?
        # PRÉ-CHECK : sur une page de correction QCM, le radio cliqué
        # par le user reste coché mais TOUS les radios sont désactivés
        # (disabled / readonly / pointer-events:none). Si on détecte
        # cette signature, on ignore TOUS les radios.
        try:
            all_radios = self.driver.find_elements(
                By.CSS_SELECTOR,
                "input[type='radio'], input[type='checkbox']",
            )
            # Un radio/checkbox compte s'il est visible LUI-MÊME, OU si son
            # LABEL est visible (cas fréquent : input class="sr-only" masqué
            # visuellement mais cliquable via son <label>). Sans ça, un QCM
            # à inputs sr-only n'était pas vu comme "exo en cours" et la
            # page finissait prise pour une page de correction (boucle).
            visible_radios = []
            for r in all_radios:
                if safe_is_displayed(r):
                    visible_radios.append(r)
                    continue
                rid = safe_attr(r, "id")
                if rid:
                    try:
                        lbl = self.driver.find_element(
                            By.CSS_SELECTOR, f"label[for='{rid}']",
                        )
                        if safe_is_displayed(lbl):
                            visible_radios.append(r)
                    except (NoSuchElementException, WebDriverException):
                        pass
            if visible_radios:
                # Un radio coché existe-t-il ?
                has_checked = False
                for r in visible_radios:
                    try:
                        if r.is_selected():
                            has_checked = True
                            break
                    except (StaleElementReferenceException, WebDriverException):
                        continue
                # Tous les radios sont-ils désactivés ?
                all_disabled = True
                for r in visible_radios:
                    if self._is_enabled_safe(r):
                        # Vérifier aussi si le pointer-events est bloqué
                        # via CSS sur le radio ou son label
                        try:
                            blocked = self.driver.execute_script(
                                "var el = arguments[0]; "
                                "var s = window.getComputedStyle(el); "
                                "if (s.pointerEvents === 'none') return true; "
                                "var lbl = document.querySelector("
                                "  'label[for=\"' + (el.id || '') + '\"]'); "
                                "if (lbl) { "
                                "  var ls = window.getComputedStyle(lbl); "
                                "  if (ls.pointerEvents === 'none') return true; "
                                "} "
                                "return false;",
                                r,
                            )
                            if not blocked:
                                all_disabled = False
                                break
                        except (JavascriptException, WebDriverException):
                            all_disabled = False
                            break
                # QCM déjà répondu = au moins un coché + tous figés
                if has_checked and all_disabled:
                    # Page de correction QCM : on ignore les radios
                    pass
                else:
                    # QCM potentiellement actif → vérifier individuellement
                    for inp in visible_radios:
                        try:
                            if inp.is_selected():
                                continue
                        except (StaleElementReferenceException,
                                WebDriverException):
                            continue
                        if not self._is_enabled_safe(inp):
                            continue
                        # Filtre : pas dans une zone de feedback/correction
                        if _in_correction_zone(inp):
                            continue
                        # Le label associé est-il visible ?
                        inp_id = safe_attr(inp, "id")
                        if inp_id:
                            try:
                                lbl = self.driver.find_element(
                                    By.CSS_SELECTOR,
                                    f"label[for='{inp_id}']",
                                )
                                if safe_is_displayed(lbl):
                                    if _in_correction_zone(lbl):
                                        continue
                                    return True
                            except (NoSuchElementException, WebDriverException):
                                pass
                        # Sinon input lui-même visible ?
                        return True
        except WebDriverException:
            pass

        # 3) Text input / textarea visibles et vides ?
        try:
            for el in self.driver.find_elements(
                By.XPATH,
                "//input[@type='text'] | //textarea | "
                "//input[not(@type)]",
            ):
                if not safe_is_displayed(el):
                    continue
                if not self._is_enabled_safe(el):
                    continue
                if _in_correction_zone(el):
                    continue
                try:
                    val = el.get_attribute("value") or ""
                except (StaleElementReferenceException, WebDriverException):
                    continue
                if not val.strip():
                    return True
        except WebDriverException:
            pass

        return False

    # Feedback NÉGATIF uniquement (6 langues). Volontairement limité à
    # des formules non ambiguës pour éviter de prendre un mot d'exercice
    # (ex. « casi » dans une phrase espagnole) pour un verdict.
    _WRONG_FEEDBACK_RE = re.compile(
        r"\b(?:Incorrect|Wrong\s+answer|Not\s+quite|Almost|"
        r"You\s+have\s+not\s+provided|"
        r"Mauvaise\s+r[ée]ponse|"
        r"Incorrecto|Respuesta\s+incorrecta|"
        r"Falsch|Sbagliato|Você\s+errou)\b",
        re.IGNORECASE,
    )

    # Verdicts reconnus EN DÉBUT d'un texte court (sensible à la casse :
    # une tuile « almost » en minuscule ne compte pas).
    _JS_READ_VERDICT = r"""
        // arguments[0] = nb d'éléments rouges AVANT validation (référence)
        // arguments[1] = true → renvoie seulement le nombre d'éléments rouges
        const baseline = arguments[0] || 0;
        const countOnly = arguments[1] === true;
        const BAD = /^(Almost|Not quite|Incorrect|Wrong|Oops|No worries|No response|You have not provided|You did not|Mauvaise réponse|Presque|Dommage|Incorrecto|Respuesta incorrecta|Falsch|Sbagliato|Errado)\b/;
        const GOOD = /^(Correct|Well done|Great|Perfect|Excellent|Bravo|Good job|Nice|Congratulations|You have provided all|Bonne réponse|Félicitations|Correcto|Richtig|Corretto)\b/;
        const SKIP = 'button.draggable-item,[data-draggable-item-id],' +
            '[data-sortable-item],label,input,textarea,select,option,' +
            '[data-name="user-answer-container"],.ordering-drop-zone,' +
            '[contenteditable="true"],script,style,noscript';
        const walker = document.createTreeWalker(document.body,
                                                 NodeFilter.SHOW_TEXT);
        let good = null, bad = null;
        const samples = [];
        while (!countOnly && walker.nextNode()) {
            const t = (walker.currentNode.nodeValue || '').trim();
            if (!t || t.length > 90) continue;
            const el = walker.currentNode.parentElement;
            if (!el || el.closest(SKIP)) continue;
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) continue;
            const cs = getComputedStyle(el);
            if (cs.visibility === 'hidden' || cs.display === 'none'
                || parseFloat(cs.opacity) === 0) continue;
            const clean = t.replace(/^[\s¡!¿?«"'\u{1F300}-\u{1FAFF}☀-➿]+/u, '');
            if (!bad && BAD.test(clean)) bad = t;
            else if (!good && GOOD.test(clean)) good = t;
            const cls = (el.className && el.className.toString) ?
                        el.className.toString() : '';
            if (samples.length < 6 && t.length > 2 && /!|feedback|result|correct|answer|success|error|danger|warning|green|red/i.test(t + ' ' + cls))
                samples.push(t);
        }
        if (bad) return ['bad', bad, samples];
        // ── LETTRES / CASES : réponse ≠ correction ─────────────────────
        // Ex. anagramme « écoute + épelle » : la réponse donnée (« ASA_ »,
        // lettres grises) et, en dessous, la correction (« CASA », en
        // vert). Deux rangées de cases dont le contenu diffère = faux.
        if (!countOnly) {
            const cands = [];
            for (const el of document.querySelectorAll('div, span, button, li')) {
                const r = el.getBoundingClientRect();
                if (r.width < 24 || r.height < 24 || r.width > 110 || r.height > 110) continue;
                if (Math.abs(r.width - r.height) > r.width * 0.35) continue;
                const cs = getComputedStyle(el);
                if (cs.visibility === 'hidden' || cs.display === 'none') continue;
                const dashed = /dashed|dotted/.test(cs.borderTopStyle);
                const cls = (el.className && el.className.toString) ? el.className.toString() : '';
                if (!dashed && !/letter|sorting|slot/i.test(cls)) continue;
                const t = (el.innerText || '').trim();
                if (t.length > 2) continue;
                cands.push({el: el, x: r.left, y: r.top, t: t});
            }
            const boxes = cands.filter(b => !cands.some(o => o !== b && b.el.contains(o.el)));
            const rows = [];
            for (const b of boxes) {
                let row = rows.find(rw => Math.abs(rw.y - b.y) < 12);
                if (!row) { row = {y: b.y, items: []}; rows.push(row); }
                row.items.push(b);
            }
            const words = rows.filter(rw => rw.items.length >= 3)
                .map(rw => rw.items.sort((a, c) => a.x - c.x)
                         .map(b => b.t || '_').join('').toUpperCase())
                .filter(w => /[^_]/.test(w));
            for (let i = 0; i < words.length; i++)
                for (let j = i + 1; j < words.length; j++)
                    if (words[i] !== words[j])
                        return ['bad', 'lettres « ' + words[i] + ' » ≠ « ' + words[j] + ' »', samples];
        }
        // ── COULEUR ROUGE / ROSE (réponse fausse) ── vérifiée AVANT les mots
        // positifs : certains écrans n'affichent QUE des couleurs (cases à
        // cocher, tuiles), et un texte d'exo commençant par « Great… »
        // ne doit pas masquer une mauvaise réponse.
        // GlobalExam colore les mauvaises réponses : case rouge vif
        // (~rgb(192,38,45)) et fond rose (~rgb(255,221,224)) ; les bonnes
        // sont vertes. On cherche un bloc visible de taille raisonnable
        // avec un fond rouge vif ou rose.
        const parse = (c) => {
            const m = (c || '').match(/rgba?\(([^)]+)\)/);
            if (!m) return null;
            const v = m[1].split(',').map(x => parseFloat(x));
            return {r: v[0], g: v[1], b: v[2], a: v.length > 3 ? v[3] : 1};
        };
        const isRed = (c) => c && c.a > 0.3 && (
            (c.r >= 170 && c.r - Math.max(c.g, c.b) >= 90) ||           // rouge vif
            (c.r >= 245 && c.g >= 190 && c.r - c.g >= 22 && c.r - c.b >= 18 &&
             Math.abs(c.g - c.b) <= 20)                                  // rose
        );
        const els = document.querySelectorAll(
            'div, span, li, label, p, button, input, svg, rect, path');
        const n = Math.min(els.length, 5000);
        let redCount = 0, redFirst = null;
        for (let i = 0; i < n; i++) {
            const el = els[i];
            const r = el.getBoundingClientRect();
            if (r.width < 12 || r.height < 12 || r.width > window.innerWidth * 0.98) continue;
            if (r.bottom < 0 || r.top > window.innerHeight * 1.5) continue;
            if (el.closest('header, nav')) continue;
            const cs = getComputedStyle(el);
            if (cs.visibility === 'hidden' || cs.display === 'none') continue;
            const isSvg = el instanceof SVGElement;
            // Fond (blocs, lignes de réponse) ; bordure (tuiles cerclées) ;
            // remplissage (cases dessinées en SVG) ; accent-color (cases natives)
            const cands = isSvg ? [cs.fill, cs.stroke]
                : [cs.backgroundColor, cs.borderTopColor, cs.accentColor];
            for (const raw of cands) {
                const c = parse(raw);
                if (!isRed(c)) continue;
                // une bordure seule doit être réellement visible
                if (raw === cs.borderTopColor && parseFloat(cs.borderTopWidth) < 1) continue;
                redCount++;
                if (!redFirst) {
                    const host = isSvg ? (el.closest('label, li, div') || el) : el;
                    redFirst = (host.innerText || '').trim().split('\n')[0].slice(0, 60);
                }
                break;
            }
        }
        if (countOnly) return redCount;
        // Rouge APPARU après la validation → réponse fausse
        if (redCount > baseline)
            return ['bad', 'couleur rouge' + (redFirst ? ' sur « ' + redFirst + ' »' : ''), samples];
        if (good) return ['good', good, samples];
        return ['none', '', samples];
    """

    def _count_red(self) -> int:
        """Nombre d'éléments rouges/roses visibles (référence AVANT
        validation, pour ne détecter que le rouge qui APPARAÎT)."""
        try:
            n = self.driver.execute_script(self._JS_READ_VERDICT, 0, True)
            return int(n or 0)
        except (WebDriverException, TypeError, ValueError):
            return 0

    def _read_verdict(self):
        """('bad'|'good'|'none', texte du verdict, échantillons)."""
        try:
            res = self.driver.execute_script(
                self._JS_READ_VERDICT,
                getattr(self, "_red_baseline", 0), False)
            if isinstance(res, (list, tuple)) and len(res) >= 2:
                return (res[0], res[1] or "",
                        list(res[2]) if len(res) > 2 and res[2] else [])
        except WebDriverException:
            pass
        return ("none", "", [])

    _JS_GREEN_TEXTS = r"""
        // Textes courts affichés dans un encadré VERT (la correction).
        const isGreen = (c) => {
            const m = (c || '').match(/rgba?\(([^)]+)\)/);
            if (!m) return false;
            const v = m[1].split(',').map(x => parseFloat(x));
            if (v.length > 3 && v[3] < 0.3) return false;
            return v[1] >= 140 && v[1] - v[0] >= 40 && v[1] - v[2] >= 40;
        };
        const out = [];
        for (const el of document.querySelectorAll('div, p, span, li, label')) {
            const r = el.getBoundingClientRect();
            if (!r.width || !r.height) continue;
            const cs = getComputedStyle(el);
            if (parseFloat(cs.borderTopWidth) < 1 || !isGreen(cs.borderTopColor)) continue;
            const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
            if (t && t.length <= 80 && !(el.innerText || '').trim().includes('\n'))
                out.push(t);
        }
        return out;
    """

    @staticmethod
    def _norm_answer(s: str) -> str:
        s = re.sub(r"\s+", " ", (s or "").strip().lower())
        return s.strip(" .,!?;:¡¿\"'")

    def _typed_vs_correction(self) -> str | None:
        """Exercice à champ texte : si l'encadré vert de correction montre
        autre chose que ce que le bot a tapé → réponse fausse."""
        typed = getattr(self, "_last_typed", None) or []
        if not typed:
            return None
        mine = self._norm_answer(" ".join(typed))
        try:
            greens = self.driver.execute_script(self._JS_GREEN_TEXTS) or []
        except WebDriverException:
            return None
        greens = [g for g in greens if self._norm_answer(g)]
        if not greens:
            return None
        if any(self._norm_answer(g) == mine for g in greens):
            return None
        return (f"réponse tapée « {' '.join(typed)} » ≠ correction "
                f"« {greens[0][:50]} »")

    @staticmethod
    def _scrambled_from_page(page_text: str) -> str | None:
        """Mots mélangés d'un exo « Listen and rewrite the word in the
        correct order » (ex. « roper settri »). None si autre exercice."""
        lines = [l.strip() for l in (page_text or "").splitlines()
                 if l.strip()]
        for i, l in enumerate(lines):
            low = l.lower()
            if "letter" in low:      # « put the letters… » = exo à tuiles
                continue
            if re.search(r"rewrite the word|word.*correct order|"
                         r"reescrib|r[ée][ée]cri|remets? .*ordre", low):
                # Les mots mélangés peuvent être sur UNE ligne
                # (« un raínjd nerdag ») ou sur PLUSIEURS (« un » /
                # « raínjd » / « nerdag ») → on les rassemble TOUS.
                stop = {"validate", "valider", "check", "next", "skip",
                        "submit", "pronounce", "continue", "continuer",
                        "suivant", "vérifier", "verifier", "comprobar"}
                words, started = [], False
                for nxt in lines[i + 1:i + 9]:
                    ok = (len(nxt) <= 40 and nxt.lower() not in stop and
                          re.fullmatch(r"[^\W\d_]+(?: [^\W\d_]+){0,5}", nxt))
                    if ok:
                        words.extend(nxt.split())
                        started = True
                    elif started:
                        break
                    if len(words) > 6:
                        break
                if words:
                    return " ".join(words[:6])
        return None

    def _arrange_like(self, scrambled: str, ai_words: list) -> str | None:
        """Mot absent du dictionnaire (souvent une coquille du site, ex.
        « oiilipm » → « limipio ») : on cherche l'arrangement qui utilise
        EXACTEMENT toutes les lettres et qui ressemble le plus au mot
        proposé par l'IA. Priorité à un arrangement déjà VU sur une page
        de la session (orthographe exacte du site)."""
        import difflib
        import itertools
        letters = scrambled.lower()
        if not 2 <= len(letters) <= 9:
            return None
        # Mot de l'IA le plus proche (en lettres) du mot mélangé
        target = max(ai_words or [""], key=lambda a: difflib.SequenceMatcher(
            None, sorted(a.lower()), sorted(letters)).ratio()).lower()
        seen = getattr(self, "_seen_words", {})
        best, best_score = None, -1.0
        for perm in set(itertools.permutations(letters)):
            cand = "".join(perm)
            score = difflib.SequenceMatcher(None, cand, target).ratio()
            pre = len(os.path.commonprefix([cand, target]))
            score += 0.01 * pre             # même début que l'IA
            if cand in seen:
                score += 1.0                 # vu sur le site → priorité
            if score > best_score:
                best, best_score = cand, score
        if best:
            src = "vu sur une page" if best in seen else "proche de l'IA"
            print(f"   🔤 « {scrambled} » → « {best} » (toutes les lettres, "
                  f"{src})")
        return best

    def _activity_vocab(self) -> list[str]:
        """Vocabulaire de l'activité en cours : le code de la page contient
        la liste des mots/expressions de l'activité (« Un lago », « El
        museo »…). Source la plus sûre pour remettre des lettres dans
        l'ordre (orthographe exacte du site, coquilles comprises)."""
        try:
            src = self.driver.page_source
        except WebDriverException:
            return []
        import html as _html
        out = []
        for raw in re.findall(r'"name":"((?:[^"\\]|\\.)*)"',
                              _html.unescape(src)):
            try:
                v = json.loads('"' + raw + '"').strip()
            except ValueError:
                continue
            if (2 <= len(v) <= 60 and re.fullmatch(
                    r"[^\W\d_]+(?: [^\W\d_]+){0,6}", v)
                    and v not in out):
                out.append(v)
        return out

    def _arb_model(self) -> str:
        """Modèle de l'arbitre : Flash-Lite pour les cas simples, mais
        Flash dès qu'il faut COMPRENDRE un dialogue / transcript mémorisé
        (Lite s'y trompait souvent) ou si la passe 1 venait déjà de Lite."""
        if getattr(self, "_p1_lite", False) or self.lesson_texts:
            return MODEL_NAME
        return MODEL_ARBITRAGE

    def _id_order(self, plan: list, stock: list | None = None) -> list | None:
        """Les tuiles à remettre dans l'ordre portent un identifiant
        (data-draggable-item-id) attribué DANS L'ORDRE de la bonne réponse
        (vérifié sur ¿Agua? Sí, por favor. / DUCHARSE / dialogues…). Si le
        plan utilise exactement ces tuiles et que leurs identifiants se
        suivent, on renvoie les textes triés par identifiant."""
        try:
            items = self.driver.execute_script("""
                const out = [];
                document.querySelectorAll('[data-draggable-item-id]').forEach(e => {
                    const r = e.getBoundingClientRect();
                    if (!r.width || !r.height) return;
                    const id = parseInt(e.getAttribute('data-draggable-item-id'), 10);
                    if (isNaN(id)) return;
                    out.push([id, (e.textContent || '').replace(/\\s+/g, ' ').trim()]);
                });
                return out;
            """) or []
        except WebDriverException:
            return None
        if len(items) < 2:
            return None
        norm = lambda x: re.sub(r"\s+", " ", str(x)).strip()
        from collections import Counter
        # Les tuiles de l'exercice = celles dont les textes correspondent au
        # stock (ou au plan). On ignore les autres boutons de la page.
        ref = Counter(norm(a) for a in (stock or plan))
        pool = Counter(ref)
        mine = []
        for i, t in sorted(items):
            if pool[norm(t)] > 0:
                pool[norm(t)] -= 1
                mine.append((i, t))
        if len(mine) < 2 or len(mine) != sum(ref.values()):
            return None
        ids = [i for i, _ in mine]
        if ids == list(range(ids[0], ids[0] + len(ids))):
            return [t for _, t in mine]
        # Numéros PRESQUE à la suite : petits trous (≤ 3) et au plus UNE
        # tuile « hors série » (numéro très éloigné, ex. tuile recréée par
        # le site). On garde l'ordre des numéros pour le groupe principal
        # et on place la tuile isolée après le mot qui la précède dans la
        # réponse de l'IA (vérifié : « Jean asked how long it took … »).
        groups, cur = [], [mine[0]]
        for a, b in zip(mine, mine[1:]):
            if b[0] - a[0] <= 4:
                cur.append(b)
            else:
                groups.append(cur)
                cur = [b]
        groups.append(cur)
        main = max(groups, key=len)
        outliers = [x for g in groups if g is not main for x in g]
        if len(outliers) != 1 or len(main) < 3:
            return None
        order = [t for _, t in main]
        lone = outliers[0][1]
        ai = [norm(a) for a in plan]
        pos = None
        if norm(lone) in ai:
            k = ai.index(norm(lone))
            prev = ai[k - 1] if k > 0 else None
            if prev is None:
                pos = 0
            elif prev in [norm(o) for o in order]:
                pos = [norm(o) for o in order].index(prev) + 1
        if pos is None:
            return None
        return order[:pos] + [lone] + order[pos:]

    @staticmethod
    def _punct_violations(order: list, tiles: list | None = None) -> list:
        """Règles de ponctuation / casse d'une phrase remise en ordre.
        Renvoie la liste des fautes (vide = OK) :
          - « ¿ » / « ¡ » ouvrant doit être refermé par « ? » / « ! »
            plus loin (et pas l'inverse) ;
          - tuile de ponctuation seule (« . », « ? », « ! ») = en dernier ;
          - un mot avec majuscule (hors 1er) doit suivre une fin de
            phrase (. ? !) — seulement si la phrase a des majuscules ;
          - chaque tuile utilisée autant de fois qu'elle existe."""
        words = [str(w) for w in order if str(w).strip()]
        errs = []
        if tiles is not None:
            from collections import Counter
            if Counter(words) != Counter(str(t) for t in tiles):
                errs.append("tuiles différentes du stock")
        for opn, cls in (("¿", "?"), ("¡", "!")):
            depth = 0
            for w in words:
                if w.startswith(opn):
                    if depth:
                        errs.append(f"« {opn} » ouvert deux fois")
                    depth = 1
                if w.endswith(cls) and any(x.startswith(opn) for x in words):
                    if not depth:
                        errs.append(f"« {cls} » avant son « {opn} »")
                    depth = 0
            if depth:
                errs.append(f"« {opn} » jamais refermé")
        for w in words[:-1]:
            if w in (".", "?", "!", "…", "..."):
                errs.append(f"« {w} » pas en fin de phrase")
        for i, w in enumerate(words):
            if i and w[:1].isupper() and len(w) > 1:
                prev = words[i - 1]
                if not (prev[-1:] in ".?!…" or prev in (".", "?", "!")):
                    # un nom propre peut avoir une majuscule : faute
                    # seulement si le 1er mot n'en a pas
                    if not words[0].lstrip("¿¡")[:1].isupper():
                        errs.append(f"« {w} » (majuscule) en milieu de "
                                    f"phrase")
        return errs

    def _reorder_from_seen(self, tiles: list, actions: list) -> list:
        """Si une phrase vue sur le site (vocabulaire de l'activité, options
        d'un exercice précédent…) s'écrit EXACTEMENT avec ces tuiles,
        renvoie les tuiles dans cet ordre. Sinon, la réponse de l'IA."""
        tiles = [str(t) for t in tiles if str(t).strip()]
        if len(tiles) < 2 or not any(len(t) > 1 for t in tiles):
            return actions
        norm = lambda x: _strip_accents_keep_n(
            re.sub(r"\s+", "", x)).lower()
        sources = list(self._activity_vocab()) + list(
            getattr(self, "_seen_lines", {}).keys())

        def solve(target: str, pool: list) -> list | None:
            if not target:
                return [] if not pool else None
            for i, t in enumerate(pool):
                if i and t == pool[i - 1]:
                    continue                    # tuiles identiques
                nt = norm(t)
                if nt and target.startswith(nt):
                    rest = solve(target[len(nt):], pool[:i] + pool[i + 1:])
                    if rest is not None:
                        return [t] + rest
            return None

        pool = sorted(tiles)
        # Tuiles de ponctuation en double (doublon possible du scan) :
        # on accepte aussi la version sans le doublon.
        pools = [pool]
        dedup = []
        for t in pool:
            if not (t in dedup and not any(ch.isalnum() for ch in t)):
                dedup.append(t)
        if dedup != pool:
            pools.append(dedup)
        total = sum(len(norm(t)) for t in pool)
        for line in sources:
            base = norm(line)
            for target in (base, base + ".", base + "?", base + "!"):
                for pl in pools:
                    if len(target) != sum(len(norm(t)) for t in pl):
                        continue
                    order = solve(target, pl)
                    if order:
                        if [norm(a) for a in actions] != [norm(o)
                                                          for o in order]:
                            print(f"   📚 Phrase déjà vue sur le site : "
                                  f"« {line} » → ordre {order} (l'IA "
                                  f"proposait {actions})")
                        return order
        _ = total
        return actions

    def _fix_unscramble(self, page_text: str, actions: list) -> list:
        """Vérifie que la réponse utilise EXACTEMENT les lettres mélangées,
        mot par mot et dans l'ordre (« aun acma » → « una cama »). Sinon,
        complète/corrige chaque mot avec le dictionnaire."""
        scr = self._scrambled_from_page(page_text)
        if not scr or not actions:
            return actions
        key = lambda w: sorted(_strip_accents_keep_n(w).replace(" ", ""))
        answer = " ".join(str(a) for a in actions).strip()
        sw, aw = scr.split(), answer.split()
        # 1) Vocabulaire de l'activité (code de la page) : une expression
        #    qui a EXACTEMENT les mêmes lettres, mot par mot → c'est elle.
        vocab = self._activity_vocab()
        for v in vocab:
            vw = v.split()
            if (len(vw) == len(sw)
                    and all(key(a) == key(b) for a, b in zip(vw, sw))):
                if self._norm_answer(v) != self._norm_answer(answer):
                    print(f"   📚 « {scr} » → « {v} » (vocabulaire de "
                          f"l'activité ; l'IA proposait « {answer} »)")
                else:
                    print(f"   📚 Réponse confirmée par le vocabulaire de "
                          f"l'activité : « {v} »")
                return [v]
        # Mots isolés du vocabulaire : priorité dans les choix ci-dessous
        seen = self.__dict__.setdefault("_seen_words", {})
        for v in vocab:
            for w in v.split():
                seen[w.lower()] = seen.get(w.lower(), 0) + 50
        # Mot recopié TEL QUEL depuis les lettres mélangées (ex. « le »
        # au lieu de « el ») : l'IA n'a pas remis ce mot dans l'ordre.
        same = lambda a, b: len(b) > 1 and a.lower() == b.lower()
        copied = any(same(a, b) for a, b in zip(aw, sw))
        if (len(aw) == len(sw) and not copied
                and all(key(a) == key(b) for a, b in zip(aw, sw))):
            return actions
        # Garde-fou : la correction ne doit JAMAIS laisser des lettres de
        # côté. Si la réponse de l'IA contient toutes les lettres lues
        # sur la page + d'autres, c'est la lecture de la page qui est
        # incomplète → on garde la réponse de l'IA.
        from collections import Counter
        c_scr, c_ans = Counter(key(scr)), Counter(key(answer))
        if len(aw) > len(sw) and not (c_scr - c_ans):
            print(f"   ℹ️ Lettres lues « {scr} » incluses dans la réponse "
                  f"« {answer} » → réponse de l'IA conservée (toutes les "
                  f"lettres utilisées)")
            return actions
        print(f"   ⚠️ Réponse « {answer} » ≠ lettres mélangées « {scr} » "
              f"({len(sw)} mot(s)) → correction mot par mot")
        try:
            words = _DictCache.get_wordlist(self.langue, top_n=300_000)
        except Exception:  # noqa: BLE001
            words = []
        pool = list(aw)
        fixed = []
        for w in sw:
            mine = next((a for a in pool if key(a) == key(w)
                         and not same(a, w)), None)
            if mine is not None:          # l'IA avait trouvé ce mot
                pool.remove(mine)
                fixed.append(mine.lower())
                continue
            cands = (anagram_phrase_candidates(
                list(_strip_accents_keep_n(w)), words,
                pattern=[len(w)], limit=6) if words else [])
            # Jamais le mot mélangé lui-même ; priorité aux mots déjà vus
            # sur le site (vocabulaire de l'activité, pages précédentes)
            cands = [c for c in cands if not same(c, w)] or cands
            cands.sort(key=lambda c: -seen.get(c.lower(), 0))
            if not cands:
                alt = self._arrange_like(w, aw)
                if not alt:
                    print(f"   ⚠️ Aucun mot du dictionnaire pour « {w} » "
                          f"→ réponse de l'IA conservée")
                    return actions
                fixed.append(alt.lower())
                continue
            fixed.append(cands[0].lower())
        result = " ".join(fixed)
        if answer[:1].isupper():
            result = result[:1].upper() + result[1:]
        print(f"   🔧 Réponse corrigée : « {result} »")
        return [result]

    def _detect_wrong_answer(self) -> str | None:
        """Lit le verdict affiché après validation. Renvoie le texte du
        feedback si la réponse est FAUSSE, sinon None. Écrit toujours une
        ligne « Verdict » dans le journal (pour vérifier la détection)."""
        kind, text, samples = self._read_verdict()
        if kind == "none":
            time.sleep(1.2)  # le feedback peut arriver un peu après
            kind, text, samples = self._read_verdict()
        if kind == "bad":
            print(f"   🧾 Verdict : ❌ faux (« {text[:60]} »)")
            return text.replace("\n", " ")[:200]
        typed_err = self._typed_vs_correction()
        if typed_err:
            print(f"   🧾 Verdict : ❌ faux ({typed_err})")
            return typed_err
        if kind == "good":
            print(f"   🧾 Verdict : ✅ correct (« {text[:40]} »)")
            return None
        shown = " | ".join(x[:40] for x in samples[:4]) or "aucun texte"
        print(f"   🧾 Verdict : ❔ non reconnu — textes vus : {shown}")
        return None

    def _report_wrong_answer(self, feedback: str, res: dict,
                             actions: list, code_options: list,
                             report: dict) -> None:
        """Enregistre capture + contexte dans erreurs/<date>/ puis met le
        bot en pause jusqu'à ce que l'utilisateur clique « Reprendre »."""
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        folder = os.path.join(_errors_dir(), stamp)
        try:
            os.makedirs(folder, exist_ok=True)
            try:
                self.driver.save_screenshot(
                    os.path.join(folder, "capture.png"))
            except WebDriverException:
                pass
            try:
                with open(os.path.join(folder, "page.html"), "w",
                          encoding="utf-8") as f:
                    f.write(self.driver.page_source)
            except (WebDriverException, OSError):
                pass
            try:
                url = self.driver.current_url
            except WebDriverException:
                url = "?"
            try:
                page_txt = self._get_page_text()[:4000]
            except Exception:
                page_txt = ""
            lines = [
                f"Date        : {stamp}",
                f"URL         : {url}",
                f"Langue      : {self.langue}",
                f"Feedback    : {feedback}",
                f"Type exo    : {res.get('exercise_type') or res.get('type')}",
                f"Stock       : {code_options}",
                f"Réponse IA  : {res.get('actions')}",
                f"Plan exécuté: {actions}",
                f"Rapport     : {report}",
                "",
                "=== TEXTE DE LA PAGE ===",
                page_txt,
            ]
            with open(os.path.join(folder, "contexte.txt"), "w",
                      encoding="utf-8") as f:
                f.write("\n".join(lines))
            if isinstance(sys.stdout, _LogTee):
                with open(os.path.join(folder, "journal.txt"), "w",
                          encoding="utf-8") as f:
                    f.write(sys.stdout.recent())
        except OSError as e:
            print(f"   ⚠️ Rapport d'erreur non enregistré : {e}")
            return

        self.stats["errors"] = self.stats.get("errors", 0) + 1
        print(f"   ❌ Réponse FAUSSE détectée : « {feedback[:80]} »")
        print(f"   📸 Rapport enregistré : erreurs/{stamp}")
        if not DEBUG_PAUSE:
            print("   ↪ Mode debug désactivé → pas de pause, le bot continue")
            return
        print("   ⏸️  Bot en PAUSE. Dis à Claude « regarde l'erreur "
              f"{stamp} », puis clique « Reprendre ».")
        notify_phone(
            f"Réponse fausse : « {feedback[:80]} ». Bot en pause. "
            f"Demande à Claude : « regarde l'erreur {stamp} », "
            f"puis clique Reprendre.",
            title="Bot GlobalExam - en pause",
        )
        RUN_GATE.clear()
        if ON_AUTO_PAUSE is not None:
            try:
                ON_AUTO_PAUSE(folder)
            except Exception:
                pass
            RUN_GATE.wait()
        else:
            # Lancement en terminal (sans launcher) : reprise par Entrée
            try:
                input("   ↩️  Appuie sur Entrée pour reprendre… ")
            except (EOFError, OSError):
                pass
            RUN_GATE.set()
        print("   ▶️  Reprise après erreur")

    def _has_feedback_context(self) -> bool:
        """
        Détecte si la page montre un feedback de correction/résultat.
        C'est l'indicateur que l'exercice a été soumis et qu'un bouton
        « Next » est légitimement cliquable.

        Quatre voies indépendantes (OR) :
         1. Classes CSS caractéristiques (no-worries, feedback, result…)
         2. Popup SweetAlert2 affiché
         3. Texte de feedback multilingue visible sur la page (body.text)
         4. Texte dans les headings h1/h2/h3 (parfois absents de
            body.text si le rendu CSS les a masqués à l'enumeration)
        """
        # 1) Classes CSS typiques
        for sel in (
            "[class*='no-worries']", "[class*='correction']",
            "[class*='feedback-']", "[class*='-feedback']",
            "[class*='result-']", "[class*='-result']",
            "[class*='well-done']", "[class*='success-message']",
            "[class*='error-message']", "[class*='almost-']",
            ".swal2-popup",
        ):
            try:
                for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                    if safe_is_displayed(el):
                        return True
            except WebDriverException:
                continue

        # 2) Texte de feedback dans le body
        try:
            body = self.driver.find_element(By.TAG_NAME, "body")
            body_text = safe_text(body)
            if body_text and self._FEEDBACK_TEXT_RE.search(body_text):
                return True
        except (NoSuchElementException, WebDriverException):
            pass

        # 3) Texte de feedback dans les headings (h1/h2/h3). Parfois
        # un titre "Well done!" / "Nice work!" est rendu dans un
        # heading avec CSS spécifique qui peut échapper à body.text
        # selon le timing ou la structure du DOM.
        try:
            for el in self.driver.find_elements(
                By.XPATH, "//h1 | //h2 | //h3",
            ):
                if not safe_is_displayed(el):
                    continue
                t = safe_text(el)
                if t and self._FEEDBACK_TEXT_RE.search(t):
                    return True
        except WebDriverException:
            pass

        return False

    def _is_progression_i18n_key(self, txt: str) -> bool:
        """
        Détecte une CLÉ DE TRADUCTION i18n non résolue affichée à la place
        du libellé (ex: "user-activity/result/next_component.certification"
        au lieu de "Next"). Signature : pas d'espace, allure de clé
        (slash / underscore / point) ET contient un mot de progression.
        """
        t = (txt or "").strip().lower()
        if not t or " " in t:
            return False
        looks_like_key = (
            "/" in t or "_component" in t
            or ("." in t and ("_" in t or "/" in t))
        )
        if not looks_like_key:
            return False
        return any(k in t for k in (
            "next", "finish", "continue", "move_on", "move-on",
            "suivant", "terminer", "next_component", "finish_component",
        ))

    def _click_progression_button(self) -> bool:
        """Clique Next/Finish/Continue… ; compte l'exercice comme terminé."""
        ok = self._click_progression_button_impl()
        if ok:
            if getattr(self, "_exo_in_progress", False):
                self.stats["exos"] = self.stats.get("exos", 0) + 1
            self._exo_in_progress = False
        return ok

    def _click_progression_button_impl(self) -> bool:
        """
        Cherche et clique le premier bouton de progression disponible
        (Next, Finish, Move on, Continue, Suivant, etc.).

        Utilisé par :
         - P0 de _check_reflexes (page de correction détectée)
         - Post-validation dans _execute_actions
         - Garde-fou post-scan (stock fantôme sur page de correction)

        Filtre les boutons parasites (Zoom, Exit, Close, Help, etc.)
        UNIQUEMENT quand le texte du bouton est clairement mixte
        (ex: "Zoom Next" où "Zoom" et "Next" sont tous deux présents).
        Pour un bouton dont le texte normalisé == le label cherché
        (match exact), pas de filtre parasites.

        Retourne True si un bouton a été cliqué, False sinon.
        """
        # ── CAS SPÉCIAL : clé i18n non traduite (ex:
        # "user-activity/result/next_component.certification") affichée à la
        # place de "Next". On la traite comme un bouton de progression.
        # Le test "pas d'espace" du helper garantit qu'on ne matche que
        # l'élément serré (le bouton/span), pas un gros conteneur.
        try:
            for xp in (
                "//button | //a[@role='button'] | //a | //div[@role='button']",
                "//*[contains(@class,'cursor-pointer')]",
            ):
                for el in self.driver.find_elements(By.XPATH, xp):
                    if (not safe_is_displayed(el)
                            or not self._is_element_active(el)):
                        continue
                    el_txt = safe_text(el)
                    if not el_txt:
                        el_txt = el.get_attribute("textContent") or ""
                    if self._is_progression_i18n_key(el_txt):
                        print(f"   🚀 Progression (clé i18n non traduite) : "
                              f"'{el_txt.strip()[:45]}' → clic")
                        self._ghost_click(el)
                        time.sleep(5)
                        return True
        except WebDriverException:
            pass

        # ── CAS SPÉCIAL : tuile « Continue » (vignette image + chevron
        # dans un cercle orange). Le mot « Continue » y est MASQUÉ sur
        # ordinateur (classe lg:hidden) → la recherche par texte visible
        # échoue et la tuile était mal détectée. On la repère par sa
        # STRUCTURE : une icône chevron-right dans un cercle bg-primary-80,
        # à l'intérieur d'un conteneur cliquable (cursor-pointer). On
        # clique ce conteneur.
        try:
            tile = self.driver.execute_script("""
                var svgs = document.querySelectorAll(
                    'svg[data-icon="chevron-right"]');
                var candidates = [];
                for (var i = 0; i < svgs.length; i++) {
                    var s = svgs[i];
                    var r = s.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    // (a) le chevron doit être dans un cercle orange
                    //     (bg-primary-80) = signature de la tuile Continue
                    var inCircle = false, q = s;
                    for (var a = 0; a < 4 && q; a++) {
                        var c = (q.getAttribute
                                 && q.getAttribute('class') || '').toString();
                        if (c.indexOf('bg-primary-80') >= 0) {
                            inCircle = true; break;
                        }
                        q = q.parentElement;
                    }
                    if (!inCircle) continue;
                    // (b) remonte vers le conteneur cliquable. On part du
                    //     parent pour ne pas renvoyer le <svg> lui-même
                    //     (son curseur peut être hérité du conteneur).
                    var p = s.parentElement, container = null;
                    for (var k = 0; k < 8 && p; k++) {
                        var cls = (p.getAttribute
                                   && p.getAttribute('class') || '').toString();
                        if (cls.indexOf('cursor-pointer') >= 0) {
                            container = p; break;
                        }
                        var cur = '';
                        try { cur = getComputedStyle(p).cursor; } catch (e) {}
                        if (cur === 'pointer' && p.tagName
                                && p.tagName.toLowerCase() !== 'svg') {
                            container = p; break;
                        }
                        p = p.parentElement;
                    }
                    if (container) candidates.push(container);
                }
                if (candidates.length === 0) return null;
                // (c) S'il y a PLUSIEURS vignettes (grille de leçons), on
                //     préfère celle qui contient vraiment le mot « Continue »
                //     (même masqué : textContent le voit) plutôt que la 1re
                //     venue → évite de cliquer une mauvaise leçon.
                for (var j = 0; j < candidates.length; j++) {
                    var t = (candidates[j].textContent || '').toLowerCase();
                    if (t.indexOf('continue') >= 0
                            || t.indexOf('continuer') >= 0) {
                        return candidates[j];
                    }
                }
                return candidates[0];
            """)
            if tile is not None:
                print("   ➡️  Clic progression : tuile « Continue » "
                      "(chevron orange, texte masqué)")
                self._ghost_click(tile)
                self.memory_context = ""
                self.last_reflex_clicked = tile
                time.sleep(5)
                return True
        except WebDriverException:
            pass

        progression_labels = [
            "Move on to the next",
            "Next activity",
            "Next",
            "Suivant",
            "Continue",
            "Continuer",
            "Finish",
            "Terminer",
            "See results",
            "Consulter les résultats",
        ]
        PARASITES = {"zoom", "exit", "close", "fermer", "help",
                     "aide", "info", "menu", "back"}

        diag_rejected = []  # pour log diagnostic si aucun match

        for lb in progression_labels:
            lo = lb.lower()
            try:
                # XPath élargi : inclut aussi les <span>, <p>, et les
                # éléments avec cursor-pointer (au cas où le site
                # utilise des conteneurs non standard). On remontera
                # à l'ancêtre cliquable ensuite.
                xp = (
                    f"//button[contains(translate(., "
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                    f"'abcdefghijklmnopqrstuvwxyz'), '{lo}')] | "
                    f"//a[contains(translate(., "
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                    f"'abcdefghijklmnopqrstuvwxyz'), '{lo}')] | "
                    f"//div[@role='button' and contains(translate(., "
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                    f"'abcdefghijklmnopqrstuvwxyz'), '{lo}')] | "
                    f"//div[contains(@class, 'cursor-pointer') and "
                    f"contains(translate(., "
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                    f"'abcdefghijklmnopqrstuvwxyz'), '{lo}')] | "
                    f"//span[contains(translate(., "
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                    f"'abcdefghijklmnopqrstuvwxyz'), '{lo}')]"
                )
                for el in self.driver.find_elements(By.XPATH, xp):
                    if not safe_is_displayed(el):
                        continue
                    if not self._is_element_active(el):
                        continue
                    el_txt = safe_text(el)
                    el_txt_lo = el_txt.strip().lower()
                    if is_skip_word(el_txt):
                        diag_rejected.append(f"'{el_txt[:30]}' → skip_word")
                        continue
                    # ── MATCH EXACT OBLIGATOIRE ────────────────────────
                    # Un bouton de progression s'appelle EXACTEMENT
                    # "Next", "Move on to the next", etc. — pas
                    # "...next month?" ni une longue phrase qui
                    # contient le mot par hasard.
                    #
                    # On retire la ponctuation finale (Next!, Next>,
                    # etc.) pour le test, mais le texte doit être
                    # entièrement le label cherché.
                    el_clean = el_txt_lo
                    while el_clean and el_clean[-1] in ",.!?;:>›":
                        el_clean = el_clean[:-1]
                    el_clean = el_clean.strip()
                    el_tag_now = safe_tag(el)
                    el_role = safe_attr(el, "role").lower()
                    is_real_button_tag = (
                        el_tag_now in ("button", "a")
                        or el_role == "button"
                    )
                    if el_clean != lo:
                        # Extension type "Next activity", "Next exercise"
                        # acceptée UNIQUEMENT pour les vrais boutons
                        # (button/a/div role=button). Pour les <span>,
                        # <div> simple, etc. on exige le match exact —
                        # sinon on risque de matcher "next month"
                        # placé dans la zone de réponse comme s'il
                        # s'agissait du bouton Next.
                        accepted_extension = (
                            is_real_button_tag
                            and el_clean.startswith(lo + " ")
                            and len(el_clean) <= 30
                            and lo in ("next", "move on")
                        )
                        if not accepted_extension:
                            if len(el_txt) > 5:
                                diag_rejected.append(
                                    f"'{el_txt[:30]}' → texte trop long "
                                    f"(non match exact pour '{lb}')",
                                )
                            continue
                    # Exclusion : élément descendant d'une zone de
                    # réponse (mot placé par le user). Un vrai bouton
                    # de progression n'est jamais dans user-answer-
                    # container ni dans une drop-zone.
                    try:
                        in_answer_zone = self.driver.execute_script(
                            """
                            var el = arguments[0];
                            // (a) L'élément ENGLOBE-t-il une option de QCM ?
                            // Le span conteneur d'une réponse contient le
                            // <label>/<input> de l'option ; un vrai bouton
                            // de navigation n'en contient jamais.
                            try {
                                if (el.querySelector && el.querySelector(
                                    'input[type=\"radio\"],'
                                    + 'input[type=\"checkbox\"],label'))
                                    return true;
                            } catch (e) {}
                            // (b) L'élément est-il DANS une option / zone réponse ?
                            var p = el.parentElement;
                            while (p) {
                                var tag = (p.tagName || '').toLowerCase();
                                // Un bouton de navigation n'est jamais
                                // DANS un <label> (= option radio/checkbox)
                                if (tag === 'label') return true;
                                var dn = p.getAttribute && p.getAttribute('data-name');
                                if (dn === 'user-answer-container') return true;
                                var cls = (p.className || '').toString().toLowerCase();
                                if (cls.indexOf('ordering-drop-zone') >= 0) return true;
                                if (cls.indexOf('drop-zone') >= 0) return true;
                                if (cls.indexOf('custom-dashed-border') >= 0) return true;
                                p = p.parentElement;
                            }
                            return false;
                            """,
                            el,
                        )
                        if in_answer_zone:
                            diag_rejected.append(
                                f"'{el_txt[:30]}' → dans zone de réponse",
                            )
                            continue
                    except (JavascriptException,
                            StaleElementReferenceException,
                            WebDriverException):
                        pass
                    # Filtre parasites (au cas où le site aurait un
                    # bouton "Zoom Next" ou similaire — improbable
                    # mais ceinture+bretelles)
                    if (any(p in el_clean for p in PARASITES)
                            and lo not in PARASITES):
                        diag_rejected.append(
                            f"'{el_txt[:30]}' → contient parasite",
                        )
                        continue
                    # NB : on NE filtre PLUS sur l'attribut `total-pages`.
                    # Hypothèse initiale fausse : ce n'est pas un marqueur
                    # de "pager audio" — le VRAI bouton "Next" de la page
                    # de correction le porte aussi. Le faux "Next" (le mot
                    # dans la phrase de correction) est déjà écarté par
                    # l'exigence de cliquabilité ci-dessous.
                    # Un bouton de progression DOIT être cliquable :
                    # balise button/a, role=button, ou cursor:pointer
                    # (sur lui-même ou un ancêtre proche). Le mot "next"
                    # de la phrase de correction (réponse dans une
                    # drop-zone) est un <span> cursor:auto SANS ancêtre
                    # cliquable → on le rejette. Bug observé : le bot
                    # cliquait le mot "next" du texte en boucle.
                    tag = safe_tag(el)
                    el_role3 = safe_attr(el, "role").lower()
                    target_el = el
                    clickable = (tag in ("button", "a")
                                 or el_role3 == "button")
                    if not clickable:
                        try:
                            if self.driver.execute_script(
                                "return window.getComputedStyle("
                                "arguments[0]).cursor;", el) == "pointer":
                                clickable = True
                        except (JavascriptException, WebDriverException):
                            pass
                    if not clickable:
                        # Remonte vers un ancêtre cliquable.
                        try:
                            cur = el
                            for _ in range(5):
                                parent = cur.find_element(
                                    By.XPATH, "./..")
                                p_tag = safe_tag(parent)
                                p_role = safe_attr(parent, "role").lower()
                                p_cursor = ""
                                try:
                                    p_cursor = self.driver.execute_script(
                                        "return window.getComputedStyle("
                                        "arguments[0]).cursor;", parent)
                                except (JavascriptException,
                                        WebDriverException):
                                    p_cursor = ""
                                if (p_tag in ("button", "a")
                                        or p_role == "button"
                                        or p_cursor == "pointer"):
                                    target_el = parent
                                    clickable = True
                                    break
                                cur = parent
                        except (NoSuchElementException,
                                StaleElementReferenceException,
                                WebDriverException):
                            pass
                    if not clickable:
                        diag_rejected.append(
                            f"'{el_txt[:30]}' → pas cliquable "
                            f"(texte, pas un bouton)",
                        )
                        continue
                    print(f"   ➡️  Clic progression : '{lb}' "
                          f"(tag={safe_tag(target_el)}, "
                          f"texte='{safe_text(target_el)[:30]}')")
                    self._ghost_click(target_el)
                    self.memory_context = ""
                    # Fin d'exercice (Finish / See results...) → on oublie
                    # les textes mémorisés de la leçon.
                    if is_finish_label(lb) or is_finish_label(
                            safe_text(target_el)):
                        self._clear_lesson_texts("bouton Finish cliqué")
                    self.last_reflex_clicked = target_el
                    time.sleep(5)
                    return True
            except WebDriverException:
                continue

        # Aucun match : log diagnostique pour comprendre pourquoi
        if diag_rejected:
            print("   ⚠️ Aucun bouton de progression cliqué. Candidats "
                  "rejetés :")
            for r in diag_rejected[:10]:
                print(f"      • {r}")
        else:
            print("   ⚠️ Aucun bouton de progression trouvé sur la page.")
        return False

    def _click_skip_button(self) -> bool:
        """Clique le bouton « Skip » (passer l'exercice). True si cliqué."""
        try:
            for el in self.driver.find_elements(
                    By.XPATH, "//button | //*[@role='button'] | //a"):
                if not safe_is_displayed(el):
                    continue
                if safe_text(el).strip().lower() == "skip":
                    self._ghost_click(el)
                    return True
        except WebDriverException:
            pass
        return False

    def _install_fake_speech(self) -> None:
        """Installe la reconnaissance vocale simulée : sur chaque page
        chargée à l'avenir (CDP) ET sur la page actuelle."""
        if not SPEAK_MODE:
            return
        if not getattr(self, "_fake_speech_registered", False):
            try:
                # « __botEarly » : le micro factice a été installé AVANT
                # les scripts du site (indispensable : le site garde une
                # référence au micro dès son chargement).
                self.driver.execute_cdp_cmd(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": "window.__botEarly = true;\n"
                               + JS_FAKE_SPEECH})
                self._fake_speech_registered = True
            except Exception as e:  # noqa: BLE001
                print(f"   ⚠️ Voix simulée (CDP) : {str(e)[:60]}")
        try:
            self.driver.execute_script(JS_FAKE_SPEECH)
        except WebDriverException:
            pass

    _JS_SPEECH_TARGET = r"""
        // Phrase à prononcer : texte visible le plus en évidence (plus
        // grande police) près de l'exercice, hors boutons et consignes.
        const UI = /can.?t talk|repeat|r[ée]p[ée]te|listen|[ée]coute|pronounce|prononce|record|enregistr|click|cliqu|next|skip|validate|microphone|speak now|parlez/i;
        const out = [];
        const els = document.querySelectorAll('p, span, div, h1, h2, h3, h4, li, strong, b');
        for (const el of els) {
            if (el.closest('button, a, [role=button], nav, header, script, style')) continue;
            const r = el.getBoundingClientRect();
            if (!r.width || !r.height || r.bottom < 0) continue;
            const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
            if (t.length < 2 || t.length > 250 || !/\p{L}/u.test(t)) continue;
            if ((el.innerText || '').trim().includes('\n')) continue;
            if (UI.test(t)) continue;
            // élément le plus « profond » portant ce texte
            let deeper = false;
            for (const ch of el.children)
                if ((ch.innerText || '').replace(/\s+/g, ' ').trim() === t) { deeper = true; break; }
            if (deeper) continue;
            const fs = parseFloat(getComputedStyle(el).fontSize) || 0;
            out.push([fs, t.length, t]);
        }
        out.sort((a, b) => (b[0] - a[0]) || (b[1] - a[1]));
        return out.length ? out[0][2] : '';
    """

    def _find_mic_button(self):
        try:
            return self.driver.execute_script("""
                const cand = [];
                for (const b of document.querySelectorAll('button, [role=button], a, div[class*=cursor-pointer]')) {
                    const r = b.getBoundingClientRect();
                    if (!r.width || !r.height) continue;
                    if (b.getAttribute('data-role') === 'skip-speak-part') continue;
                    const svg = b.querySelector('svg');
                    const icon = svg ? (svg.getAttribute('data-icon') || '') : '';
                    const lbl = ((b.getAttribute('aria-label') || '') + ' ' +
                                 (b.getAttribute('title') || '') + ' ' +
                                 (b.getAttribute('data-role') || '') + ' ' +
                                 (b.innerText || '')).toLowerCase();
                    if (/megaphone|volume|speaker/.test(icon)) continue;
                    if (/microphone|\bmic\b/.test(icon) ||
                        /micro|record|speak|parler|enregistr/.test(lbl))
                        cand.push(b);
                }
                return cand.length ? cand[0] : null;
            """)
        except WebDriverException:
            return None

    _JS_STOP_BUTTON = r"""
        // Bouton « carré » qui arrête l'enregistrement.
        const vis = e => { const r = e.getBoundingClientRect();
                           return r.width > 0 && r.height > 0; };
        // Signature GlobalExam : <button><span class="… rounded-[2px]">
        // (petit carré blanc) à côté du point rouge et du minuteur.
        for (const sq of document.querySelectorAll('button span[class*="rounded-[2px]"]')) {
            const b = sq.closest('button');
            if (b && vis(b)) return b;
        }
        const btns = [...document.querySelectorAll(
            'button, [role=button], div[class*=cursor-pointer], span[class*=cursor-pointer]')].filter(vis);
        for (const b of btns) {
            const svg = b.querySelector('svg');
            const ic = svg ? (svg.getAttribute('data-icon') || '') : '';
            const lbl = ((b.getAttribute('aria-label') || '') + ' ' +
                         (b.getAttribute('title') || '')).toLowerCase();
            if (/stop|square/.test(ic) || /stop|arr[êe]ter/.test(lbl)) return b;
        }
        // Repli : bouton à côté du minuteur « 00:42 » de l'enregistreur
        const timer = [...document.querySelectorAll('span, div, p')].find(e =>
            vis(e) && /^\d{1,2}:\d{2}$/.test((e.innerText || '').trim()));
        if (timer) {
            let c = timer.parentElement;
            for (let i = 0; i < 4 && c; i++, c = c.parentElement) {
                const bs = [...c.querySelectorAll(
                    'button, [role=button], div[class*=cursor-pointer], span[class*=cursor-pointer], svg')]
                    .filter(vis).map(e => e.tagName.toLowerCase() === 'svg'
                        ? (e.closest('button, [role=button], div, span') || e) : e);
                if (bs.length) return bs[bs.length - 1];
            }
        }
        return null;
    """

    def _recording_ui_visible(self) -> bool:
        """Enregistrement EN COURS (pas l'écran de réécoute) : point rouge
        (bg-warning-80, rond) juste à côté du minuteur « 00:xx »."""
        try:
            return bool(self.driver.execute_script(r"""
                const vis = e => { const r = e.getBoundingClientRect();
                                   return r.width > 0 && r.height > 0; };
                for (const dot of document.querySelectorAll(
                        '.rounded-full[class*="bg-warning"], .rounded-full[class*="bg-red"], .rounded-full[class*="bg-error"]')) {
                    const box = dot.parentElement;
                    if (!box || !vis(box)) continue;
                    if (getComputedStyle(dot).display === 'none') continue;
                    if (/\d{1,2}:\d{2}/.test(box.innerText || '')) return true;
                }
                return false;
            """))
        except WebDriverException:
            return False

    def _click_stop_recording(self) -> bool:
        try:
            stop = self.driver.execute_script(self._JS_STOP_BUTTON)
        except WebDriverException:
            stop = None
        if stop is None:
            print("   ⚠️ Bouton « stop » de l'enregistrement introuvable")
            return False
        print("   ⏹️  Fin de l'enregistrement (bouton carré)")
        self._ghost_click(stop)
        return True

    def _exercise_audio_url(self) -> str:
        try:
            urls = self.driver.execute_script("""
                const out = [];
                document.querySelectorAll('audio, video, source').forEach(a => {
                    const u = a.currentSrc || a.src || '';
                    if (u) out.push(u);
                });
                return out;
            """) or []
        except WebDriverException:
            urls = []
        for u in urls:
            if "/audio/" in u or u.lower().split("?")[0].endswith(
                    (".mp3", ".m4a", ".ogg", ".wav")):
                return u
        return urls[0] if urls else ""

    def _load_voice(self) -> bool:
        """Télécharge l'audio natif de l'exercice et le confie au micro
        factice de la page (window.__botVoiceB64)."""
        url = self._exercise_audio_url()
        if not url:
            print("   🎙️ Audio natif introuvable sur la page")
            return False
        data = b""
        import urllib.request
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0"})
                data = urllib.request.urlopen(req, timeout=15).read()
                break
            except Exception as e:  # noqa: BLE001
                print(f"   ⚠️ Téléchargement de l'audio (essai "
                      f"{attempt + 1}/3) : {str(e)[:60]}")
                time.sleep(1.5)
        b64 = base64.b64encode(data).decode("ascii") if data else ""
        if not b64:
            # Secours : Chrome télécharge lui-même l'audio (sa connexion
            # marche, puisque la page s'affiche)
            try:
                b64 = self.driver.execute_async_script("""
                    const done = arguments[arguments.length - 1];
                    fetch(arguments[0]).then(r => r.arrayBuffer())
                      .then(buf => {
                        let s = ''; const b = new Uint8Array(buf);
                        for (let i = 0; i < b.length; i += 8192)
                          s += String.fromCharCode.apply(null,
                                                         b.subarray(i, i + 8192));
                        done(btoa(s));
                      }).catch(() => done(''));
                """, url) or ""
                if b64:
                    print("   🎙️ Audio récupéré via Chrome (secours)")
            except WebDriverException:
                b64 = ""
        if not b64:
            return False
        data = base64.b64decode(b64)
        try:
            self.driver.execute_script(
                "window.__botVoiceB64 = arguments[0];"
                "window.__botVoiceDur = 0; window.__botGumCalls = 0;",
                b64)
        except WebDriverException:
            return False
        print(f"   🎙️ Voix prête : audio natif ({len(data) // 1024} Ko)")
        return True

    def _try_speak(self, link) -> bool:
        """Exercice « répète le mot » : le bot clique « Pronounce », fait
        entendre au site l'audio NATIF de l'exercice via un micro factice,
        puis clique le carré pour arrêter. True si l'enregistrement a été
        fait et accepté (pas d'avertissement « silent »)."""
        self._speak_fail = ""
        # Micro factice installé APRÈS le chargement de la page (ex. 1er
        # écran de prononciation de la session) : le site utilise alors le
        # VRAI micro. On recharge la page une fois pour l'installer avant.
        self._install_fake_speech()
        try:
            early = bool(self.driver.execute_script(
                "return !!window.__botEarly;"))
        except WebDriverException:
            early = True
        if not early and not getattr(self, "_speak_reloaded", False):
            self._speak_reloaded = True
            print("   🔄 Micro factice pas encore actif sur cette page → "
                  "rechargement de la page")
            try:
                self.driver.refresh()
                time.sleep(5)
                self._activate_page()
                self._play_exercise_audio()
            except WebDriverException:
                pass
        mic = self._find_mic_button()
        if mic is None:
            self._speak_fail = "bouton « Pronounce » introuvable"
            print("   🎙️ Bouton « Pronounce » introuvable → repli "
                  "« I can't talk right now »")
            return False
        def _mic_enabled() -> bool:
            try:
                return not self.driver.execute_script(
                    "return !!(arguments[0].disabled || "
                    "arguments[0].hasAttribute('disabled'));", mic)
            except WebDriverException:
                return True
        for _ in range(3):
            if _mic_enabled():
                break
            print("   🔊 « Pronounce » encore grisé → réécoute de l'audio")
            self._play_exercise_audio()
            time.sleep(1.0)
            fresh = self._find_mic_button()
            if fresh is not None:
                mic = fresh
        if not _mic_enabled():
            self._speak_fail = ("bouton « Pronounce » resté désactivé "
                                "(audio non écouté jusqu'au bout)")
            print("   ⚠️ « Pronounce » toujours désactivé → repli")
            return False
        self._install_fake_speech()
        self._activate_page()
        if not self._load_voice():
            self._speak_fail = "audio natif indisponible"
            print("   🎙️ Pas de voix disponible → repli « I can't talk "
                  "right now »")
            return False
        print("   🎙️ Prononciation : clic « Pronounce » (voix native)")
        # On essaie 3 façons de cliquer (vrai clic souris CDP, clic
        # Selenium, clic JS) tant que le site n'a pas ouvert le micro.
        dur, calls, diag = 0.0, 0, {}
        for attempt in range(3):
            if attempt:
                fresh = self._find_mic_button()
                if fresh is not None:
                    mic = fresh
                if self._recording_ui_visible():
                    pass                     # déjà en train d'enregistrer
                elif attempt == 1:
                    print("   🔁 Micro pas ouvert → nouveau clic "
                          "« Pronounce » (clic Selenium)")
                    self._ghost_click(mic)
                else:
                    print("   🔁 Micro pas ouvert → nouveau clic "
                          "« Pronounce » (clic JS)")
                    try:
                        self.driver.execute_script(
                            "arguments[0].click();", mic)
                    except WebDriverException:
                        pass
            elif not self._cdp_click(mic):
                self._ghost_click(mic)
            for _ in range(10):
                time.sleep(0.5)
                try:
                    diag = self.driver.execute_script(
                        "return {calls: window.__botGumCalls || 0,"
                        " dur: window.__botVoiceDur || 0,"
                        " entered: window.__botGumEntered || 0,"
                        " ctx: window.__botCtxState || '',"
                        " err: window.__botGumErr || '',"
                        " patched: !!(navigator.mediaDevices &&"
                        "   navigator.mediaDevices.__botPatched),"
                        " any: window.__botGumEntered || 0,"
                        " active: !!(navigator.userActivation &&"
                        "   navigator.userActivation.hasBeenActive)};"
                    ) or {}
                except WebDriverException:
                    diag = {}
                calls, dur = diag.get("calls", 0), diag.get("dur", 0)
                if calls or diag.get("err"):
                    break
                # Le site enregistre déjà (micro ouvert au chargement de la
                # page, sans redemander) → on joue la voix dans ce flux.
                if self._recording_ui_visible():
                    try:
                        dur = self.driver.execute_async_script(
                            "const done = arguments[arguments.length-1];"
                            "(window.__botPlayVoice ? window.__botPlayVoice()"
                            " : Promise.resolve(0)).then(done)"
                            ".catch(e => done(0));") or 0
                    except WebDriverException:
                        dur = 0
                    if dur:
                        calls = 1
                        print("   🎙️ Micro déjà ouvert par le site → voix "
                              "jouée dans l'enregistrement en cours")
                        break
            if calls:
                break
            print(f"   🔬 Micro factice : {diag}")
        if not calls:
            print("   ⚠️ Le site n'a pas ouvert le micro (factice) → "
                  "arrêt de l'enregistrement et repli")
            self._click_stop_recording()
            self._speak_fail = (f"le site n'a pas ouvert le micro "
                                f"(diag {diag})")
            return False
        # Laisser jouer la voix, puis arrêter (+ marge)
        time.sleep(max(1.5, float(dur or 0) + 1.2))
        self._dump_speaking_page(tag="enregistrement")
        if not self._click_stop_recording():
            self._speak_fail = "bouton carré (stop) introuvable"
            return False

        # Résultat : avertissement « silent » = échec
        silent = False
        for _ in range(8):
            time.sleep(0.8)
            try:
                body = (self.driver.execute_script(
                    "return document.body.innerText || '';") or "")
            except WebDriverException:
                body = ""
            if "appears to be silent" in body or "semble silencieu" in body:
                silent = True
                break
            if not self._recording_ui_visible():
                break
        print(f"   🎙️ Résultat : "
              f"{'❌ enregistrement jugé silencieux' if silent else '✅ enregistrement envoyé'}"
              f" (voix {float(dur or 0):.1f} s)")
        if silent:
            self._speak_fail = "enregistrement jugé silencieux par le site"
            return False
        time.sleep(1.0)
        # L'enregistrement est fait : il faut VALIDER (bouton du bas),
        # puis passer à la suite.
        validated = False
        for _ in range(4):
            if self._click_validation():
                validated = True
                break
            time.sleep(1.0)
        if not validated:
            print("   ⚠️ Bouton « Validate » introuvable après l'enregistrement")
        time.sleep(1.5)
        if self._click_progression_button():
            print("   ➡️  Exercice de prononciation terminé → suivant")
        return True

    def _dump_speaking_page(self, tag: str = "") -> None:
        """DIAGNOSTIC (2 premiers écrans par session) : enregistre le HTML
        et une capture d'un exercice de prononciation dans diagnostics/,
        pour étudier comment GlobalExam enregistre la voix."""
        n = getattr(self, "_speak_dumps", 0)
        if n >= 4:
            return
        self._speak_dumps = n + 1
        try:
            stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
            folder = os.path.join(_project_dir(), "diagnostics",
                                  f"parole_{stamp}" + (f"_{tag}" if tag else ""))
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, "page.html"), "w",
                      encoding="utf-8") as f:
                f.write(self.driver.page_source)
            self.driver.save_screenshot(os.path.join(folder, "capture.png"))
            try:
                info = self.driver.execute_script("""
                    const out = {audio: [], buttons: [], scripts: []};
                    document.querySelectorAll('audio, video, source').forEach(
                        a => out.audio.push(a.currentSrc || a.src || ''));
                    document.querySelectorAll('button, [role=button], a').forEach(b => {
                        const r = b.getBoundingClientRect();
                        if (r.width && r.height) out.buttons.push(
                            (b.innerText || '').trim().slice(0, 40) + ' | ' +
                            (b.getAttribute('data-role') || '') + ' | ' +
                            (b.className || '').toString().slice(0, 80) + ' | ' +
                            (b.querySelector('svg') ? (b.querySelector('svg')
                                .getAttribute('data-icon') || 'svg') : ''));
                    });
                    out.speech = !!(window.SpeechRecognition
                                    || window.webkitSpeechRecognition);
                    out.mediaRecorder = !!window.MediaRecorder;
                    return out;
                """)
                with open(os.path.join(folder, "infos.json"), "w",
                          encoding="utf-8") as f:
                    json.dump(info, f, ensure_ascii=False, indent=2)
            except WebDriverException:
                pass
            print(f"   🔬 Page de prononciation enregistrée : "
                  f"diagnostics/parole_{stamp}")
        except (OSError, WebDriverException) as e:
            print(f"   ⚠️ Diagnostic prononciation : {str(e)[:60]}")

    def _is_speaking_screen(self) -> bool:
        """Écran « Listen to the audio and repeat the word » : lien « I
        can't talk right now » OU bouton micro « Pronounce » visible."""
        try:
            return bool(self.driver.execute_script("""
                const vis = e => { const r = e.getBoundingClientRect();
                                   return r.width > 0 && r.height > 0; };
                if ([...document.querySelectorAll(
                        '[data-role="skip-speak-part"]')].some(vis)) return true;
                return [...document.querySelectorAll(
                        'button svg[data-icon="microphone"]')]
                    .some(s => vis(s.closest('button')));
            """))
        except WebDriverException:
            return False

    def _wait_audio_end(self, max_wait: float = 15) -> bool:
        """Attend la FIN de l'audio de la page (max max_wait s). On exige
        d'avoir vu le son DÉMARRER : avant, un audio pas encore lancé
        (« en pause ») était pris pour un audio terminé → le bouton
        « Pronounce » restait grisé. Sans <audio> détectable : pause 3 s."""
        js = """
            const a = Array.from(document.querySelectorAll('audio, video'));
            if (!a.length) return -1;
            return a.some(x => !x.paused && !x.ended) ? 1 : 0;
        """
        started = False
        t0 = time.time()
        while time.time() - t0 < max_wait:
            try:
                state = self.driver.execute_script(js)
            except WebDriverException:
                state = -1
            if state == -1:
                time.sleep(3)
                return True
            if state == 1:
                started = True
            elif started or time.time() - t0 > 4:
                time.sleep(0.6)
                return started
            time.sleep(0.3)
        return started

    _JS_AUDIO_STATE = """
        const a = Array.from(document.querySelectorAll('audio'));
        return a.map(x => ({paused: x.paused, ended: x.ended,
            t: Math.round((x.currentTime || 0) * 10) / 10,
            dur: Math.round((x.duration || 0) * 10) / 10,
            ready: x.readyState, net: x.networkState,
            err: x.error ? x.error.code : 0,
            src: (x.currentSrc || x.src || '').split('/').pop()}));
    """

    def _activate_page(self) -> None:
        """Vrai clic souris (CDP) sur le titre de l'exercice, zone sans
        action : après un rechargement de page, Chrome considère que
        l'utilisateur n'a encore rien fait et bloque son et micro. Ce clic
        « réveille » la page (navigator.userActivation)."""
        try:
            active = self.driver.execute_script(
                "return !!(navigator.userActivation && "
                "navigator.userActivation.hasBeenActive);")
            if active:
                return
            els = [e for e in self.driver.find_elements(By.CSS_SELECTOR, "h1")
                   if safe_is_displayed(e)]
            if els and self._cdp_click(els[0]):
                time.sleep(0.3)
                url = self.driver.current_url
                if getattr(self, "_activated_url", None) != url:
                    self._activated_url = url
                    print("   👆 Page « réveillée » (clic sur le titre) → "
                          "son et micro autorisés")
        except WebDriverException:
            pass

    def _cdp_click(self, el) -> bool:
        """Vrai clic souris (CDP) au centre de l'élément. Contrairement à
        un clic JS, il compte comme une action de l'utilisateur pour
        Chrome → le site a le droit de lancer le son. False si un autre
        élément (overlay, popup…) recouvre le bouton."""
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center',"
                "behavior:'instant'});", el)
            time.sleep(0.25)
            info = self.driver.execute_script("""
                const el = arguments[0], r = el.getBoundingClientRect();
                const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
                const hit = document.elementFromPoint(cx, cy);
                return {cx, cy, ok: !!hit && (hit === el || el.contains(hit)),
                        hit: hit ? (hit.tagName + '.' +
                              String(hit.className || '').slice(0, 40)) : ''};
            """, el)
            if not info or not info.get("ok"):
                print(f"   ⚠️ Clic bloqué : élément recouvert par "
                      f"{(info or {}).get('hit', '?')}")
                return False
            x, y = info["cx"], info["cy"]
            for typ, btn, bts in (("mouseMoved", "none", 0),
                                  ("mousePressed", "left", 1),
                                  ("mouseReleased", "left", 0)):
                ev = {"type": typ, "x": x, "y": y, "button": btn,
                      "buttons": bts}
                if typ != "mouseMoved":
                    ev["clickCount"] = 1
                self.driver.execute_cdp_cmd("Input.dispatchMouseEvent", ev)
                time.sleep(0.06)
            return True
        except WebDriverException:
            return False

    def _play_exercise_audio(self) -> bool:
        """Clique le haut-parleur de l'exercice et attend la fin du son.
        Si le son ne démarre pas (clic bloqué, Chrome qui refuse la
        lecture après un rechargement de page…), on enchaîne :
          1) vrai clic souris CDP ;
          2) lecture forcée en JS (audio.play()) ;
          3) lecture forcée en muet (toujours autorisée par Chrome) ;
          4) signal « audio terminé » envoyé au site.
        Retourne True si l'audio a été joué jusqu'au bout."""
        try:
            megs = [m for m in self.driver.find_elements(
                By.CSS_SELECTOR, 'svg[data-icon="megaphone"]')
                if safe_is_displayed(m)]
        except WebDriverException:
            megs = []
        if not megs:
            return False
        try:
            btn = megs[0].find_element(By.XPATH, "./ancestor::button[1]")
        except (NoSuchElementException, WebDriverException):
            btn = megs[0]

        # 1) Vrai clic souris (compte comme geste utilisateur)
        self._activate_page()
        if not self._cdp_click(btn):
            self._ghost_click(btn)
        if self._wait_audio_end(max_wait=15):
            return True
        # 1 bis) Clic JS direct sur le bouton (le site lance alors l'audio
        # lui-même, comme pour un vrai clic)
        try:
            self.driver.execute_script("arguments[0].click();", btn)
        except WebDriverException:
            pass
        if self._wait_audio_end(max_wait=8):
            print("   🔊 Audio lancé au 2e clic")
            return True

        try:
            st = self.driver.execute_script(self._JS_AUDIO_STATE)
        except WebDriverException:
            st = []
        print(f"   🔇 Le son n'a pas démarré après le clic — état audio : "
              f"{st}")

        # 2) / 3) Lecture forcée en JS, puis en muet si Chrome refuse
        for muted in (False, True):
            try:
                res = self.driver.execute_async_script("""
                    const done = arguments[arguments.length - 1];
                    const muted = arguments[0];
                    const a = document.querySelector('audio');
                    if (!a) return done('no-audio');
                    try { a.currentTime = 0; } catch (e) {}
                    a.muted = muted;
                    const p = a.play();
                    if (!p || !p.then) return done('ok');
                    p.then(() => done('ok'))
                     .catch(e => done('refus: ' + (e && e.name)));
                    setTimeout(() => done('timeout'), 4000);
                """, muted)
            except WebDriverException as e:
                res = f"erreur {type(e).__name__}"
            print(f"   🔊 Lecture forcée{' (muet)' if muted else ''} : {res}")
            if res == "ok" and self._wait_audio_end(max_wait=15):
                try:
                    self.driver.execute_script(
                        "document.querySelectorAll('audio')"
                        ".forEach(a => a.muted = false);")
                except WebDriverException:
                    pass
                return True

        if str(res).startswith("refus"):
            print("   💡 Chrome bloque le son de la page : ferme TOUTES les "
                  "fenêtres Chrome puis relance l'appli (elle ouvre Chrome "
                  "avec le son autorisé).")

        # 4) Dernier recours : on signale au site que l'audio est fini
        try:
            self.driver.execute_script("""
                document.querySelectorAll('audio').forEach(a => {
                    ['play', 'playing', 'timeupdate', 'ended']
                        .forEach(t => a.dispatchEvent(new Event(t)));
                });
            """)
            print("   🔊 Signal « audio terminé » envoyé au site")
            time.sleep(0.8)
        except WebDriverException:
            pass
        return False

    def _check_reflexes(self) -> bool:
        # ── EXO SORTABLE DÉJÀ JUGÉ NON RÉSOLUBLE → Skip direct ───────
        # Si un exo « écoute + orthographe » (SortableJS) s'est déjà révélé
        # non plaçable par le bot, on Skip les suivants IMMÉDIATEMENT, sans
        # jouer l'audio ni appeler Gemini (garde-fou coût). On ne retourne
        # True que si le Skip a bien été cliqué.
        if self._sortable_unsolvable:
            try:
                if self.driver.find_elements(
                        By.CSS_SELECTOR, '[data-sortable-item]'):
                    if self._click_skip_button():
                        print("   ⏭️  Exo orthographe (non résoluble) → Skip "
                              "(aucun appel IA)")
                        self.memory_context = ""
                        time.sleep(3)
                        return True
            except WebDriverException:
                pass

        # ── EXO « écoute + choisis l'orthographe » (SortableJS) ──────
        # Les mots (data-sortable-item) peuvent être inactifs tant qu'on
        # n'a pas écouté l'audio. On joue l'audio UNE fois par écran puis
        # on laisse l'analyse normale choisir/glisser (on NE retourne PAS).
        try:
            sortables = [s for s in self.driver.find_elements(
                By.CSS_SELECTOR, '[data-sortable-item]')
                if safe_is_displayed(s)]
            if sortables:
                sig = tuple(sorted(
                    (safe_attr(s, 'data-sortable-item') or '')
                    for s in sortables))
                if sig and sig not in self._audio_clicked_sigs:
                    megs = [m for m in self.driver.find_elements(
                        By.CSS_SELECTOR, 'svg[data-icon="megaphone"]')
                        if safe_is_displayed(m)]
                    if megs:
                        try:
                            ab = megs[0].find_element(
                                By.XPATH, "./ancestor::button[1]")
                        except (NoSuchElementException, WebDriverException):
                            ab = megs[0]
                        print("   🔊 Écoute de l'audio (exo orthographe)")
                        # Lecture VÉRIFIÉE (sinon les mots restent
                        # verrouillés, ex. juste après un rechargement)
                        self._play_exercise_audio()
                        self._audio_clicked_sigs.add(sig)
        except WebDriverException:
            pass

        # ── EXERCICE DE PRONONCIATION (écouter puis répéter) ─────────
        # Le bot n'a pas de voix : impossible de « prononcer ». Sur ces
        # exercices, on écoute l'audio (haut-parleur) puis on clique
        # « I can't talk right now » pour passer la partie orale — sans
        # appel Gemini (déterministe). Signature fiable et unique : le
        # lien data-role="skip-speak-part".
        #
        # ANTI-BOUCLE : avant, l'audio était relancé à CHAQUE cycle puis le
        # lien cliqué 2 s après, alors que le son jouait encore → le clic
        # était ignoré et l'écran ne changeait jamais (boucle de 30 cycles).
        # Désormais : audio joué UNE seule fois par écran, on attend la fin
        # du son, puis clic vérifié (repli JS si le clic n'a rien fait).
        # Enregistrement en cours oublié (minuteur qui tourne) → stop.
        # Sans ça, l'IA analysait l'écran d'enregistrement et répondait
        # « Skip » (bug observé).
        try:
            if self._recording_ui_visible():
                n = getattr(self, "_stop_attempts", 0) + 1
                self._stop_attempts = n
                if n <= 3 and self._click_stop_recording():
                    time.sleep(2)
                    if not self._recording_ui_visible():
                        self._stop_attempts = 0
                        self._click_validation()
                    return True
            else:
                self._stop_attempts = 0
        except WebDriverException:
            pass

        try:
            skip_speak = [e for e in self.driver.find_elements(
                By.CSS_SELECTOR, '[data-role="skip-speak-part"]')
                if safe_is_displayed(e)]
            if skip_speak:
                link = skip_speak[0]
                try:
                    page_sig = (self._get_page_text() or "")[:400]
                except Exception:
                    page_sig = ""
                sig = (getattr(link, "id", None), page_sig)
                first_time = sig != self._speak_sig
                if first_time:
                    self._speak_sig = sig
                    self._speak_tries = 0
                    self._dump_speaking_page()
                self._speak_tries += 1

                # 1) Écouter UNE fois par écran, puis attendre la fin du son
                if first_time:
                    try:
                        megs = [m for m in self.driver.find_elements(
                            By.CSS_SELECTOR, 'svg[data-icon="megaphone"]')
                            if safe_is_displayed(m)]
                        if megs:
                            try:
                                btn = megs[0].find_element(
                                    By.XPATH, "./ancestor::button[1]")
                            except (NoSuchElementException,
                                    WebDriverException):
                                btn = megs[0]
                            print("   🔊 Prononciation : écoute de l'audio")
                            self._play_exercise_audio()
                    except WebDriverException:
                        pass

                # 1 bis) Voix simulée : une tentative par écran
                if SPEAK_MODE and first_time:
                    if self._try_speak(link):
                        self.memory_context = ""
                        time.sleep(1.5)
                        return True
                    self._speak_tries = 1  # repli : « I can't talk »
                    # Échec → capture + pause + notification AVANT le
                    # repli, pour analyser l'écran tel qu'il était.
                    if PAUSE_ON_ERROR:
                        reason = getattr(self, "_speak_fail", "") or "raison inconnue"
                        self._report_wrong_answer(
                            f"Prononciation non réussie ({reason}) → "
                            f"« I can't talk right now »", {}, [], [], {})
                        # Pendant la pause, l'exercice a pu être fait à la
                        # main : on ne clique que si le lien est encore là.
                        try:
                            left = [e for e in self.driver.find_elements(
                                By.CSS_SELECTOR,
                                '[data-role="skip-speak-part"]')
                                if safe_is_displayed(e)]
                        except WebDriverException:
                            left = []
                        if not left:
                            return True
                        link = left[0]

                # 2) Trop d'échecs sur le même écran → Skip / Next
                if self._speak_tries > 4:
                    print("   ⚠️ Prononciation bloquée → tentative Skip / Suivant")
                    self._speak_tries = 0
                    if self._click_skip_button() or self._click_progression_button():
                        time.sleep(2)
                        return True
                    return False

                # 3) Passer l'oral — le bot ne peut pas parler
                print("   🗣️ Exercice de prononciation → « I can't talk "
                      "right now » (le bot n'a pas de voix)"
                      + ("" if first_time else f" [essai {self._speak_tries}]"))
                if self._speak_tries >= 2:
                    # Le clic « humain » n'a rien donné → clic JS direct
                    try:
                        self.driver.execute_script(
                            "arguments[0].click();", link)
                    except WebDriverException:
                        self._ghost_click(link)
                else:
                    self._ghost_click(link)
                self.memory_context = ""
                self.last_reflex_clicked = link
                time.sleep(2)
                return True
        except WebDriverException:
            pass

        # ── PRIORITÉ 0 : PAGE DE CORRECTION DÉTECTÉE ─────────────────
        # Si un feedback de correction est affiché ET qu'aucun exercice
        # n'est en cours (pas de draggable-items actifs), on clique
        # IMMÉDIATEMENT le bouton de progression sans re-analyser.
        #
        # Sans le garde-fou draggable-items, un exo reorder_sentence
        # contenant un mot comme "Bravo" / "Quasi" / "Correct" dans son
        # stock déclencherait P0 à tort → bot skip l'exo en cliquant
        # un bouton Next ailleurs sur la page.
        has_feedback = self._has_feedback_context()
        if has_feedback:
            # Un exo en cours (draggable actif, QCM non répondu,
            # input texte vide) interdit le skip via P0 — sinon on
            # risquerait de passer un exo dont un mot matche la regex
            # feedback (ex: reorder contenant "Bravo" / "Quasi" /
            # "Correct" / "Almost" dans ses options).
            if self._has_pending_exercise():
                # Feedback détecté MAIS exo en cours → on laisse Gemini
                # analyser normalement (NE PAS skip via P0)
                pass
            else:
                progression_labels = [
                    "Move on to the next",
                    "Next activity",
                    "Next",
                    "Suivant",
                    "Continue",
                    "Continuer",
                    "Finish",
                    "Terminer",
                    "See results",
                    "Consulter les résultats",
                ]
                for lb in progression_labels:
                    try:
                        lo = lb.lower()
                        xp = (
                            f"//button[contains(translate(., "
                            f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                            f"'abcdefghijklmnopqrstuvwxyz'), '{lo}')] | "
                            f"//a[contains(translate(., "
                            f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                            f"'abcdefghijklmnopqrstuvwxyz'), '{lo}')] | "
                            f"//div[@role='button' and contains(translate(., "
                            f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                            f"'abcdefghijklmnopqrstuvwxyz'), '{lo}')]"
                        )
                        for el in self.driver.find_elements(By.XPATH, xp):
                            if not safe_is_displayed(el):
                                continue
                            if not self._is_element_active(el):
                                continue
                            el_txt = safe_text(el)
                            if is_skip_word(el_txt):
                                continue
                            el_txt_lo = el_txt.lower()
                            if lo not in el_txt_lo:
                                continue
                            # Rejette les boutons parasites (Zoom, Exit,
                            # Close, Help, Menu, Back) qui peuvent
                            # contenir par hasard le mot cherché.
                            PARASITES = {"zoom", "exit", "close",
                                         "fermer", "help", "aide",
                                         "info", "menu", "back"}
                            if (any(p in el_txt_lo for p in PARASITES)
                                    and lo not in PARASITES):
                                continue
                            print(f"   📖 Page de correction détectée — "
                                  f"clic '{lb}'")
                            self._ghost_click(el)
                            self.memory_context = ""
                            if is_finish_label(lb) or is_finish_label(el_txt):
                                self._clear_lesson_texts(
                                    "bouton Finish (réflexe)")
                            self.last_reflex_clicked = el
                            # Temps d'attente pour le chargement de
                            # l'exercice suivant (site parfois lent)
                            time.sleep(5)
                            return True
                    except WebDriverException:
                        continue

        # ── PRIORITÉ 1 : "Move on to the next" ───────────────────────
        try:
            xp = (
                "//*[contains(normalize-space(.), 'Move on to the next') "
                "and (self::button or self::a or "
                "self::div[@role='button'] or self::span)]"
            )
            candidates: list[tuple[int, object]] = []
            for el in self.driver.find_elements(By.XPATH, xp):
                if not safe_is_displayed(el):
                    continue
                try:
                    own_text = self.driver.execute_script(
                        "return Array.from(arguments[0].childNodes)"
                        ".filter(n=>n.nodeType===3)"
                        ".map(n=>n.textContent).join('').trim();",
                        el,
                    ) or safe_text(el)
                except (JavascriptException,
                        StaleElementReferenceException, WebDriverException):
                    own_text = safe_text(el)
                candidates.append((len(own_text or ""), el))

            if candidates:
                # Priorité au plus petit container (texte direct le plus court)
                candidates.sort(key=lambda x: x[0])
                btn = candidates[0][1]
                print("   🚀 REFLEX : 'Move on to the next' → clic immédiat")
                # ── Frontière d'activité ──────────────────────────────
                # "Move on to the next" quitte la leçon courante pour la
                # suivante. Juste avant, le site affiche souvent un RECAP
                # de fin qui a mémorisé un transcript inutile (résumé de
                # la leçon terminée). On vide l'accumulateur ici pour ne
                # pas traîner ce texte mort dans la leçon suivante.
                self._clear_lesson_texts(
                    "Move on to the next (nouvelle activité)")
                try:
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", btn,
                    )
                    self.driver.execute_script("arguments[0].click();", btn)
                    try:
                        parent = btn.find_element(By.XPATH, "./..")
                        self.driver.execute_script(
                            "arguments[0].click();", parent,
                        )
                    except (NoSuchElementException,
                            StaleElementReferenceException, WebDriverException):
                        pass
                    # Temps d'attente pour le chargement de l'exo
                    # suivant après clic d'un bouton de progression
                    time.sleep(5)
                    return True
                except (JavascriptException, WebDriverException):
                    pass
        except WebDriverException:
            pass

        # ── PRIORITÉ 2 : Popup SweetAlert ────────────────────────────
        try:
            popup = self.driver.find_element(
                By.CSS_SELECTOR, "button.swal2-confirm",
            )
            if safe_is_displayed(popup) and self._is_element_active(popup):
                self._ghost_click(popup)
                time.sleep(1)
                return True
        except (NoSuchElementException, WebDriverException):
            pass

        # ── PRIORITÉ 3 : bouton primary-large (retour accueil) ───────
        # CRITIQUE : sur beaucoup de sites, le bouton "Validate" /
        # "Check" / "Finish" / "Submit" est un `button-solid-primary-
        # large` qui est ACTIF dès le chargement de l'exercice (avant
        # même qu'on ait répondu). Si on le clique en priorité, on
        # valide un état vide → "Incorrect" → Next → exo skippé.
        #
        # On ne clique ici QUE si :
        #  (a) un feedback de correction est présent (exo fini)
        #  (b) le texte N'EST PAS un mot de validation/navigation
        #      (sinon on laisse _click_validation gérer après Gemini)
        has_fb_for_primary = self._has_feedback_context()
        try:
            for btn in self.driver.find_elements(
                By.CSS_SELECTOR,
                "a.button-solid-primary-large, button.button-solid-primary-large",
            ):
                if not safe_is_displayed(btn):
                    continue
                if not self._is_element_active(btn):
                    continue
                txt = safe_text(btn)
                txt_lo = txt.strip().lower()
                # Ne JAMAIS cliquer un bouton qui est un skip déguisé
                if is_skip_word(txt):
                    continue
                # ⚠️ Ne JAMAIS cliquer un bouton de validation/navigation
                #    sans feedback visible (l'exo n'est pas fini !)
                is_action_button = (
                    any(vw in txt_lo for vw in VALIDATE_WORDS)
                    or any(nw in txt_lo for nw in NAV_WORDS)
                )
                if is_action_button and not has_fb_for_primary:
                    # Laisse Gemini analyser l'exo et _click_validation
                    # décider ensuite
                    continue
                fb_tag = " (feedback OK)" if has_fb_for_primary else ""
                print(f"   🚀 REFLEX : primary-large '{txt}'{fb_tag} → clic")
                self._ghost_click(btn)
                self.last_reflex_clicked = btn
                # 5s pour les boutons de progression, 1.5s sinon
                time.sleep(5 if (is_action_button and has_fb_for_primary)
                           else 1.5)
                return True
        except WebDriverException:
            pass

        # ── PRIORITÉ 4 : boutons de navigation génériques ────────────
        # RÈGLE CRITIQUE : on NE clique JAMAIS un bouton Next/Suivant/
        # Continue/Finish en début de cycle sans preuve que l'exercice
        # est fini. Sinon risque de sauter l'exercice non résolu.
        #
        # Preuve acceptable (l'une des suivantes suffit) :
        #   a) feedback de correction visible sur la page (Correct!,
        #      Almost!, ¡Correcto!, etc. en 6 langues)
        #   b) le bouton lui-même est ACTIVEMENT cliquable (non disabled)
        #      ET ce n'est pas un bouton "Next" (Next est trop ambigu)
        has_feedback = self._has_feedback_context()

        for bt in REFLEX_BUTTONS:
            try:
                lo = bt.lower()
                xp = (
                    f"//button[contains(translate(text(), "
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                    f"'abcdefghijklmnopqrstuvwxyz'), '{lo}')] | "
                    f"//a[contains(translate(text(), "
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                    f"'abcdefghijklmnopqrstuvwxyz'), '{lo}')] | "
                    f"//div[@role='button' and contains(translate(text(), "
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                    f"'abcdefghijklmnopqrstuvwxyz'), '{lo}')] | "
                    f"//span[contains(@class, 'btn') and "
                    f"contains(translate(text(), "
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                    f"'abcdefghijklmnopqrstuvwxyz'), '{lo}')]"
                )
                for el in self.driver.find_elements(By.XPATH, xp):
                    if not safe_is_displayed(el):
                        continue
                    if not self._is_element_active(el):
                        continue
                    # Double sécurité : pas un skip déguisé
                    if is_skip_word(safe_text(el)):
                        continue
                    # ⚠️ ANTI-CLIC-PRÉMATURÉ : les boutons de navigation
                    # (Next/Suivant/Continue/Finish) ne sont cliqués que
                    # si on a un signal clair de fin d'exercice.
                    is_nav_button = norm_key(bt) in {
                        "next", "suivant", "continue", "continuer",
                        "finish", "terminer",
                    }
                    if is_nav_button and not has_feedback:
                        # Pas de preuve de fin d'exo → on ne clique pas
                        # (sera relu naturellement par Gemini à ce cycle)
                        continue
                    try:
                        same_as_last = (el == self.last_reflex_clicked)
                    except (StaleElementReferenceException, WebDriverException):
                        same_as_last = False
                    if same_as_last:
                        continue
                    self.last_reflex_clicked = el
                    fb_tag = " (feedback OK)" if has_feedback else ""
                    print(f"   🚀 REFLEX : '{bt}'{fb_tag} → clic")
                    self._ghost_click(el)
                    # Temps d'attente plus long pour les boutons de
                    # progression (Next/Finish) — le site peut mettre
                    # plusieurs secondes à charger l'exo suivant.
                    time.sleep(5 if is_nav_button else 1)
                    return True
            except WebDriverException:
                continue
        return False

    # =================================================================
    #  DASHBOARD (page d'accueil du cours)
    # =================================================================
    _JS_MODULE_OPTIONS = r"""
        const vis = e => { const r = e.getBoundingClientRect();
                           return r.width > 0 && r.height > 0; };
        return [...document.querySelectorAll('[role="option"]')]
            .filter(vis).map((o, i) => {
                const circle = o.firstElementChild;
                const ctext = circle ? (circle.innerText || '').trim() : '';
                const hasNum = /^\d+$/.test(ctext);
                const hasSvg = !!(circle && circle.querySelector('svg'));
                const txt = (o.innerText || '').replace(/\s+/g, ' ').trim();
                const m = txt.match(/(\d+)\s*\/\s*(\d+)/);
                let done = hasSvg && !hasNum;
                if (m) done = (+m[1] >= +m[2]);
                const displayed = /\bDisplayed\b|Affich/i.test(txt)
                    || o.getAttribute('aria-selected') === 'true';
                const sp = o.querySelector('span');
                const name = ((sp && sp.innerText) || txt).trim();
                return [i, done, displayed, name];
            });
    """

    def _switch_to_unfinished_module(self) -> str:
        """Ouvre le menu des modules. Si le module affiché est terminé,
        sélectionne le 1er module NON terminé. Renvoie :
        'switched' | 'current' (module affiché à faire) | 'all_done' |
        'none' (pas de menu des modules sur la page)."""
        try:
            sel = self.driver.execute_script("""
                return [...document.querySelectorAll(
                    'button[aria-haspopup="listbox"]')].find(b => {
                        const r = b.getBoundingClientRect();
                        return r.width > 0 && r.height > 0 &&
                            /module/i.test(b.getAttribute('aria-label') || '');
                    }) || null;
            """)
        except WebDriverException:
            sel = None
        if sel is None:
            return "none"

        def _close():
            try:
                if (sel.get_attribute("aria-expanded") or "") == "true":
                    self.driver.execute_script("arguments[0].click();", sel)
                    time.sleep(0.4)
            except WebDriverException:
                pass

        try:
            if (sel.get_attribute("aria-expanded") or "") != "true":
                self._ghost_click(sel)
                time.sleep(1.0)
            opts = self.driver.execute_script(self._JS_MODULE_OPTIONS) or []
        except WebDriverException:
            return "none"
        if not opts:
            _close()
            return "none"
        shown = next((o for o in opts if o[2]), None)
        todo = [o for o in opts if not o[1]]
        if shown is not None and not shown[1]:
            _close()
            return "current"
        if not todo:
            print("   🏁 Dashboard : tous les modules sont terminés")
            _close()
            return "all_done"
        if shown is None:
            # module affiché inconnu : on ne touche à rien
            _close()
            return "current"
        target = todo[0]
        print(f"   📚 Dashboard : module « {shown[3]} » terminé → "
              f"passage au module « {target[3]} »")
        try:
            self.driver.execute_script(
                "const v = [...document.querySelectorAll('[role=option]')]"
                ".filter(e => { const r = e.getBoundingClientRect();"
                " return r.width > 0 && r.height > 0; });"
                "if (v[arguments[0]]) v[arguments[0]].click();", target[0])
        except WebDriverException:
            return "none"
        self._clear_lesson_texts("nouveau module (dashboard)")
        self._expanded_chevrons.clear()
        return "switched"

    def _pass_dashboard(self) -> bool:
        # ── GARDE-FOU GLOBAL : on ne traite le dashboard QUE si la
        # page n'a manifestement aucun exo en cours.
        #
        # Sans ce garde, P1/P2/P3 peuvent matcher des draggable-items
        # ou des éléments d'exo qui ont par hasard les classes du
        # dashboard ('border-primary-80', 'rounded-full',
        # 'cursor-pointer + SVG'). Bug observé : sur un reorder
        # les boutons-mots du stock matchaient une priorité du
        # dashboard → clic sur un mot → exo skippé silencieusement.
        try:
            if self._get_drop_zones():
                # Page avec zone de drop = forcément un exo
                return False
            if self._has_pending_exercise():
                # Page avec draggable actif / radio non coché /
                # input vide = exo en cours, on n'est pas sur
                # un dashboard.
                return False
        except WebDriverException:
            pass

        # =================================================================
        # ── NOUVELLE UI : BADGE PLAY VIOLET + MENU DES MODULES ──
        # =================================================================
        badge_xpath = (
            "//div[contains(@class, 'node-badge') "
            "and contains(@class, 'rounded-full')]"
        )

        def _click_play_badge() -> bool:
            try:
                for badge in self.driver.find_elements(By.XPATH, badge_xpath):
                    if safe_is_displayed(badge):
                        print("   🎯 Dashboard : Bouton Play violet → clic")
                        self._clear_lesson_texts("nouvelle activité (dashboard)")
                        self._expanded_chevrons.clear()
                        self._ghost_click(badge)
                        time.sleep(1.5)
                        return True
            except WebDriverException:
                pass
            return False

        # ── ÉTAPE 1 : le module AFFICHÉ est-il terminé ? ─────────────
        # Menu « Select a module of the learning path » : un module
        # terminé a un DRAPEAU dans son rond, un module à faire a son
        # NUMÉRO (et souvent un compteur « 3/5 »). Avant, le bot prenait
        # le 1er module sans « success/green » dans le HTML → il relançait
        # des modules déjà terminés (bug observé : Ropa au lieu de Casa).
        state = self._switch_to_unfinished_module()
        if state == "switched":
            time.sleep(3)
            _click_play_badge()
            return True

        # ── ÉTAPE 2 : badge Play du module en cours ──
        # (pas si TOUT est terminé : ça relancerait un module fini)
        if state != "all_done" and _click_play_badge():
            return True

        try:
            # ── PRIORITÉ 0 : bouton "Continue" de la page d'accueil ─
            # Signature : <a class="button-solid-primary-large"
            #              href=".../levels/content">Continue</a>
            #
            # Le garde-fou global ci-dessus garantit déjà qu'on n'est
            # pas dans un exo : on peut donc matcher en toute sécurité.
            for el in self.driver.find_elements(
                By.XPATH,
                "//a[contains(@class, 'button-solid-primary-large')] | "
                "//button[contains(@class, 'button-solid-primary-large')]",
            ):
                if not safe_is_displayed(el):
                    continue
                if not self._is_element_active(el):
                    continue
                txt = safe_text(el).strip().lower()
                if is_skip_word(txt):
                    continue
                # MATCH STRICT : le bouton doit avoir EXACTEMENT
                # "Continue" / "Continuer" comme texte (pas
                # "Continue last session" ni une phrase qui
                # contient ce mot).
                accueil_labels = {"continue", "continuer"}
                if txt in accueil_labels:
                    print(f"   🎯 Dashboard : bouton '{safe_text(el)[:30]}' "
                          f"→ clic (accueil)")
                    self._ghost_click(el)
                    time.sleep(2)
                    return True

            # ── PRIORITÉ 1 : cercle avec bordure orange = activité dispo
            for el in self.driver.find_elements(
                By.XPATH,
                "//div[contains(@class,'border-primary-80') and "
                "contains(@class,'rounded-full')]",
            ):
                if not safe_is_displayed(el):
                    continue
                target = el
                for _ in range(4):
                    try:
                        p = target.find_element(By.XPATH, "./..")
                    except (NoSuchElementException,
                            StaleElementReferenceException, WebDriverException):
                        break
                    cls = safe_attr(p, "class")
                    if "group" in cls or "cursor-pointer" in cls:
                        target = p
                        break
                    target = p
                print("   🎯 Dashboard : cercle orange → clic")
                # Nouvelle activité sélectionnée depuis le dashboard →
                # la leçon précédente est terminée, on oublie ses textes.
                self._clear_lesson_texts("nouvelle activité (dashboard)")
                self._expanded_chevrons.clear()
                self._ghost_click(target)
                time.sleep(1.5)
                return True

            # ── PRIORITÉ 2 : image laurel-todo ────────────────────────
            for img in self.driver.find_elements(
                By.XPATH, "//img[contains(@src, 'laurel-todo')]",
            ):
                if not safe_is_displayed(img):
                    continue
                el = img
                for _ in range(5):
                    try:
                        el = el.find_element(By.XPATH, "./..")
                    except (NoSuchElementException,
                            StaleElementReferenceException, WebDriverException):
                        break
                    if "cursor-pointer" in safe_attr(el, "class"):
                        print("   🎯 Dashboard : laurel-todo → clic")
                        self._clear_lesson_texts(
                            "nouvelle activité (dashboard)")
                        self._ghost_click(el)
                        time.sleep(1.5)
                        return True

            # ── RESTART : série terminée ────────────────────────────────
            # Placé APRÈS les vraies activités (Continue / cercle orange /
            # laurel-todo) mais AVANT la PRIORITÉ 3 (cursor-pointer+SVG)
            # qui est trop permissive et bouclait sur un élément décoratif
            # sans jamais atteindre Restart. Si un bouton "Restart" est
            # visible ici, c'est que toutes les activités sont faites.
            RESTART_LABELS = {
                "restart", "recommencer", "redémarrer", "reiniciar",
                "volver a empezar", "neu starten", "ricomincia",
                "recomeçar", "reiniciar série",
            }
            restart_visible = None   # bouton Restart visible (préféré)
            restart_hidden = None    # bouton Restart présent mais caché
            for el in self.driver.find_elements(
                By.XPATH,
                "//button[contains(@class, 'button-outline-primary')] | "
                "//a[contains(@class, 'button-outline-primary')] | "
                "//button[contains(@class, 'button-outline-primary-medium')] | "
                "//a[contains(@class, 'button-outline-primary-medium')] | "
                "//button[normalize-space()='Restart'] | "
                "//button[normalize-space()='Recommencer']",
            ):
                try:
                    txt = safe_text(el).strip().lower()
                    if not txt:
                        # .text est vide pour un élément non affiché →
                        # on lit le textContent (présent même si masqué).
                        txt = (el.get_attribute("textContent")
                               or "").strip().lower()
                except WebDriverException:
                    continue
                if txt not in RESTART_LABELS:
                    continue
                if safe_is_displayed(el) and self._is_element_active(el):
                    restart_visible = el
                    break
                # Présent dans le DOM mais masqué (ex: classe "hidden
                # lg:block" sur fenêtre étroite) → candidat de secours.
                if restart_hidden is None:
                    restart_hidden = el
            if restart_visible is not None:
                print("   🔄 Dashboard : toutes les activités faites "
                      "→ bouton 'Restart' → clic")
                self._clear_lesson_texts("restart de la série")
                self._ghost_click(restart_visible)
                time.sleep(2)
                return True
            if restart_hidden is not None:
                # Clic JS direct : déclenche le handler même si l'élément
                # est en display:none (bouton "hidden lg:block").
                print("   🔄 Dashboard : bouton 'Restart' présent mais "
                      "masqué → clic JS de secours")
                self._clear_lesson_texts("restart de la série")
                try:
                    self.driver.execute_script(
                        "arguments[0].click();", restart_hidden)
                    time.sleep(2)
                    return True
                except WebDriverException as e:
                    print(f"   ⚠️ Clic JS Restart échoué : {str(e)[:60]}")

            # ── DÉPLIAGE D'UNE PARTIE REPLIÉE (Learning path) ───────────
            # Sur la page du parcours, la partie suivante (ex: partie 9)
            # est REPLIÉE : son cercle orange n'existe pas encore tant
            # qu'on n'a pas cliqué le chevron (fa-chevron-down). Si aucune
            # activité dispo n'a été trouvée ci-dessus, on déplie la
            # première partie repliée non encore traitée → au cycle
            # suivant, le cercle orange apparaît et la PRIORITÉ 1 le clique.
            try:
                chevrons = self.driver.find_elements(
                    By.CSS_SELECTOR,
                    "[data-icon='chevron-down'], svg.fa-chevron-down")
                for chev in chevrons:
                    if not safe_is_displayed(chev):
                        continue
                    # Signature = texte de l'entête (n° + titre de partie),
                    # pour ne pas re-cliquer (et donc replier) la même.
                    sig = ""
                    try:
                        sig = (self.driver.execute_script(
                            "var e=arguments[0];"
                            "for (var i=0;i<6 && e;i++){"
                            "  var t=(e.innerText||'').trim();"
                            "  if (t.length>4) return t.slice(0,90);"
                            "  e=e.parentElement;}"
                            "return '';", chev) or "").strip()
                    except (JavascriptException, WebDriverException):
                        sig = ""
                    if sig and sig in self._expanded_chevrons:
                        continue
                    # Cherche l'ancêtre cliquable (entête de la partie) ;
                    # à défaut, on clique le chevron lui-même.
                    target = chev
                    try:
                        anc = self.driver.execute_script(
                            "var e=arguments[0];"
                            "for (var i=0;i<6 && e;i++){"
                            "  var cs=window.getComputedStyle(e);"
                            "  if (cs.cursor==='pointer' || e.onclick ||"
                            "      e.getAttribute('role')==='button')"
                            "    return e;"
                            "  e=e.parentElement;}"
                            "return null;", chev)
                        if anc is not None:
                            target = anc
                    except (JavascriptException, WebDriverException):
                        pass
                    if sig:
                        self._expanded_chevrons.add(sig)
                    print(f"   🔽 Partie repliée → dépliage (chevron) : "
                          f"'{sig[:45]}'")
                    self._ghost_click(target)
                    time.sleep(1.3)
                    return True
            except (JavascriptException, WebDriverException):
                pass

            # ── PRIORITÉ 3 : cursor-pointer + couronne SVG ───────────
            for el in self.driver.find_elements(
                By.XPATH,
                "//div[contains(@class, 'cursor-pointer') and "
                ".//*[name()='svg']]",
            ):
                if not safe_is_displayed(el):
                    continue
                inner = safe_attr(el, "innerHTML")
                if "laurel-done" in inner or "laurel-success" in inner:
                    continue
                if (safe_size(el).get("width", 0) or 0) > 40:
                    print("   🎯 Dashboard : cursor-pointer + SVG → clic")
                    self._ghost_click(el)
                    time.sleep(1.5)
                    return True

            # ── FALLBACK : anciens sélecteurs (plus spécifiques pour
            # éviter de matcher les boutons draggable-item / réponses
            # QCM qui peuvent avoir border-primary en état focus) ────
            for xp in (
                "//*[contains(text(), 'Unlock the next one!')]"
                "/preceding-sibling::div",
                "//div[contains(@class, 'rounded-circle') and "
                "not(contains(@class, 'success'))]//*[name()='svg']",
                # border-warning est safe (exclusif dashboard)
                "//div[contains(@class, 'border-warning') "
                "and not(contains(@class, 'draggable-item')) "
                "and not(ancestor::*[contains(@data-name, 'answer-container')])]",
            ):
                for el in self.driver.find_elements(By.XPATH, xp):
                    if not safe_is_displayed(el):
                        continue
                    if (safe_size(el).get("width", 0) or 0) < 15:
                        continue
                    # Exclusion supplémentaire : l'élément ne doit PAS
                    # être dans une zone d'exo (stock / zone de réponse)
                    cls = safe_attr(el, "class").lower()
                    if "draggable-item" in cls:
                        continue
                    print(f"   🎯 Dashboard fallback : {xp[:50]}... → clic")
                    self._ghost_click(el)
                    time.sleep(1)
                    return True
        except Exception as e:
            print(f"   ⚠️ _pass_dashboard : {str(e)[:80]}")
        return False

    # =================================================================
    #  CARROUSEL
    # =================================================================
    def _find_carousel_arrow(self):
        for xp in (
            "//*[name()='svg' and @data-icon='chevron-right']",
            "//button[contains(@class, 'slick-next')]",
            "//div[contains(@class, 'swiper-button-next')]",
            "//button[@aria-label='Next slide']",
            "//*[contains(@class, 'carousel-control-next')]",
        ):
            try:
                vis = [a for a in self.driver.find_elements(By.XPATH, xp)
                       if safe_is_displayed(a) and not safe_text(a)]
                if vis:
                    return sorted(
                        vis, key=lambda x: safe_location(x).get("x", 0) or 0,
                    )[-1]
            except WebDriverException:
                continue
        return None

    def _is_card_carousel(self) -> bool:
        """Page de fiches (vocabulaire, règles…) avec une flèche pour
        passer à la suivante, SANS rien à répondre (pas de tuiles, de
        cases à cocher, de champ texte)."""
        try:
            busy = self.driver.execute_script("""
                const vis = e => { const r = e.getBoundingClientRect();
                                   return r.width > 0 && r.height > 0; };
                return [...document.querySelectorAll(
                    '[data-draggable-item-id], [data-sortable-item], '
                    + 'input[type=radio], input[type=checkbox], '
                    + 'input[type=text], textarea, '
                    + '[data-name=user-answer-container], .dropzone')]
                    .some(vis);
            """)
        except WebDriverException:
            return False
        return not busy

    def _run_carousel(self) -> None:
        """Fait défiler TOUTES les diapos : la flèche est re-cherchée à
        chaque fois (avant : trouvée une seule fois, elle devenait
        périmée après le 1er clic → un seul clic). Arrêt quand il n'y a
        plus de flèche ou que la page ne change plus."""
        try:
            body = self.driver.find_element(By.TAG_NAME, "body")
        except (NoSuchElementException, WebDriverException):
            body = None
        seen, still, arrow, last_sig = set(), 0, None, None
        for _ in range(40):
            # Signature de la diapo (minuteurs audio retirés)
            sig = re.sub(r"\d{1,2}:\d{2}", "", self._get_page_text())[:1500]
            if sig in seen and sig != last_sig:
                break                        # retour au début : tour fini
            if sig == last_sig:
                still += 1
                if still >= 2:
                    break                    # plus rien ne bouge
            else:
                still = 0
                seen.add(sig)
                # Chaque diapo peut porter un texte / un transcript : on
                # les récolte AVANT de passer à la suivante.
                self._harvest_page_texts()
            last_sig = sig
            arrow = (self._find_carousel_arrow()
                     or self._find_stock_pagination_arrow())
            if arrow is None or not safe_is_displayed(arrow):
                break
            # Flèche grisée = dernière diapo
            try:
                if self.driver.execute_script("""
                    const a = arguments[0];
                    const b = a.closest('button, [role=button], a') || a;
                    const st = getComputedStyle(b);
                    return !!(b.disabled || b.getAttribute('aria-disabled') === 'true'
                              || parseFloat(st.opacity) < 0.45
                              || st.pointerEvents === 'none');
                """, arrow):
                    break
            except WebDriverException:
                pass
            try:
                if not self._cdp_click(arrow):
                    self._ghost_click(arrow)
            except Exception:  # noqa: BLE001
                if body is not None:
                    try:
                        body.send_keys(Keys.ARROW_RIGHT)
                    except WebDriverException:
                        pass
            time.sleep(0.8)                  # animation de la diapo
        slides = len(seen)
        print(f"   🎠 Carrousel : {slides} diapo(s) parcourue(s)")

        time.sleep(1.5)
        self._harvest_page_texts()          # dernière diapo
        for lb in ("Next", "Suivant", "Continue", "Continuer", "Finish"):
            try:
                xp = (
                    f"//button[contains(., '{lb}')] | "
                    f"//a[contains(., '{lb}')] | "
                    f"//div[@role='button' and contains(., '{lb}')]"
                )
                for btn in self.driver.find_elements(By.XPATH, xp):
                    if not safe_is_displayed(btn):
                        continue
                    if (safe_location(btn).get("y", 0) or 0) <= 100:
                        continue
                    # Sécurité : pas un bouton Skip déguisé
                    if is_skip_word(safe_text(btn)):
                        continue
                    self._ghost_click(btn)
                    return
            except WebDriverException:
                continue
        if arrow and safe_is_displayed(arrow):
            self._ghost_click(arrow)
        self._click_validation()

    # =================================================================
    #  VALIDATION (bouton Valider/Submit/etc.)
    # =================================================================
    # Mots explicitement interdits dans le texte d'un bouton de
    # validation (Skip, Pass, Suivant sans validation, etc.)
    _VALIDATE_FORBIDDEN_KW = (
        "skip", "passer", "sauter", "pass",
        "hint", "indice",
        "help", "aide",
        "exit", "quitter",
        "menu", "close", "fermer", "cancel", "annuler",
    )

    def _is_valid_validate_button(self, el) -> bool:
        """
        Un bouton est un VRAI bouton de validation si :
         - affiché + actif (pas disabled/inactive)
         - son texte contient UN mot de VALIDATE_WORDS mais AUCUN
           mot interdit (Skip, Hint, Help, Menu, etc.)
         - pas un bouton "Check later", "Check answers in X",
           "Submit feedback" hors-contexte.
         - PAS un élément draggable-item (mot d'exo déposé dans la
           zone de réponse) — bug observé : quand un exo contenait
           le mot "check", _click_validation le trouvait dans la
           zone user-answer-container et cliquait dessus au lieu
           du vrai bouton Finish.
        """
        if not safe_is_displayed(el):
            return False
        if not self._is_element_active(el):
            return False
        # ── Exclusion structurelle : draggable-item (mot d'exo) ──
        try:
            el_cls = safe_attr(el, "class").lower()
            if "draggable-item" in el_cls:
                return False
            # Attribut data-draggable-item-id non vide = draggable
            if safe_attr(el, "data-draggable-item-id"):
                return False
        except Exception:
            pass
        # ── Exclusion : élément DANS une zone de réponse ──
        # Un mot déposé dans user-answer-container / drop-zone etc.
        # n'est jamais un bouton de validation.
        try:
            in_answer_zone = self.driver.execute_script(
                """
                var el = arguments[0];
                var p = el.parentElement;
                while (p) {
                    var dn = p.getAttribute && p.getAttribute('data-name');
                    if (dn === 'user-answer-container') return true;
                    var cls = (p.className || '').toString().toLowerCase();
                    if (cls.indexOf('ordering-drop-zone') >= 0) return true;
                    if (cls.indexOf('drop-zone') >= 0) return true;
                    if (cls.indexOf('custom-dashed-border') >= 0) return true;
                    p = p.parentElement;
                }
                return false;
                """,
                el,
            )
            if in_answer_zone:
                return False
        except (JavascriptException, StaleElementReferenceException,
                WebDriverException):
            pass
        # ── Exigence de CLIQUABILITÉ (anti faux-clic sur une PHRASE) ──
        # Un vrai bouton de validation est un <button>/<a>, ou porte
        # role=button, ou a cursor:pointer (sur lui-même ou un ancêtre
        # proche — le mot "Check" peut être dans un <span> interne au
        # bouton). Une simple phrase d'énoncé contenant un mot de
        # validation (ex: "in the last step... you should CHECK that")
        # n'est PAS cliquable → rejetée. (Même classe de bug que le
        # faux clic sur le mot "Next" dans une phrase de correction.)
        try:
            clickable = self.driver.execute_script(
                """
                var el = arguments[0];
                var node = el;
                for (var i = 0; i < 5 && node; i++) {
                    var tag = (node.tagName || '').toLowerCase();
                    if (tag === 'button' || tag === 'a') return true;
                    var role = node.getAttribute &&
                               node.getAttribute('role');
                    if (role === 'button') return true;
                    try {
                        var cur = window.getComputedStyle(node).cursor;
                        if (cur === 'pointer') return true;
                    } catch (e) {}
                    node = node.parentElement;
                }
                return false;
                """,
                el,
            )
            if not clickable:
                return False
        except (JavascriptException, StaleElementReferenceException,
                WebDriverException):
            pass
        raw = safe_text(el).strip().lower()
        if not raw:
            return False
        # Mots interdits (prioritaires)
        for bad in self._VALIDATE_FORBIDDEN_KW:
            if bad in raw:
                return False
        # Doit contenir au moins un mot de validation.
        # On tolère aussi "check answer(s)" / "check it" mais pas "checklist".
        for good in VALIDATE_WORDS:
            if good in raw:
                # Filtre anti-"checklist" / "checkbox" / "checkpoint"
                if good == "check":
                    # 'check' entouré d'autres lettres collées = pas un mot
                    import re as _re
                    if _re.search(r"\bcheck\w{2,}", raw):
                        # 'checklist', 'checkbox', 'checkpoint'…
                        return False
                return True
        return False

    def _click_validation(self) -> bool:
        """Clique Valider, puis lit le verdict affiché. Réponse fausse →
        capture + pause + notification. Couvre TOUS les chemins de
        validation (exécution normale, déblocage, secours…)."""
        baseline = self._count_red() if PAUSE_ON_ERROR else 0
        ok = self._click_validation_impl()
        if ok and PAUSE_ON_ERROR:
            self._red_baseline = baseline
            for _ in range(3):
                if self._has_feedback_context():
                    break
                time.sleep(1)
            wrong = self._detect_wrong_answer()
            if wrong:
                self._report_wrong_answer(
                    wrong, getattr(self, "_last_res", None) or {},
                    getattr(self, "_last_plan", None) or [],
                    getattr(self, "_last_stock", None) or [],
                    getattr(self, "_last_report", None) or {})
        return ok

    def _click_validation_impl(self) -> bool:
        """
        Clique le bouton Valider/Submit en toute sécurité.

        Stratégie :
         1. Parcourt les candidats par WHOLE-WORD match du texte visible
         2. Vérifie qu'ils sont actifs (pas disabled) et sans mot interdit
         3. En dernier recours : bouton primary-large ACTIF dont le texte
            contient un mot de validation (PAS le dernier primary aveugle)
        """
        # 1) Recherche ciblée par texte
        # ORDRE IMPORTANT : "Check" en DERNIER car c'est un mot qui
        # peut apparaître dans un exo (ex: "I would prefer to CHECK
        # the car"). Un vrai bouton "Check" ne sera trouvé que si
        # aucun autre label (Validate/Submit/Finish) ne match d'abord.
        seen_ids: set[int] = set()
        for lb in ("Validate", "Valider", "Submit",
                   "Finish", "Terminer", "Vérifier", "Check"):
            try:
                xp = (
                    f"//*[contains(translate(text(), "
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                    f"'abcdefghijklmnopqrstuvwxyz'), '{lb.lower()}')]"
                )
                for el in self.driver.find_elements(By.XPATH, xp):
                    try:
                        eid = id(el)
                    except Exception:
                        eid = 0
                    if eid in seen_ids:
                        continue
                    seen_ids.add(eid)
                    if self._is_valid_validate_button(el):
                        print(f"   🖱️  Validation : clic '{safe_text(el)[:40]}'")
                        self._ghost_click(el)
                        return True
            except WebDriverException:
                continue

        # 2) Fallback : bouton primary-large contenant un mot de
        #    validation, ACTIF, non-Skip. On évite le aveugle [-1].
        try:
            primaries = self.driver.find_elements(
                By.CSS_SELECTOR,
                "button[class*='primary'], a[class*='primary']",
            )
            for btn in primaries:
                if self._is_valid_validate_button(btn):
                    print(f"   🖱️  Validation (fallback primary) : "
                          f"'{safe_text(btn)[:40]}'")
                    self._ghost_click(btn)
                    return True
        except WebDriverException:
            pass

        print("   ⚠️ Aucun bouton de validation trouvé")
        return False

    # =================================================================
    #  EXÉCUTION DES ACTIONS
    # =================================================================
    def _execute_actions(
        self, actions: list[str], image_options: list,
        exercise_type: str = "",
        code_options: list[str] | None = None,
    ) -> dict:
        """
        Exécute la liste d'actions : chaque action peut être :
         - "IMG_N" → clique l'image N
         - un texte → remplit l'input si présent, sinon drag vers la
                      PREMIÈRE zone vide restante, sinon clic simple.

        Deux modes de drag gérés :
         - classique   : N actions pour N zones → chaque action a sa zone
         - accumulateur: plusieurs actions dans UNE MÊME zone, dans l'ordre
                         (typique des reorder chronologiques)

        IMPORTANT : drop_zones est re-scanné à CHAQUE drag. On cible
        toujours la 1ère zone VIDE (pas un index fixe), car le DOM est
        re-rendu par les frameworks (React/Vue) après chaque drag et les
        anciennes références deviennent stales.

        Retourne un rapport dict avec :
          total        : nombre d'actions attendues (hors Skip/Validate)
          succeeded    : nombre d'actions réussies
          failed_items : liste des textes qui ont échoué
          mode         : "drag_classique"|"drag_accumulateur"|"click"|"text_input"|"image"
          complete     : bool — tout a été placé avec succès
          zones_empty  : nombre de zones encore vides en fin d'exécution
                         (0 = idéal en mode classique)
        """
        report = {
            "total": 0,
            "succeeded": 0,
            "failed_items": [],
            "mode": "unknown",
            "complete": False,
            "zones_empty": 0,
        }
        if not actions:
            return report

        # ── ÉCOUTE AUDIO AVANT DE RÉPONDRE ────────────────────────────
        # Exos de dictée/écoute (épeler un mot, choisir l'orthographe) : les
        # tuiles/lettres sont souvent VERROUILLÉES tant que l'audio n'a pas
        # été joué. On clique le(s) haut-parleur(s) UNE fois, ici, juste
        # avant de placer. Le sélecteur data-icon="megaphone" ne matche que
        # le bouton audio de l'exercice (pas le lecteur flottant). Aucun
        # megaphone (exo sans audio) → no-op, pas d'effet de bord.
        try:
            _megs = [m for m in self.driver.find_elements(
                By.CSS_SELECTOR, 'svg[data-icon="megaphone"]')
                if safe_is_displayed(m)]
            if len(_megs) == 1:
                # Un seul audio : lecture VÉRIFIÉE (relance si le son ne
                # démarre pas → sinon les tuiles restent verrouillées)
                print("   🔊 Écoute de l'audio (1 haut-parleur(s)) "
                      "avant de répondre")
                self._play_exercise_audio()
            else:
                for _m in _megs:
                    try:
                        _mb = _m.find_element(By.XPATH,
                                              "./ancestor::button[1]")
                    except (NoSuchElementException, WebDriverException):
                        _mb = _m
                    try:
                        self._ghost_click(_mb)
                    except WebDriverException:
                        pass
                if _megs:
                    print(f"   🔊 Écoute de l'audio ({len(_megs)} "
                          f"haut-parleur(s)) avant de répondre")
                    time.sleep(1.6)
        except WebDriverException:
            pass

        text_inputs = self._get_text_inputs()
        # Exo à TUILES (stock de mots/lettres à cliquer ou glisser) : on ne
        # force JAMAIS un champ texte désactivé. Bug observé : sur un
        # reorder « un hombre y un niño », un input caché a été forcé actif
        # et le 1er mot « un » y a été TAPÉ au lieu d'être cliqué → phrase
        # fausse validée.
        is_tile_exercise = bool(code_options) and exercise_type != "text_input"
        if not text_inputs and not is_tile_exercise:
            # Exo flashcard « écris la réponse » : l'input peut être
            # disabled (activé seulement après l'audio) → on l'active puis
            # on re-scanne. No-op si aucun input désactivé (exos drag/QCM).
            if self._enable_disabled_text_input():
                text_inputs = self._get_text_inputs()
        input_idx = 0
        # Scan initial : détermine si on est en mode drag ou clic simple
        initial_zones = self._get_drop_zones()
        if not initial_zones:
            time.sleep(0.6)
            initial_zones = self._get_drop_zones()
        # IMPORTANT : si l'exercice est de type "navigation" (clic Next
        # sur une page "Well done!"), on force le mode CLIC même si des
        # drop-zones sont détectées (la zone verte de correction).
        # Sans ça, le bot tenterait de placer "Next" dans une zone vide
        # qui n'existe pas → échec en boucle.
        is_navigation = (exercise_type == "navigation")
        has_drag_mode = bool(initial_zones) and not is_navigation

        # ── DÉTECTION DU MODE ACCUMULATEUR ────────────────────────────
        # DEUX critères (un seul suffit = OR) :
        #
        # 1) HEURISTIQUE NUMÉRIQUE (générique, aucune limite codée en dur)
        #    Si on a strictement plus d'actions draggables que de zones,
        #    la (ou les) zone(s) sont forcément "accumulatrices".
        #    → couvre tout (2/1, 6/1, 100/1, 5/2, 30/10, etc.)
        #
        # 2) CLASSIFICATION GEMINI (ceinture + bretelles)
        #    Si Gemini a catégorisé l'exercice comme reorder chronologique
        #    ou reorder de phrase, on force le mode accumulateur même
        #    quand le ratio ne le suggère pas (cas rare où N == M).
        # Protection : les mots présents dans le stock (code_options)
        # ne doivent JAMAIS être filtrés par VALIDATE_WORDS, même si
        # un mot comme "check" ou "finish" y figure. Ce sont des mots
        # d'exo légitimes (ex: "I would prefer to CHECK the car now").
        _stock_keys = {norm_key(o) for o in (code_options or [])}

        def _is_action_word(s: str) -> bool:
            """
            Retourne True si `s` est une action exploitable (pas un
            Skip/Validate/vide). Un mot présent dans le stock est
            toujours considéré comme action légitime.
            """
            c = clean_ai(s)
            if not c:
                return False
            if norm_key(c) in _stock_keys:
                return True
            if is_skip_word(c) or is_validate_button_word(c):
                return False
            return True

        nb_drag_actions = sum(
            1 for a in actions
            if _is_action_word(a)
            and not clean_ai(a).startswith("IMG_")
        )
        ratio_triggers = (
            has_drag_mode
            and len(initial_zones) > 0
            and nb_drag_actions > len(initial_zones)
        )
        type_triggers = exercise_type in (
            "reorder_chronological", "reorder_sentence",
        ) and has_drag_mode and nb_drag_actions > 1

        is_accumulator_mode = ratio_triggers or type_triggers
        if is_accumulator_mode:
            reason = []
            if ratio_triggers:
                reason.append(f"{nb_drag_actions} actions / "
                              f"{len(initial_zones)} zone(s)")
            if type_triggers:
                reason.append(f"type={exercise_type}")
            print(f"   🧩 Mode accumulateur ({' + '.join(reason)}) — "
                  f"tout dans la même zone")

        # ── UN champ texte, PLUSIEURS morceaux → une seule saisie ──────
        # Ex. « Listen and rewrite the word » : l'IA répond ['una', 'cama']
        # mais il n'y a qu'UN champ. Avant, le bot tapait « una » puis
        # cherchait un bouton « cama » → réponse « Una » seulement (faux).
        if (text_inputs and not is_tile_exercise
                and len(text_inputs) == 1):
            words = [clean_ai(a) for a in actions
                     if clean_ai(a).strip()
                     and not clean_ai(a).startswith("IMG_")
                     and not is_validate_button_word(clean_ai(a))
                     and not is_skip_word(clean_ai(a))]
            if len(words) > 1:
                merged = " ".join(w.strip() for w in words)
                print(f"   🔗 Un seul champ texte : {words} → « {merged} »")
                actions = [merged]

        # ── Remplissage initial du rapport ────────────────────────────
        # Total des actions "utiles" (après filtre Skip/Validate)
        _usable_actions = [
            clean_ai(a) for a in actions
            if _is_action_word(a)
        ]
        report["total"] = len(_usable_actions)
        if has_drag_mode:
            report["mode"] = ("drag_accumulateur" if is_accumulator_mode
                              else "drag_classique")
        elif text_inputs:
            report["mode"] = "text_input"
        elif any(clean_ai(a).startswith("IMG_") for a in actions):
            report["mode"] = "image"
        else:
            report["mode"] = "click"

        for action_num, mot in enumerate(actions, 1):
            clean = clean_ai(mot)
            if not clean:
                continue
            # Skip uniquement si le mot N'EST PAS dans le stock
            # (sinon c'est un mot d'exo légitime à placer)
            if norm_key(clean) not in _stock_keys:
                if is_skip_word(clean) or is_validate_button_word(clean):
                    continue

            # ── Image ────────────────────────────────────────────────
            if clean.startswith("IMG_"):
                try:
                    idx = int(clean.split("_", 1)[1]) - 1
                except (ValueError, IndexError):
                    print(f"   ⚠️ IMG_ mal formé : {clean}")
                    report["failed_items"].append(clean)
                    continue
                if not image_options or idx < 0 or idx >= len(image_options):
                    print(f"   ⚠️ IMG_{idx+1} hors bornes")
                    report["failed_items"].append(clean)
                    continue
                img_el = image_options[idx]
                print(f"   🖼️ Clic image {idx+1}/{len(image_options)}")
                target = img_el
                for _ in range(4):
                    try:
                        parent = target.find_element(By.XPATH, "./..")
                    except (NoSuchElementException,
                            StaleElementReferenceException, WebDriverException):
                        break
                    cls = safe_attr(parent, "class")
                    tag = safe_tag(parent)
                    if (tag in ("button", "a", "label")
                            or "cursor-pointer" in cls
                            or "group" in cls):
                        target = parent
                        break
                    target = parent
                img_ok = False
                try:
                    img_ok = bool(self._ghost_click(target))
                except Exception as e:
                    print(f"   ⚠️ Clic image échoué : {str(e)[:60]}")
                if img_ok:
                    report["succeeded"] += 1
                else:
                    report["failed_items"].append(clean)
                time.sleep(0.8)
                continue

            # ── Champ texte ──────────────────────────────────────────
            # Un mot qui est une TUILE du stock se clique/glisse, il ne
            # se tape pas (sauf exo explicitement « text_input »).
            tile_word = (is_tile_exercise
                         and norm_key(clean) in _stock_keys)
            if text_inputs and input_idx < len(text_inputs) and not tile_word:
                print(f"   ⌨️  Saisie : {clean}")
                ok = False
                try:
                    ti = text_inputs[input_idx]
                    ti.clear()
                    ti.send_keys(clean)
                    ok = True
                except (StaleElementReferenceException,
                        ElementNotInteractableException, WebDriverException):
                    text_inputs = self._get_text_inputs()
                    if input_idx < len(text_inputs):
                        try:
                            text_inputs[input_idx].send_keys(clean)
                            ok = True
                        except (StaleElementReferenceException,
                                ElementNotInteractableException,
                                WebDriverException) as e:
                            print(f"   ⚠️ Text input échoué : {str(e)[:60]}")
                if ok:
                    input_idx += 1
                    self._last_typed = (getattr(self, "_last_typed", [])
                                        + [clean])
                    report["succeeded"] += 1
                    time.sleep(0.3)
                else:
                    report["failed_items"].append(clean)
                continue

            # ── Drag & Drop ou Clic ──────────────────────────────────
            # _find_element_or_paginate = _find_element_by_text + tentatives
            # de pagination si élément introuvable sur la page courante
            # (utile après que _scan_and_capture nous a laissés en dernière
            # page du stock).
            #
            # EN MODE ACCUMULATEUR OU DRAG-DROP CLASSIQUE : on essaye
            # d'abord la recherche STRICTE (button.draggable-item avec
            # data-id non vide) pour éviter de matcher des boutons
            # parasites (Skip/Exit/Menu) qui contiennent par hasard la
            # lettre/mot cherché. Le stock est constitué de
            # button.draggable-item dans les deux modes.
            element = None
            if has_drag_mode:
                element = self._find_draggable_item_by_text(clean)
                if element is not None:
                    print("   🎯 Draggable-item trouvé (recherche stricte)")
                elif self._is_draggable_already_placed(clean):
                    # La recherche stricte a échoué CAR la tuile est déjà
                    # dans une zone de réponse. On NE retombe PAS sur le
                    # fallback (qui re-glisserait l'élément réservoir en
                    # taille zéro → échec en boucle). On la compte comme
                    # placée et on passe à la suivante.
                    print(f"   ✅ '{clean}' déjà placé dans une zone → "
                          f"ignoré (compté comme placé)")
                    report["succeeded"] += 1
                    continue
            if element is None:
                element = self._find_element_or_paginate(clean)
            if element is None:
                print(f"   ❓ '{clean}' non trouvé (même après pagination)")
                report["failed_items"].append(clean)
                continue

            if has_drag_mode:
                # RESCAN à chaque drag : le DOM change après chaque action.
                # Deux stratégies selon le mode :
                #  - classique : cibler la 1ère zone VIDE restante
                #  - accumulateur : toujours la 1ère zone (elle se remplit
                #    progressivement, le framework ordonne par arrivée)
                n_empty_before = 0   # pour vérification post-drag
                if is_accumulator_mode:
                    zones = self._get_drop_zones()
                    if not zones:
                        time.sleep(0.5)
                        zones = self._get_drop_zones()
                    if not zones:
                        print(f"   ⚠️ Zone accumulatrice disparue pour "
                              f"'{clean}' (action {action_num}/"
                              f"{len(actions)}) — ignore")
                        report["failed_items"].append(clean)
                        continue
                    tgt = zones[0]
                    print(f"   ✊ Drag [{action_num}/{len(actions)}] → "
                          f"zone accumulatrice : {clean}")
                else:
                    empty_zones = self._get_empty_drop_zones()

                    # Si plus aucune zone vide mais drop_zones existe encore,
                    # c'est probablement un rendu intermédiaire : on attend + rescan.
                    if not empty_zones:
                        time.sleep(0.5)
                        empty_zones = self._get_empty_drop_zones()

                    if not empty_zones:
                        # Toutes les zones sont remplies mais il reste des actions :
                        # soit le site a auto-complété, soit on a trop d'actions.
                        print(f"   ⚠️ Plus de zone vide pour '{clean}' "
                              f"(action {action_num}/{len(actions)}) — ignore")
                        report["failed_items"].append(clean)
                        continue

                    tgt = empty_zones[0]
                    n_empty_before = len(empty_zones)
                    print(f"   ✊ Drag [{action_num}/{len(actions)}] → "
                          f"1ère zone vide (reste {n_empty_before}) : {clean}")

                # Placement au CLIC pour les widgets qui ignorent le drag
                # synthétique : SortableJS (data-sortable-item) ET dragula
                # des anagrammes de lettres (.item.sorting-letter[data-
                # answer-id]). Dans les deux cas, cliquer la tuile la place
                # dans la case suivante.
                _cls_el = safe_attr(element, "class") or ""
                is_sortable = bool(
                    safe_attr(element, "data-sortable-item")
                    or (safe_attr(element, "data-answer-id")
                        and "sorting-letter" in _cls_el))
                if is_sortable:
                    # SortableJS (« écoute + choisis l'orthographe ») : le
                    # widget place le mot au CLIC (le drag synthétique est
                    # ignoré). L'audio a déjà été joué en début d'exécution.
                    # On clique le mot, puis on vérifie qu'une zone vide s'est
                    # bien remplie.
                    print(f"   🖱️  Clic (sortable) : {clean}")
                    try:
                        self._ghost_click(element)
                    except WebDriverException:
                        pass
                    time.sleep(0.9)
                    n_after = len(self._get_empty_drop_zones())
                    success = (n_after < n_empty_before
                               if n_empty_before > 0 else True)
                    # Mot pas placé : souvent l'audio n'a pas vraiment été
                    # joué (mots verrouillés) → on le rejoue (lecture
                    # vérifiée), puis vrai clic souris, puis clic JS.
                    def _placed() -> bool:
                        # Le mot est-il déjà dans la case réponse ?
                        try:
                            return bool(self.driver.execute_script(
                                "const t = arguments[0];"
                                "const ok = [...document.querySelectorAll("
                                "'[data-user-answer-list]')].some(z =>"
                                " (z.innerText || '').trim() === t);"
                                "return ok;", clean))
                        except WebDriverException:
                            return False
                    if not success and _placed():
                        success = True
                    for how in ("souris", "JS"):
                        if success or _placed():
                            success = True
                            break
                        if how == "souris":
                            print("   🔁 Mot non placé → réécoute de "
                                  "l'audio puis nouveau clic")
                            self._play_exercise_audio()
                        try:
                            if how == "souris":
                                self._cdp_click(element)
                            else:
                                self.driver.execute_script(
                                    "arguments[0].click();", element)
                        except WebDriverException:
                            pass
                        time.sleep(0.9)
                        n_after = len(self._get_empty_drop_zones())
                        success = n_after < n_empty_before
                        if success:
                            print(f"   ✅ Placé au clic {how}")
                elif is_accumulator_mode:
                    # Drag spécialisé : dépose après le dernier token
                    success = self._ghost_drag_accumulator(element, tgt)
                else:
                    success = self._ghost_drag(element, tgt)
                # Attente plus longue pour laisser le framework re-rendre
                # le DOM après drag (React/Vue peuvent prendre ~700-900ms).
                time.sleep(0.9)

                # ── VÉRIFICATION POST-DRAG (mode classique uniquement) ──
                # Même si _ghost_drag retourne True, le framework peut
                # avoir ignoré les événements (ex: timing JS, listeners
                # mal attachés). On valide le drop en vérifiant que le
                # nombre de zones vides a bien DIMINUÉ.
                #
                # En mode accumulateur ce check ne s'applique pas :
                # la zone unique reste visuellement "remplie" mais
                # accueille toujours l'élément suivant.
                if (success and not is_accumulator_mode and not is_sortable
                        and n_empty_before > 0):
                    n_empty_after = len(self._get_empty_drop_zones())
                    if n_empty_after >= n_empty_before:
                        # Le drop n'a pas été pris visuellement : retry
                        print(f"   🔁 Drop non confirmé "
                              f"({n_empty_before}→{n_empty_after} vides), "
                              f"retry...")
                        time.sleep(0.6)

                        # ── a) Vérifier si element est toujours valide ──
                        retry_element = element
                        element_stale = False
                        try:
                            _ = element.is_displayed()
                        except (StaleElementReferenceException,
                                WebDriverException):
                            element_stale = True

                        # ── b) Rescan via texte si stale ──
                        if element_stale:
                            print(f"   🔍 Élément '{clean}' stale → "
                                  f"rescan via texte...")
                            # Retirer l'ancienne ref du cache sinon
                            # _find_element_by_text l'ignorera
                            try:
                                if element in self.used_elements_cache:
                                    self.used_elements_cache.remove(element)
                            except (ValueError,
                                    StaleElementReferenceException,
                                    WebDriverException):
                                pass
                            retry_element = self._find_element_by_text(
                                clean,
                            )
                            if retry_element is None:
                                print(f"   ⚠️ '{clean}' introuvable au "
                                      f"rescan → abandon retry")
                                success = False

                        # ── c) Tenter le retry avec logs explicites ──
                        if retry_element is not None:
                            try:
                                retry_zones = self._get_empty_drop_zones()
                                if not retry_zones:
                                    print("   ℹ️ Plus de zone vide à "
                                          "l'instant T du retry "
                                          "— drop peut-être pris "
                                          "entretemps")
                                else:
                                    retry_ok = self._ghost_drag(
                                        retry_element, retry_zones[0],
                                    )
                                    print(f"   🔁 Retry _ghost_drag → "
                                          f"{'OK' if retry_ok else 'ÉCHEC'}")
                                    time.sleep(1.1)
                                    final_empty = len(
                                        self._get_empty_drop_zones()
                                    )
                                    if final_empty >= n_empty_before:
                                        print(f"   ⚠️ Retry infructueux "
                                              f"pour '{clean}' — le "
                                              f"framework ignore le drop")
                                        success = False
                                    else:
                                        print(f"   ✅ Retry réussi : "
                                              f"{n_empty_before}→"
                                              f"{final_empty} vides")
                                        # Le retry a marché : on garde
                                        # retry_element pour le cache
                                        element = retry_element
                            except (StaleElementReferenceException,
                                    WebDriverException) as e:
                                print(f"   ⚠️ Retry exception : "
                                      f"{type(e).__name__} : "
                                      f"{str(e)[:60]}")
                                success = False

                # Mémorise l'élément comme utilisé (même en cas d'échec
                # silencieux : évite de le retrouver au tour suivant)
                try:
                    if element not in self.used_elements_cache:
                        self.used_elements_cache.append(element)
                except (StaleElementReferenceException, WebDriverException):
                    pass

                if success:
                    report["succeeded"] += 1
                else:
                    print(f"   ⚠️ Drag de '{clean}' a échoué "
                          f"(toutes les stratégies)")
                    report["failed_items"].append(clean)
            else:
                print(f"   ⚡ Clic : {clean}")
                click_ok = False
                try:
                    click_ok = bool(self._ghost_click(element))
                except Exception as e:
                    print(f"   ⚠️ Clic échoué : {str(e)[:60]}")
                if click_ok:
                    report["succeeded"] += 1
                else:
                    report["failed_items"].append(clean)
                time.sleep(0.6)

        # ── Finalisation du rapport ──────────────────────────────────
        # En mode classique avec drop zones : la complétude se mesure
        # aux zones vides restantes ; en mode accumulateur ou clic :
        # au ratio succeeded/total.
        try:
            report["zones_empty"] = (
                len(self._get_empty_drop_zones()) if has_drag_mode else 0
            )
        except (WebDriverException, Exception):
            report["zones_empty"] = 0

        if (report["mode"] == "drag_classique"
                and has_drag_mode and not is_accumulator_mode):
            # Classique : exigence stricte = 0 zone vide restante
            report["complete"] = (
                report["zones_empty"] == 0
                and report["succeeded"] == report["total"]
            )
        else:
            # Autres modes : complet si tout succeeded
            report["complete"] = (
                report["total"] > 0
                and report["succeeded"] == report["total"]
            )

        if not report["complete"]:
            print(f"   📊 Exécution incomplète : "
                  f"{report['succeeded']}/{report['total']} OK, "
                  f"{len(report['failed_items'])} échec(s), "
                  f"{report['zones_empty']} zone(s) vide(s)")
        return report

    # =================================================================
    #  BOUCLE PRINCIPALE
    # =================================================================
    def run(self) -> None:
        print(f"\n🛡️ BOT V5.1 [{self._get_status()}]\n")
        consecutive_none = 0
        cycle_num = 0

        while True:
            # ── PAUSE / REPRISE (interface graphique) ─────────────────
            # Ouvert par défaut en mode terminal → ne bloque jamais.
            # En pause, le bot termine le cycle courant puis s'arrête ICI
            # jusqu'à ce que l'utilisateur clique « Reprendre ».
            RUN_GATE.wait()
            if STOP_REQUESTED.is_set():
                if not hasattr(self, "_stop_seen_at"):
                    self._stop_seen_at = time.time()
                    if self._exo_in_progress:
                        print("⏱️  Temps écoulé → je termine l'exercice "
                              "en cours avant de m'arrêter…")
                late = time.time() - self._stop_seen_at > STOP_GRACE_S
                if not self._exo_in_progress or late:
                    print(f"🏁 Session terminée : {self.stats.get('exos', 0)} "
                          f"exercice(s) fait(s).")
                    return
            try:
                cycle_num += 1
                # --- Auto-refresh périodique ---
                if self._check_auto_refresh():
                    consecutive_none = 0
                    continue

                self._scroll_bottom()

                # --- Capture déterministe du transcript (PRIORITAIRE) ---
                # Avant les réflexes / Gemini : si la page d'intro affiche
                # "View the transcript", on l'ouvre et on mémorise le texte
                # AVANT toute navigation. Ne dépend pas de l'IA.
                if self._capture_transcript_if_present():
                    print(f"   ↪ Cycle {cycle_num} : transcript capturé")
                    consecutive_none = 0
                    continue

                # --- Récolte des textes de la page (1 fois par page) ---
                # AVANT les réflexes / carrousel / Next, qui peuvent quitter
                # la page : dialogue visible + transcript éventuel.
                try:
                    _psig = self._get_page_text()[:1500]
                    if _psig != getattr(self, "_harvest_page_sig", None):
                        self._harvest_page_sig = _psig
                        self._harvest_page_texts()
                except Exception:  # noqa: BLE001
                    pass

                # --- Réflexes ---
                if self._check_reflexes():
                    print(f"   ↪ Cycle {cycle_num} : réflexe déclenché")
                    self._reset_state()
                    consecutive_none = 0
                    time.sleep(2.5)
                    continue

                # --- Dashboard / Carrousel ---
                if self._pass_dashboard():
                    print(f"   ↪ Cycle {cycle_num} : dashboard traité")
                    self._reset_state()
                    consecutive_none = 0
                    continue
                if self._find_carousel_arrow() or (
                        self._find_stock_pagination_arrow()
                        and self._is_card_carousel()):
                    print(f"   ↪ Cycle {cycle_num} : carrousel")
                    self._run_carousel()
                    self._reset_state()
                    consecutive_none = 0
                    continue

                # --- Scan + capture (avec pagination du stock si flèche) ---
                code_options, img, image_options = self._scan_and_capture()

                # --- GARDE-FOU POST-SCAN : page de correction ────────
                # Sur certains exos, la page de correction affiche
                # soit les éléments de la solution en boutons/spans
                # qui ressemblent au stock mais ne sont PLUS cliquables,
                # soit juste une phrase corrigée sans aucun stock.
                # Sans ce garde-fou, Gemini serait appelé inutilement
                # et pourrait boucler sur des clics parasites.
                #
                # Scénarios gérés (tous ont feedback visible) :
                #  A) code_options non vide → stock fantôme (anagramme
                #     CRAZY, lettres en bulle feedback)
                #  B) code_options vide → page "No response?",
                #     "Almost!" ou "No worries!" qui affiche juste la
                #     phrase corrigée en texte, pas de stock à cliquer.
                #
                # Dans les deux cas, si aucun exo EN COURS n'est
                # détecté (draggable actif, radio non coché, input
                # vide), on tente directement le bouton de progression
                # sans appeler Gemini.
                fb_context = self._has_feedback_context()
                pending_exo = (self._has_pending_exercise()
                               if fb_context else False)
                # Réarmement du refresh préventif : dès qu'on n'est PAS
                # sur une page de correction "propre" (feedback affiché +
                # aucun exo en cours), on réautorise un refresh pour la
                # prochaine page de correction.
                if not fb_context or pending_exo:
                    self._correction_refreshed = False

                if fb_context:
                    if pending_exo:
                        # Exo en cours (draggable actif, QCM non
                        # répondu, input vide). Note : la présence
                        # d'une drop-zone seule ne suffit PAS à
                        # déclarer "exo en cours" car la drop-zone
                        # reste affichée sur la page de correction
                        # pour montrer les réponses du user.
                        pass
                    else:
                        ctx = ("stock fantôme" if code_options
                               else "sans stock")
                        print(f"   ↪ Cycle {cycle_num} : page de correction "
                              f"({ctx}) → saut de Gemini")
                        # REFRESH PRÉVENTIF — une fois par page de
                        # correction. Site affiche une page d'erreur
                        # "activité suspecte" après trop d'actions sans
                        # rechargement ; le seul moyen de la lever est un
                        # refresh. On rafraîchit donc entre CHAQUE exercice
                        # (ici, sur la correction → l'exo est déjà validé,
                        # rien à casser) pour garder le compteur du site au
                        # plus bas. Le drapeau évite de boucler si recharger
                        # la correction la réaffiche.
                        # ── GARDE-FOU : PAS de refresh sur un écran audio /
                        #    orthographe (SortableJS). Ces exos sont classés
                        #    à tort « page de correction » (stock=0) ; or un
                        #    refresh y réinitialise l'audio écouté et casse le
                        #    placement. On saute le refresh → la suite
                        #    (analyse) traite l'exercice normalement. Le
                        #    refresh préventif reste actif sur les VRAIES
                        #    pages de correction (sans sortable), entre exos.
                        sortable_here = False
                        try:
                            sortable_here = bool(self.driver.find_elements(
                                By.CSS_SELECTOR, '[data-sortable-item]'))
                        except WebDriverException:
                            pass
                        if sortable_here:
                            print("   🛑 Écran audio/orthographe → PAS de "
                                  "refresh (préserve l'audio écouté)")
                        if not self._correction_refreshed and not sortable_here:
                            print("   🔄 Refresh préventif (page de "
                                  "correction)")
                            try:
                                self.driver.refresh()
                                time.sleep(5)
                            except WebDriverException as e:
                                print(f"   ⚠️ Refresh échoué : {e}")
                            self._correction_refreshed = True
                            self._reset_state(full=True)
                            consecutive_none = 0
                            continue
                        # Déjà rafraîchi cette page → on avance.
                        # Tenter le clic Next/Finish immédiatement
                        if self._click_progression_button():
                            self._correction_refreshed = False
                            self._reset_state()
                            consecutive_none = 0
                            continue
                        # Aucun bouton de progression trouvé.
                        # Tenter Validate au cas où l'exo n'est pas
                        # encore validé (feedback partiel ?)
                        if self._click_validation():
                            print("   ↪ Validate cliqué (fallback "
                                  "post-scan)")
                            time.sleep(3)
                            continue
                        # Ni progression ni validation trouvées. Plutôt
                        # que de boucler à vide (cas observé : QCM mal
                        # classé en "page de correction", ou bouton Next
                        # non reconnu par l'heuristique), on LAISSE Gemini
                        # analyser la page ci-dessous : il saura soit la
                        # résoudre (vrai QCM en cours), soit renvoyer la
                        # bonne action de navigation. Petite pause pour
                        # laisser un éventuel bouton finir de charger.
                        print("   ↪ Aucun bouton trouvé → on laisse "
                              "Gemini analyser la page")
                        time.sleep(1.5)
                        # PAS de continue : on tombe dans ask_gemini.

                # ── GARDE-FOU : écran de PRONONCIATION ───────────────────
                # Il a pu s'afficher après le passage des réflexes (page
                # pas encore chargée). L'IA le prenait pour un choix
                # d'image (« Baño ») puis le bot cliquait « Skip ». On
                # laisse le réflexe prononciation le traiter au cycle
                # suivant, sans appel IA.
                if SPEAK_MODE and self._is_speaking_screen():
                    self._speak_guard_hits = getattr(
                        self, "_speak_guard_hits", 0) + 1
                    if self._speak_guard_hits <= 3:   # anti-boucle
                        print(f"   ↪ Cycle {cycle_num} : écran de "
                              f"prononciation → module voix (pas d'IA)")
                        time.sleep(1)
                        continue
                else:
                    self._speak_guard_hits = 0

                # ── GARDE-FOU : exo de glisser-déposer déjà fait ──────────
                # Stock vide (plus rien à glisser) + zone(s) de dépôt + pas
                # de champ texte → la réponse est déjà placée/validée (cas
                # observé : écoute + orthographe, correction affichée sans
                # texte « Correct » et zone qui paraît encore vide). Si un
                # bouton Next actif est là, on avance SANS appeler Gemini
                # (avant : boucle infinie de 2 appels IA par cycle).
                if not code_options:
                    try:
                        drag_screen = (bool(self._get_drop_zones())
                                       and not self._get_text_inputs())
                    except WebDriverException:
                        drag_screen = False
                    if drag_screen and self._click_progression_button():
                        print(f"   ↪ Cycle {cycle_num} : exo déjà fait "
                              f"(rien à placer) → Next, sans IA")
                        self.memory_context = ""
                        self._reset_state()
                        consecutive_none = 0
                        continue

                res = self.ask_gemini(
                    img, self.memory_context, code_options, image_options,
                )

                # --- Échec d'analyse ---
                if not res:
                    consecutive_none += 1
                    print(f"   ... Scan ... ({consecutive_none}/{MAX_NONE_RUNS})")
                    if consecutive_none >= MAX_NONE_RUNS:
                        print(f"   ⚠️ {MAX_NONE_RUNS} échecs → déblocage")
                        consecutive_none = 0
                        self._unblock()
                    time.sleep(2)
                    continue

                consecutive_none = 0
                self._exo_in_progress = True

                # --- Cas spéciaux ---
                if res.get("transcript_action"):
                    # Filet de sécurité : la capture déterministe en début
                    # de cycle gère normalement le transcript. Si on arrive
                    # ici, c'est que Gemini a (re)classé la page en
                    # transcript — on tente la capture via la même méthode.
                    captured = self._capture_transcript_if_present()
                    if not captured:
                        # Pas de bouton / déjà capturé : on tente d'avancer
                        nxt = (self._find_element_by_text("Next")
                               or self._find_element_by_text("Suivant"))
                        if nxt and safe_is_displayed(nxt):
                            self._ghost_click(nxt)
                            time.sleep(2)
                    continue


                if res.get("special") == "CAROUSEL":
                    self._run_carousel()
                    self._reset_state()
                    continue

                # --- Exécution des actions ---
                actions = res.get("actions") or []
                if actions:
                    # Reset du cache d'éléments avant chaque nouveau cycle
                    self.used_elements_cache = []

                    # Éclatement des lettres collées si on est sur un anagramme
                    letters = [o for o in code_options if len(o) == 1]
                    is_letter_anagram = (
                        len(code_options) > 0
                        and len(letters) / max(len(code_options), 1) > 0.8
                    )
                    expanded = []
                    for item in actions:
                        if is_letter_anagram and len(item) > 1:
                            expanded.extend(list(item))
                        else:
                            expanded.append(item)

                    _etype = str(res.get("exercise_type") or res.get("type") or "")
                    _ido = (self._id_order(expanded, [
                        o for o in code_options if str(o).strip()])
                        if len(expanded) >= 2 and (
                        is_letter_anagram or _etype.startswith("reorder")
                        or _etype == "sorting"
                        or (_etype == "drag_drop" and sorted(map(str, expanded))
                            == sorted(str(o) for o in code_options))) else None)
                    if _ido and _ido != [re.sub(r"\s+", " ", str(a)).strip()
                                         for a in expanded]:
                        print(f"   🔢 Ordre corrigé d'après les tuiles du "
                              f"site : {_ido} (l'IA proposait {expanded})")
                        expanded = _ido
                    print(f"\n   📋 PLAN FINAL : {expanded}\n")
                    # Plan = juste quitter la page (Next/Continue…) : on
                    # récolte d'abord les textes et le transcript éventuels.
                    if expanded and all(
                            str(a).strip().lower() in (
                                "next", "suivant", "continue", "continuer",
                                "start", "commencer", "finish", "terminer")
                            for a in expanded):
                        self._harvest_page_texts()
                    self._last_res = res
                    self._last_plan = expanded
                    self._last_stock = code_options
                    self._last_report = {}
                    self._last_typed = []
                    report = self._execute_actions(
                        expanded, image_options,
                        exercise_type=res.get("exercise_type", ""),
                        code_options=code_options,
                    )
                    time.sleep(0.5)

                    # ── DÉCISION DE VALIDATION BASÉE SUR LE RAPPORT ──
                    # On ne valide QUE si l'exécution est complète.
                    # Sinon on laisse le cycle suivant relire l'état —
                    # cliquer Validate sur un exo incomplet provoque
                    # un "Incorrect" côté site, le Next cliqué ensuite
                    # fait sauter l'exercice = le fameux "Skip fantôme".
                    if report.get("complete"):
                        print(f"   ✅ Exécution complète "
                              f"({report['succeeded']}/{report['total']}) "
                              f"→ validation")
                        self._last_report = report
                        self._click_validation()
                        time.sleep(1.8)

                        # ── CLIC POST-VALIDATION : Finish / Next / Move on ──
                        # Une fois la validation faite, on attend que le
                        # feedback s'affiche (Correct!, Almost!, etc.)
                        # puis on clique le bouton de progression dans
                        # l'ordre de priorité :
                        #   1. "Move on to the next" (le plus explicite)
                        #   2. "Next activity" / "Next"
                        #   3. "Suivant" / "Continue" / "Continuer"
                        #   4. "Finish" / "Terminer" (fin de module)
                        #
                        # On attend le feedback jusqu'à 3s (retry 3× à
                        # 1s d'intervalle) — certains sites mettent du
                        # temps à afficher Correct/Incorrect.
                        progressed = False
                        for wait_attempt in range(3):
                            if self._has_feedback_context():
                                break
                            time.sleep(1)


                        # Ordre de priorité des boutons à chercher
                        progression_targets = [
                            "Move on to the next",
                            "Next activity",
                            "Next",
                            "Suivant",
                            "Continue",
                            "Continuer",
                            "Finish",
                            "Terminer",
                            "See results",
                            "Consulter les résultats",
                        ]
                        for lb in progression_targets:
                            if progressed:
                                break
                            try:
                                lo = lb.lower()
                                xp = (
                                    f"//button[contains(translate(., "
                                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                                    f"'abcdefghijklmnopqrstuvwxyz'), "
                                    f"'{lo}')] | "
                                    f"//a[contains(translate(., "
                                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                                    f"'abcdefghijklmnopqrstuvwxyz'), "
                                    f"'{lo}')] | "
                                    f"//div[@role='button' and "
                                    f"contains(translate(., "
                                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                                    f"'abcdefghijklmnopqrstuvwxyz'), "
                                    f"'{lo}')]"
                                )
                                for el in self.driver.find_elements(
                                    By.XPATH, xp,
                                ):
                                    if not safe_is_displayed(el):
                                        continue
                                    if not self._is_element_active(el):
                                        continue
                                    el_txt = safe_text(el)
                                    if is_skip_word(el_txt):
                                        continue
                                    # Anti-match trop large : on exige
                                    # que le texte du bouton contienne
                                    # réellement le mot-clé (pas juste
                                    # un sous-élément)
                                    el_txt_lo = el_txt.lower()
                                    if lo not in el_txt_lo:
                                        continue
                                    # Rejette les boutons parasites
                                    # (Zoom, Exit, Close, Help, Menu)
                                    PARASITES = {"zoom", "exit", "close",
                                                 "fermer", "help", "aide",
                                                 "info", "menu", "back"}
                                    if (any(p in el_txt_lo for p in PARASITES)
                                            and lo not in PARASITES):
                                        continue
                                    print(f"   ▶️  Post-validation : "
                                          f"clic '{lb}'")
                                    self._ghost_click(el)
                                    self.memory_context = ""
                                    progressed = True
                                    # 5s : temps de charger l'exo suivant
                                    time.sleep(5)
                                    break
                            except WebDriverException:
                                continue

                        if not progressed:
                            # La correction (et son bouton Next) met un
                            # instant à s'afficher. On ATTEND et on réessaie
                            # quelques fois AVANT de rendre la main. Sinon le
                            # cycle suivant ne reconnaît pas la correction et
                            # RE-ANALYSE l'écran = appel Gemini gaspillé (bug
                            # observé sur les exos audio/orthographe).
                            for _ in range(4):
                                time.sleep(1.5)
                                if self._click_progression_button():
                                    print("   ▶️  Progression trouvée après "
                                          "attente → exo suivant (pas de "
                                          "ré-analyse)")
                                    self.memory_context = ""
                                    self._reset_state()
                                    consecutive_none = 0
                                    progressed = True
                                    time.sleep(3)
                                    break
                        if not progressed:
                            print("   ℹ️  Aucun bouton de progression "
                                  "trouvé — prochain cycle s'en chargera")
                    else:
                        # ── GARDE-FOU COÛT : exo « écoute + orthographe »
                        #    (SortableJS) non plaçable (drag ET clic ignorés).
                        #    Re-analyser rappellerait Gemini en boucle =
                        #    argent brûlé. On clique « Skip » pour passer au
                        #    mot suivant, et on mémorise que ce type est non
                        #    résoluble → les suivants seront skippés sans IA.
                        sortable_here = False
                        audio_exo = False
                        try:
                            sortable_here = bool(self.driver.find_elements(
                                By.CSS_SELECTOR, '[data-sortable-item]'))
                            # Exo audio (dictée/écoute) : bouton haut-parleur
                            # de l'exercice présent. Ces exos, si le bot ne
                            # peut pas les placer (audio non entendu, ordre
                            # des lettres incertain), doivent être passés, pas
                            # bouclés.
                            audio_exo = sortable_here or bool(
                                self.driver.find_elements(
                                    By.CSS_SELECTOR,
                                    'svg[data-icon="megaphone"]'))
                        except WebDriverException:
                            pass
                        if (audio_exo and not (SPEAK_MODE
                                               and self._is_speaking_screen())
                                and self._click_skip_button()):
                            if sortable_here:
                                self._sortable_unsolvable = True
                            self.memory_context = ""
                            consecutive_none = 0
                            if PAUSE_ON_ERROR and not getattr(
                                    self, "_skip_reported", False):
                                self._skip_reported = True
                                self._report_wrong_answer(
                                    "Exercice audio non résolu → passé "
                                    "avec Skip", res, expanded,
                                    code_options, report)
                            print("   ⏭️  Exo audio non plaçable → « Skip » "
                                  "(pas de boucle, pas d'appels IA en série)")
                            time.sleep(3)
                        elif report.get("zones_empty", 0) == 0:
                            # Toutes les zones sont REMPLIES mais l'exécution
                            # est comptée incomplète (0 candidat : le mot est
                            # déjà placé + validé, écran de correction non
                            # détecté comme tel). L'exo est en fait TERMINÉ →
                            # on tente un Next/Finish au lieu de rappeler
                            # Gemini en boucle (c'était la boucle observée sur
                            # les exos orthographe après validation).
                            if self._click_progression_button():
                                print("   ➡️  Exo déjà rempli/terminé → "
                                      "progression (fin de boucle)")
                                self.memory_context = ""
                                self._reset_state()
                                consecutive_none = 0
                            else:
                                consecutive_none += 1
                                print("   ⏸️  Rien à placer, pas de Next "
                                      "trouvé → relecture")
                                if consecutive_none >= MAX_NONE_RUNS:
                                    consecutive_none = 0
                                    self._unblock()
                                else:
                                    time.sleep(2)
                        else:
                            # Exécution incomplète avec des zones ENCORE VIDES
                            # : on NE VALIDE PAS, on NE CLIQUE PAS Next. On
                            # relit l'état au prochain cycle et on retentera.
                            # Compté comme échec partiel (évite boucle infinie).
                            print(f"   ⚠️ Exécution INCOMPLÈTE "
                                  f"({report['succeeded']}/{report['total']}, "
                                  f"échecs={report['failed_items']}, "
                                  f"zones vides={report['zones_empty']})")
                            if self._click_progression_button():
                                print("   ➡️  Bouton Next présent → exo déjà "
                                      "terminé, on avance")
                                self.memory_context = ""
                                self._reset_state()
                                consecutive_none = 0
                                continue
                            print("   ⏸️  Pas de validation → relecture au "
                                  "prochain cycle")
                            consecutive_none += 1
                            if consecutive_none >= MAX_NONE_RUNS:
                                print(f"   ⚠️ {MAX_NONE_RUNS} exécutions "
                                      f"incomplètes → déblocage")
                                consecutive_none = 0
                                self._unblock()
                            else:
                                time.sleep(2)
                else:
                    # Aucune action produite : Gemini a répondu mais le
                    # validateur a tout filtré, ou retour vide. On le
                    # traite explicitement (avant : fall-through silencieux).
                    print("   ⚠️ Aucune action exploitable retournée "
                          "(Gemini vide ou tout filtré)")
                    # ── DIAGNOSTIC : afficher ce que Gemini a renvoyé ──
                    # Pour comprendre pourquoi on arrive ici : type
                    # détecté, nombre d'éléments initiaux, etc.
                    print(f"   🩺 Diag : type='{res.get('exercise_type') or res.get('type', '?')}', "
                          f"raw actions={res.get('actions_raw', 'N/A')}, "
                          f"stock={len(code_options)}, "
                          f"zones={len(self._get_drop_zones())}, "
                          f"inputs={len(self._get_text_inputs())}")
                    consecutive_none += 1
                    if consecutive_none >= MAX_NONE_RUNS:
                        print(f"   ⚠️ {MAX_NONE_RUNS} cycles sans action "
                              f"→ déblocage")
                        consecutive_none = 0
                        self._unblock()
                    else:
                        time.sleep(1.5)

            except KeyboardInterrupt:
                print("\n🛑 Arrêt.")
                break
            except Exception as e:
                if not self._session_alive():
                    print("🛑 Chrome fermé ou déconnecté → arrêt du bot.")
                    notify_phone("Chrome a été fermé ou s'est déconnecté : "
                                 "le bot est arrêté.",
                                 title="Bot GlobalExam - arrete")
                    raise RuntimeError(
                        "Chrome a été fermé : relance le bot.") from e
                print(f"   Erreur boucle : {str(e)[:100]}")
                time.sleep(1)

    def _session_alive(self) -> bool:
        """False si la session Chrome est perdue (navigateur fermé).
        Si seul l'onglet actif a été fermé, bascule sur un autre onglet."""
        try:
            handles = self.driver.window_handles
        except Exception:
            return False
        if not handles:
            return False
        try:
            _ = self.driver.current_url
            return True
        except Exception:
            try:
                self.driver.switch_to.window(handles[-1])
                return True
            except Exception:
                return False

    # -----------------------------------------------------------------
    #  Déblocage automatique après trop d'échecs consécutifs
    # -----------------------------------------------------------------
    def _unblock(self) -> None:
        """Tente : validation → Next/Suivant/Continue → refresh."""
        # 1) Click Validate d'abord
        if self._click_validation():
            print("   🔄 Déblocage → validation cliquée")
            self._reset_state()
            time.sleep(1.5)
            return

        # 2) Tente un Next/Suivant/Continue (jamais Skip, jamais inactif)
        for lb in ("Next", "Suivant", "Continue"):
            try:
                els = self.driver.find_elements(
                    By.XPATH, f"//*[contains(text(), '{lb}')]",
                )
                for el in els:
                    if not safe_is_displayed(el):
                        continue
                    if not self._is_element_active(el):
                        continue
                    if is_skip_word(safe_text(el)):
                        continue
                    print(f"   🔄 Déblocage → clic '{lb}'")
                    self._ghost_click(el)
                    self._reset_state()
                    time.sleep(1.5)
                    return
            except WebDriverException:
                continue

        # 3) Dernier recours : refresh
        print("   🔄 Déblocage → refresh page")
        try:
            self.driver.refresh()
        except WebDriverException:
            pass
        time.sleep(5)
        self._reset_state(full=True)


# =====================================================================
# POINT D'ENTRÉE
# =====================================================================
if __name__ == "__main__":

    LANGUES: dict[str, tuple[str, str]] = {
        "1": ("es", "Espagnol 🇪🇸"),
        "2": ("en", "Anglais 🇬🇧"),
        "3": ("fr", "Français 🇫🇷"),
        "4": ("de", "Allemand 🇩🇪"),
        "5": ("pt", "Portugais 🇵🇹"),
        "6": ("it", "Italien 🇮🇹"),
    }

    print("\n" + "═" * 45)
    print("   🌍  SÉLECTION DE LA LANGUE D'APPRENTISSAGE")
    print("═" * 45)
    for k, (code, nom) in LANGUES.items():
        print(f"   {k}. {nom}  [{code}]")
    print("═" * 45)

    langue_choisie: str | None = None
    while langue_choisie is None:
        try:
            choix = input("   → Ton choix (1-6) : ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n🛑 Annulé.")
            sys.exit(0)
        if choix in LANGUES:
            langue_choisie, nom_langue = LANGUES[choix]
            print(f"\n   ✅ Langue sélectionnée : {nom_langue}\n")
        else:
            print("   ⚠️ Choix invalide, entre un chiffre entre 1 et 6.")

    bot = HybridBot(langue=langue_choisie)
    bot.run()