# Creates "Relay Manager" shortcuts on the desktop and in the Start menu, using ComfyUI's own pythonw (no console window).
# Double-click create_shortcut.bat. If Python is not found: create_shortcut.bat -Python <path to pythonw.exe>
param(
    [string]$Python = "",
    [string[]]$Folders = @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))
)

try {
    $plugin = Split-Path $PSScriptRoot -Parent
    if (-not (Test-Path (Join-Path $plugin "manager_app.py"))) { throw "Cannot find $plugin\manager_app.py" }

    if (-not $Python) {
        # When the plugin is in <ComfyUI>\custom_nodes\, Python is close by: a virtual environment or the Windows portable build
        $base = Join-Path $plugin "..\.."
        $candidates = @(
            (Join-Path $base ".venv\Scripts\pythonw.exe"),
            (Join-Path $base "venv\Scripts\pythonw.exe"),
            (Join-Path $base "..\.venv\Scripts\pythonw.exe"),
            (Join-Path $base "..\python_embeded\pythonw.exe")
        )
        $installs = Join-Path $env:LOCALAPPDATA "Comfy-Desktop\ComfyUI-Installs"
        if (Test-Path $installs) {
            $candidates += Get-ChildItem $installs -Directory | ForEach-Object { Join-Path $_.FullName "ComfyUI\.venv\Scripts\pythonw.exe" }
        }
        $Python = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    }
    if (-not $Python -or -not (Test-Path $Python)) {
        throw "Cannot find ComfyUI's pythonw.exe. Specify it: create_shortcut.bat -Python <path to pythonw.exe>"
    }
    $Python = (Resolve-Path $Python).Path

    $shell = New-Object -ComObject WScript.Shell
    foreach ($folder in $Folders) {
        $path = Join-Path $folder "Relay Manager.lnk"
        $link = $shell.CreateShortcut($path)
        $link.TargetPath = $Python
        $link.Arguments = "`"$(Join-Path $plugin 'manager_app.py')`""
        $link.WorkingDirectory = $plugin
        $link.IconLocation = "$(Join-Path $plugin 'manager\icon.ico'),0"
        $link.Description = "Relay Manager: add third-party APIs"
        $link.Save()
        Write-Output "Created: $path"
    }
}
catch {
    Write-Host "Error: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
