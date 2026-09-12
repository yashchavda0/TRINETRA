@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0apply_camera_locations.ps1" %*
