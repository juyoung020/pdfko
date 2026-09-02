@echo off
REM pdfko 원샷 실행 (윈도우) — 이 파일을 더블클릭하세요.
REM
REM 윈도우는 .ps1 을 더블클릭하면 실행하지 않고 메모장으로 엽니다. 그래서
REM 실제 일은 pdfko.ps1 이 하고, 이 파일은 그걸 띄워 주기만 합니다.
REM -ExecutionPolicy Bypass 는 이 실행에만 적용되고 시스템 설정을 바꾸지
REM 않습니다 (기본값이 Restricted 라 이게 없으면 스크립트가 막힙니다).

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0pdfko.ps1"
if errorlevel 1 pause
