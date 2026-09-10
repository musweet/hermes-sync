@echo off
title Hermes Sync
cd /d "%~dp0"
"%USERPROFILE%\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe" "%~dp0run-forever.py"
