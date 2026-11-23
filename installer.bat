@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
echo === Installation de l'assistant de pre-audit ISO 27001 ===

if exist venv\Scripts\python.exe goto :install

set "PY="
rem 1) Lanceur "py" (installe avec Python depuis python.org) : versions preferees d'abord
for %%V in (3.12 3.11 3.13 3.14) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
  )
)
rem 2) Emplacements d'installation habituels
if not defined PY for %%P in ("C:\Python312\python.exe" "C:\Python311\python.exe" "C:\Python313\python.exe" "C:\Python314\python.exe") do (
  if not defined PY if exist %%P set "PY=%%~P"
)
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
  if not defined PY if exist "%%D\python.exe" set "PY=%%D\python.exe"
)
rem 3) "python" du PATH, seulement s'il ne s'agit pas du raccourci Microsoft Store
if not defined PY (
  python -c "import sys" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo Python introuvable. Installe Python 3.12 depuis python.org en cochant "Add python.exe to PATH".
  pause
  exit /b 1
)

echo Python utilise : !PY!
!PY! --version
!PY! -m venv venv
if errorlevel 1 (
  echo Echec de creation de l'environnement virtuel.
  pause
  exit /b 1
)

:install
call venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Echec d'installation des paquets. Avec Python 3.14, un paquet peut ne pas etre encore disponible :
  echo installe Python 3.12, supprime le dossier venv, puis relance installer.bat.
  pause
  exit /b 1
)
echo.
echo === Verification d'Ollama et des modeles ===
python -m preaudit check
pause
