@echo off
rem Opens the relay manager without starting ComfyUI, using the Python that ComfyUI already has.
rem Keep this file ASCII only: cmd reads it in the system code page.
setlocal
set "PLUGIN=%~dp0.."
set "BASE=%PLUGIN%\..\.."
set "PYW="
for %%P in (
    "%BASE%\.venv\Scripts\pythonw.exe"
    "%BASE%\venv\Scripts\pythonw.exe"
    "%BASE%\..\.venv\Scripts\pythonw.exe"
    "%BASE%\..\python_embeded\pythonw.exe"
) do (
    if not defined PYW if exist "%%~P" set "PYW=%%~P"
)
if not defined PYW (
    for /f "delims=" %%P in ('where pythonw 2^>nul') do (
        if not defined PYW set "PYW=%%P"
    )
)
if not defined PYW goto no_python

"%PYW:pythonw.exe=python.exe%" -c "import aiohttp" >nul 2>nul
if errorlevel 1 goto no_aiohttp

start "" "%PYW%" "%PLUGIN%\manager_app.py" %*
exit /b 0

:no_python
echo Cannot find Python. Put this plugin in ComfyUI\custom_nodes, or add the Python that runs ComfyUI to PATH.
pause
exit /b 1

:no_aiohttp
echo This Python has no aiohttp, so it is probably not the one that runs ComfyUI:
echo   %PYW%
echo Add the Python that runs ComfyUI to PATH, or run: pip install aiohttp
pause
exit /b 1
