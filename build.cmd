@echo off
rem Builds the distributable Windows package without touching the PowerShell
rem execution policy. Double-click this file, or run it from cmd.
rem
rem   build.cmd          -> folder build (dist\IndexTranslate-desktop\) + zip
rem   build.cmd onefile  -> single .exe convenience build
rem
rem A folder build is the default because a PyInstaller --onefile exe unpacks
rem itself into %TEMP% on every start, which fails with "Could not create
rem temporary directory!" on locked-down Windows profiles.
setlocal
cd /d "%~dp0"

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
set "NAME=IndexTranslate-desktop"

if not exist "assets\icon.ico" "%PY%" tools\make_icon.py
if errorlevel 1 goto :fail

if /i "%~1"=="onefile" goto :onefile

rem ---- folder build (recommended) --------------------------------------------
"%PY%" -m PyInstaller ^
  --noconfirm --clean --windowed ^
  --name %NAME% ^
  --icon assets\icon.ico ^
  --version-file version_info.txt ^
  --add-data "assets;assets" ^
  --collect-all qfluentwidgets ^
  --collect-all qframelesswindow ^
  --exclude-module PyQt5 ^
  --exclude-module tkinter ^
  --exclude-module matplotlib ^
  --exclude-module numpy ^
  run.py
if errorlevel 1 goto :fail
echo.
echo Done: dist\%NAME%\%NAME%.exe
exit /b 0

rem ---- single file build ----------------------------------------------------
:onefile
rem --runtime-tmpdir keeps the unpack next to the exe instead of in %TEMP%,
rem which is what makes the one-file build usable on locked-down machines.
"%PY%" -m PyInstaller ^
  --noconfirm --clean --windowed --onefile ^
  --runtime-tmpdir ".IndexTranslateRuntime" ^
  --name %NAME% ^
  --icon assets\icon.ico ^
  --version-file version_info.txt ^
  --add-data "assets;assets" ^
  --collect-all qfluentwidgets ^
  --collect-all qframelesswindow ^
  --exclude-module PyQt5 ^
  --exclude-module tkinter ^
  --exclude-module matplotlib ^
  --exclude-module numpy ^
  run.py
if errorlevel 1 goto :fail
echo.
echo Done: dist\%NAME%.exe
exit /b 0

:fail
echo.
echo Build failed. See the output above.
exit /b 1
