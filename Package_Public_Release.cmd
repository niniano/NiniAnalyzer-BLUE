@echo off
setlocal
cd /d "%~dp0"
where py.exe >nul 2>&1
if errorlevel 1 goto FAIL
echo [PACK] Packaging NiniAnalyzer Public EXE folder...
py -3 package_public_release.py
if errorlevel 1 goto FAIL
echo.
echo [DONE] ZIP and SHA256 are in dist folder.
pause
exit /b 0
:FAIL
echo [ERROR] Package failed. Build EXE first and check dist folder.
pause
exit /b 1
