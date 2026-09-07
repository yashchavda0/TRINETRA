@rem Runs publish_videos.ps1 from cmd.exe, which cannot execute .ps1 files
@rem itself - it hands them to their file association, which is usually Notepad.
@rem %~dp0 resolves beside this wrapper; %* forwards -List / -Stop / -VideoDir.
@powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0publish_videos.ps1" %*
