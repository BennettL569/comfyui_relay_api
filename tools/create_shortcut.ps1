# 在桌面和开始菜单创建「中转站管理器」快捷方式（用 ComfyUI 自带的 pythonw 运行，不会弹出黑色命令行窗口）。
# 用法：双击 create_shortcut.bat。找不到 Python 时指定：create_shortcut.bat -Python <pythonw.exe 的路径>
param(
    [string]$Python = "",
    [string[]]$Folders = @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))
)

try {
    $plugin = Split-Path $PSScriptRoot -Parent
    if (-not (Test-Path (Join-Path $plugin "manager_app.py"))) { throw "找不到 $plugin\manager_app.py" }

    if (-not $Python) {
        # 插件放在 <ComfyUI>\custom_nodes\ 里时，Python 就在它旁边。支持虚拟环境和 Windows 便携版
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
        throw "找不到 ComfyUI 的 pythonw.exe，请这样指定：create_shortcut.bat -Python <pythonw.exe 的路径>"
    }
    $Python = (Resolve-Path $Python).Path

    $shell = New-Object -ComObject WScript.Shell
    foreach ($folder in $Folders) {
        $path = Join-Path $folder "中转站管理器.lnk"
        $link = $shell.CreateShortcut($path)
        $link.TargetPath = $Python
        $link.Arguments = "`"$(Join-Path $plugin 'manager_app.py')`""
        $link.WorkingDirectory = $plugin
        $link.IconLocation = "$(Join-Path $plugin 'manager\icon.ico'),0"
        $link.Description = "中转站管理器：快捷添加第三方 API"
        $link.Save()
        Write-Output "已创建：$path"
    }
}
catch {
    Write-Host "出错了：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
