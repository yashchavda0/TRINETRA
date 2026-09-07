@rem cmd.exe wrapper for gen_dev_certs.ps1. See publish_videos.cmd.
@powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gen_dev_certs.ps1" %*
