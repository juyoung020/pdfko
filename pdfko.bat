@echo off
REM pdfko one-shot launcher (Windows) - double-click this file.
REM
REM Windows opens .ps1 in Notepad on double-click instead of running it,
REM so pdfko.ps1 does the real work and this file only launches it.
REM -ExecutionPolicy Bypass applies to this run only; it does not change
REM any system setting (the default is Restricted, which would block it).
REM
REM Keep this file ASCII-only. cmd.exe reads .bat in the OEM codepage
REM (949 on Korean Windows), not UTF-8, so non-ASCII bytes get mis-decoded
REM and cmd can run a fragment of a comment line as a command. Do not add
REM a UTF-8 BOM here either - cmd would try to execute it. Korean notes
REM live in pdfko.ps1, which is UTF-8 with BOM and handles them fine.

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0pdfko.ps1"
if errorlevel 1 pause
