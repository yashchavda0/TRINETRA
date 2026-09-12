@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0probe_camera_health.ps1" %*
