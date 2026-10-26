@echo off
chcp 65001 >nul
setlocal
title Construction - BotGlobalExam.exe
cd /d "%~dp0"

echo ============================================================
echo    CONSTRUCTION DE BotGlobalExam.exe
echo ============================================================
echo.

rem --- 1. Trouver Python -------------------------------------------
set "PY=py -3"
where py >nul 2>&1
if not errorlevel 1 goto :py_ok
set "PY=python"
where python >nul 2>&1
if errorlevel 1 goto :no_python
:py_ok
echo [1/5] Python trouve : %PY%

rem --- 2. Verifier que le code n'est pas casse ----------------------
echo [2/5] Verification de bot.py et launcher.py...
%PY% -m py_compile bot.py
if errorlevel 1 goto :code_error
%PY% -m py_compile launcher.py
if errorlevel 1 goto :code_error
echo       OK, aucune erreur de syntaxe.

rem --- 3. Dependances -----------------------------------------------
echo [3/5] Verification des dependances...
%PY% -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :pip_error

rem --- 4. Fermer l'ancien exe et le garder en secours ---------------
echo [4/5] Preparation...
taskkill /f /im BotGlobalExam.exe >nul 2>&1
timeout /t 1 /nobreak >nul
if exist "dist\BotGlobalExam.exe" copy /y "dist\BotGlobalExam.exe" "dist\BotGlobalExam_ancien.exe" >nul

rem --- 5. Construction -----------------------------------------------
echo [5/5] Construction en cours, patiente quelques minutes...
echo.
%PY% -m PyInstaller --noconfirm --clean globalexam_bot.spec
if errorlevel 1 goto :build_error
if not exist "dist\BotGlobalExam.exe" goto :build_error

echo.
echo ============================================================
echo    TERMINE ! Le nouvel exe est ici :
echo    %CD%\dist\BotGlobalExam.exe
echo ============================================================
if exist "dist\BotGlobalExam_ancien.exe" echo    Version precedente gardee : dist\BotGlobalExam_ancien.exe
echo.
explorer "%CD%\dist"
pause
exit /b 0

:no_python
echo [ERREUR] Python introuvable. Installe-le depuis python.org
echo          en cochant "Add Python to PATH".
pause
exit /b 1

:code_error
echo.
echo [ERREUR] Le code contient une erreur, voir le message ci-dessus.
echo          Construction annulee : l'ancien exe n'a pas ete touche.
pause
exit /b 1

:pip_error
echo.
echo [ERREUR] Installation des dependances echouee, voir ci-dessus.
pause
exit /b 1

:build_error
echo.
echo [ERREUR] La construction a echoue, voir les messages ci-dessus.
if exist "dist\BotGlobalExam_ancien.exe" if not exist "dist\BotGlobalExam.exe" copy /y "dist\BotGlobalExam_ancien.exe" "dist\BotGlobalExam.exe" >nul
pause
exit /b 1
