# gh-rank 一键启动器
#
# 职责：
#   1. 服务已在跑 → 直接开浏览器（重复点不会起一堆进程）
#   2. 服务没跑 → 后台拉起，等端口就绪后自动开浏览器
#   3. 缺依赖时给出可照做的提示，而不是甩一堆红字

$ErrorActionPreference = 'Continue'
$Port = 8765
$Url = "http://127.0.0.1:$Port/"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Test-PortAlive([int]$Port) {
    $c = New-Object Net.Sockets.TcpClient
    try {
        $task = $c.ConnectAsync('127.0.0.1', $Port)
        if (-not $task.Wait(800)) { return $false }
        return $c.Connected
    } catch { return $false } finally { $c.Close() }
}

function Wait-PortAlive([int]$Port, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-PortAlive $Port) { return $true }
        Start-Sleep -Milliseconds 400
    }
    return $false
}

function Stop-Existing {
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($conn in $conns) {
        $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        if ($proc -and $proc.ProcessName -like 'python*') {
            Write-Host "  Found old gh-rank process (PID $($conn.OwningProcess)), stopping it..."
            Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
            Start-Sleep -Milliseconds 600
        }
    }
}

function Show-Fail([string]$Msg) {
    try {
        Add-Type -AssemblyName PresentationFramework -ErrorAction SilentlyContinue
        [System.Windows.MessageBox]::Show($Msg, 'gh-rank 启动失败', 'OK', 'Error') | Out-Null
    } catch {
        Write-Host $Msg
    }
    exit 1
}

# ---------------------------------------------------------------- 1. already running
if (Test-PortAlive $Port) {
    Start-Process $Url
    exit 0
}

# ---------------------------------------------------------------- 2. preflight checks
$python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Show-Fail "没找到虚拟环境。`n`n请先在项目目录执行：`n`n  cd `"$Root`"`n  uv venv`n  uv pip install fastapi uvicorn[standard] httpx beautifulsoup4 lxml apscheduler python-dotenv"
}

$envFile = Join-Path $Root '.env'
if (-not (Test-Path $envFile)) {
    Show-Fail "没找到 .env 配置文件。`n`n从 .env.example 复制一份并填入 LLM_API_KEY：`n`n  copy `"$envFile.example`" `"$envFile`""
}

$hasKey = Select-String -Path $envFile -Pattern '^LLM_API_KEY=.+$' -Quiet -ErrorAction SilentlyContinue
$isMock = Select-String -Path $envFile -Pattern '^LLM_PROVIDER=mock\s*$' -Quiet -ErrorAction SilentlyContinue
if (-not $hasKey -and -not $isMock) {
    Show-Fail ".env 里没有配置 LLM_API_KEY。`n`n请编辑：`n$envFile`n`n填上你的大模型 API Key（DeepSeek 之类），否则只能看到榜单、看不到 AI 说明。"
}

# ---------------------------------------------------------------- 3. start server
Stop-Existing

Write-Host '  正在启动 gh-rank...'
$env:PYTHONUTF8 = '1'
$startArgs = @{
    FilePath         = $python
    ArgumentList     = @('-m', 'app.cli', 'serve')
    WorkingDirectory = $Root
    WindowStyle      = 'Minimized'
}
Start-Process @startArgs

# ---------------------------------------------------------------- 4. wait until ready
if (Wait-PortAlive $Port 30) {
    Start-Sleep -Milliseconds 500
    Start-Process $Url
    exit 0
}

Show-Fail "等了 30 秒服务还没起来。`n`n请在项目目录手动运行看报错：`n`n  cd `"$Root`"`n  .\.venv\Scripts\python.exe -m app.cli serve"