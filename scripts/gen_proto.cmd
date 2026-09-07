@rem cmd.exe wrapper for gen_proto.ps1. See publish_videos.cmd.
@powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gen_proto.ps1" %*
