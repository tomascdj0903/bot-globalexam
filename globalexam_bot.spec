# -*- mode: python ; coding: utf-8 -*-
# =====================================================================
#  Spec PyInstaller — Bot GlobalExam (.exe autonome, un seul fichier)
# =====================================================================
#
#  Construit "dist/BotGlobalExam.exe" à partir de :
#     • launcher.py  (la fenêtre d'accueil = point d'entrée)
#     • bot.py       (le moteur, importé par le launcher)
#
#  Les bibliothèques wordfreq / spellchecker / google.generativeai sont
#  chargées DYNAMIQUEMENT par le bot : PyInstaller ne les détecte pas
#  automatiquement. On les force ici avec collect_all (code + données).
#
#  Build (sur Windows, dans le dossier contenant ces fichiers) :
#       pyinstaller globalexam_bot.spec
# =====================================================================

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []

# Paquets à embarquer en entier (leurs données comprises)
for pkg in ("wordfreq", "spellchecker", "google.generativeai", "selenium"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:   # noqa: BLE001
        print(f"[spec] collect_all({pkg}) a échoué : {e}")

# Le moteur, importé dynamiquement par le launcher
hiddenimports += ["bot", "cle_fournie", "google.oauth2.credentials", "google.auth.credentials"]


a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="BotGlobalExam",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # console=False : pas de fenêtre noire de terminal (appli graphique).
    # Pour DÉBOGUER un build qui plante au lancement, mettre True afin de
    # voir les erreurs, puis repasser à False pour la version finale.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="icone.ico",     # logo (symbole infini)
)
