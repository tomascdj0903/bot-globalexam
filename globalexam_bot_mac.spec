# -*- mode: python ; coding: utf-8 -*-
# =====================================================================
#  Spec PyInstaller — Bot GlobalExam — macOS (.app double-cliquable)
# =====================================================================
#
#  ⚠️ À CONSTRUIRE SUR UN MAC. PyInstaller ne peut PAS fabriquer une app
#     Mac depuis Windows. Ce fichier est inutile sous Windows.
#
#  Build (sur le Mac, dans le dossier des fichiers) :
#       python3 -m PyInstaller --clean globalexam_bot_mac.spec
#
#  Résultat : dist/BotGlobalExam.app  (qu'on double-clique).
#
#  IMPORTANT (macOS) :
#   • L'app n'est PAS signée → au 1er lancement, macOS la bloque.
#     Solution : clic DROIT sur l'app → « Ouvrir » → « Ouvrir » (à faire
#     une seule fois). Voir GUIDE_MAC.md.
#   • L'architecture suit le Mac de build : un build sur Mac Apple Silicon
#     (M1/M2/M3) cible Apple Silicon ; un build sur Mac Intel cible Intel.
#     Construire de préférence sur le même type de Mac que la cible.
#   • Google Chrome doit être installé sur la machine qui l'utilise.
# =====================================================================

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []

for pkg in ("wordfreq", "spellchecker", "google.generativeai", "selenium",
            "webdriver_manager", "certifi"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:   # noqa: BLE001
        print(f"[spec] collect_all({pkg}) a échoué : {e}")

hiddenimports += ["bot", "google.oauth2.credentials", "google.auth.credentials"]


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
    [],
    exclude_binaries=True,
    name="BotGlobalExam",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # console=False → application fenêtrée (pas de terminal).
    # Pour DÉBOGUER : on peut lancer le binaire interne depuis le Terminal
    #   ./dist/BotGlobalExam.app/Contents/MacOS/BotGlobalExam
    # ce qui affiche les erreurs (voir GUIDE_MAC.md).
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,          # suit l'architecture du Mac de build
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="BotGlobalExam",
)

app = BUNDLE(
    coll,
    name="BotGlobalExam.app",
    icon="icone.ico",          # converti en .icns par PyInstaller (Pillow)
    bundle_identifier="com.globalexam.bot",
    info_plist={
        "CFBundleShortVersionString": "1.0",
        "NSHighResolutionCapable": True,
    },
)
