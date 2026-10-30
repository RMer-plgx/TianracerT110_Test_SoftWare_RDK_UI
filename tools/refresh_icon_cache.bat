@echo off
REM Clear Windows icon cache and restart Explorer.
REM Run this if the exe still shows an old or default icon.
REM
REM Why: Explorer caches icons by file path. Rebuilding an exe at the same
REM path can leave the stale icon visible even though the new icon is
REM correctly embedded in the file.
REM
REM Usage: double-click this file (admin usually not required).

setlocal

echo Stopping Explorer...
taskkill /f /im explorer.exe >nul 2>&1

echo Deleting icon cache files...
del /a /f /q "%localappdata%\IconCache.db" >nul 2>&1
del /a /f /q "%localappdata%\Microsoft\Windows\Explorer\iconcache_*.db" >nul 2>&1
del /a /f /q "%localappdata%\Microsoft\Windows\Explorer\thumbcache_*.db" >nul 2>&1

echo Starting Explorer...
start explorer.exe

echo.
echo Done. Check the exe icon again.
pause
