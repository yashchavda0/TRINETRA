@rem cmd.exe wrapper for migrate.ps1. See publish_videos.cmd.
@powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0migrate.ps1" %*
