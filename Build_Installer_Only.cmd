@echo off
setlocal
cd /d "%~dp0"
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC (
  echo [ERROR] Inno Setup 6 not found. Install it once from https://jrsoftware.org/isdl.php
  echo         then run Build_Installer_Only.cmd again. The EXE folder and ZIP are already built.
  if /i not "%~1"=="nopause" pause
  exit /b 1
)
if not exist "dist\NiniAnalyzer_Public\NiniAnalyzer_Public.exe" (
  echo [ERROR] dist\NiniAnalyzer_Public is missing. Run Build_Public_EXE.cmd first.
  if /i not "%~1"=="nopause" pause
  exit /b 1
)
echo [SETUP] Compiling installer with Inno Setup...
"%ISCC%" /Q "installer\NiniAnalyzer_Public.iss"
if errorlevel 1 (
  echo [ERROR] Installer compile failed.
  if /i not "%~1"=="nopause" pause
  exit /b 1
)
powershell -NoProfile -Command "$f='dist\NiniAnalyzer_Public_1.0.6_Setup_Windows_x64.exe'; $h=(Get-FileHash $f -Algorithm SHA256).Hash.ToLower(); Set-Content -Encoding ascii ($f+'.sha256.txt') ($h+' *'+(Split-Path $f -Leaf)); Write-Host ('SETUP SHA256: '+$h)"
echo [DONE] dist\NiniAnalyzer_Public_1.0.6_Setup_Windows_x64.exe
if /i not "%~1"=="nopause" pause
exit /b 0
