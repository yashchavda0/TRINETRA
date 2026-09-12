@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_api_key.ps1" %*
