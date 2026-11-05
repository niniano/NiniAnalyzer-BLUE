@echo off
setlocal
cd /d "%~dp0"
py -3 nini_raid_gui.py
if errorlevel 1 pause
