param([int]$Port=8765,[string]$Bind='127.0.0.1')
$ErrorActionPreference='Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python虚拟环境创建失败' }
}
& '.venv/Scripts/python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败' }
Write-Host "网页地址 http://$Bind`:$Port （无需登录）"
& '.venv/Scripts/python.exe' -m uvicorn app:app --host $Bind --port $Port
