@echo off
setlocal
cd /d "%~dp0"
where py.exe >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python 3.12/3.13 required on the BUILD PC.
  pause
  exit /b 1
)
py -3 verify_release.py
if errorlevel 1 goto FAIL
py -3 -m pip install -r requirements-build.txt
if errorlevel 1 goto FAIL
echo [CHECK] Public Drive privacy regression...
py -3 -m unittest discover -s qa -p test_public_no_drive_leak.py -v
if errorlevel 1 goto FAIL
echo [CHECK] Release packaging regressions...
py -3 -m unittest discover -s qa -p test_public_stage2_4_release.py -v
if errorlevel 1 goto FAIL
py -3 -m unittest discover -s qa -p test_public_stage2_5_rc3.py -v
if errorlevel 1 goto FAIL
echo [CHECK] RC4/RC4.1 fixes (feign death evidence, first attacker, first screen, scroll, fonts, backup)...
py -3 -m unittest discover -s qa -p test_public_rc4_fixes.py -v
if errorlevel 1 goto FAIL
echo [CHECK] RC4.2 Feign Death recheck (read-only search, selected-only, backup first)...
py -3 -m unittest discover -s qa -p test_public_rc4_2_feign_recheck.py -v
if errorlevel 1 goto FAIL
echo [CHECK] RC4.3 puller name class colour...
py -3 -m unittest discover -s qa -p test_public_rc4_3_class_color.py -v
if errorlevel 1 goto FAIL
echo [CHECK] RC4.4 installed font is not registered twice...
py -3 -m unittest discover -s qa -p test_public_rc4_4_fonts.py -v
if errorlevel 1 goto FAIL
echo [CHECK] RC5 V4.7 display modules, frozen boss lookup, personal data excluded...
py -3 -m unittest discover -s qa -p test_public_rc5_layout.py -v
if errorlevel 1 goto FAIL
echo [CHECK] RC5.3 GOLD V5.0/V5.2 (Kithix mythic, Coiled kick rules, shield box, timeline links)...
py -3 -m unittest discover -s qa -p test_public_rc5_3.py -v
if errorlevel 1 goto FAIL
echo [CHECK] RC5.3.1 rogue-only modules removed...
py -3 -m unittest discover -s qa -p test_public_rc5_3_1.py -v
if errorlevel 1 goto FAIL
echo [CHECK] RC5.4 GOLD V5.2.4 sync...
py -3 -m unittest discover -s qa -p "test_public_rc5_4*.py" -v
if errorlevel 1 goto FAIL
echo [CHECK] RC5.5 Brand Kit R3 icons...
py -3 -m unittest discover -s qa -p "test_public_rc5_5*.py" -v
if errorlevel 1 goto FAIL
echo [CHECK] RC5.6 mythic hotfix 2026-11-06...
py -3 -m unittest discover -s qa -p "test_public_rc5_6*.py" -v
if errorlevel 1 goto FAIL
echo [CHECK] 1.0.1 bloodlust self-aura fix...
py -3 -m unittest discover -s qa -p "test_public_1_0_1*.py" -v
if errorlevel 1 goto FAIL
echo [CHECK] 1.0.2 coiled judgment fixes...
py -3 -m unittest discover -s qa -p "test_public_1_0_2*.py" -v
if errorlevel 1 goto FAIL
echo [CHECK] 1.0.3 onboarding cards...
py -3 -m unittest discover -s qa -p "test_public_1_0_3*.py" -v
if errorlevel 1 goto FAIL
echo [CHECK] 1.0.5 Ulatek egg/viper notes...
py -3 -m unittest discover -s qa -p "test_public_1_0_5*.py" -v
if errorlevel 1 goto FAIL
echo [CHECK] 1.0.6 GOLD catch-up (kill review / board / coiled accuracy)...
py -3 -m unittest discover -s qa -p "test_public_1_0_6*.py" -v
if errorlevel 1 goto FAIL
echo [CHECK] 1.0.7 Coiled P1 guillotine / kill review / Ulatek heart cut...
py -3 -m unittest discover -s qa -p "test_public_1_0_7*.py" -v
if errorlevel 1 goto FAIL
echo [CHECK] 1.0.8 Ulatek egg room big eggs...
py -3 -m unittest discover -s qa -p "test_public_1_0_8*.py" -v
if errorlevel 1 goto FAIL
echo [CHECK] Try mouse-wheel GUI regressions...
py -3 -m unittest discover -s qa -p test_nini_stage2_2_1_gui_regression.py -v
if errorlevel 1 goto FAIL
py -3 -m unittest discover -s qa -p test_b37_5_0_step4_3_live_try_wheel.py -v
if errorlevel 1 goto FAIL
py -3 -m unittest discover -s qa -p test_b37_5_0_5_try_wheel_focus_regression.py -v
if errorlevel 1 goto FAIL
py -3 build_public_windows.py
if errorlevel 1 goto FAIL
echo [PACK] Create release ZIP...
py -3 package_public_release.py
if errorlevel 1 goto FAIL
call "%~dp0Build_Installer_Only.cmd" nopause
if errorlevel 1 goto FAIL
echo.
echo [DONE] dist\NiniAnalyzer_Public_1.0.8_Setup_Windows_x64.exe   (give this to guild members)
echo [DONE] dist\NiniAnalyzer_Public_1.0.8_Windows_x64_SAFE_FOLDER.zip   (portable, optional)
pause
exit /b 0
:FAIL
echo [ERROR] Build failed. See the console messages.
pause
exit /b 1
