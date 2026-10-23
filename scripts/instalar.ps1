# Instala (ou atualiza) o ambiente do RPA.
# O ambiente Python fica em %LOCALAPPDATA%\RPA_NF_ESP\venv, FORA do OneDrive,
# para o OneDrive nao ficar sincronizando milhares de arquivos de biblioteca.
#
#   powershell -ExecutionPolicy Bypass -File scripts\instalar.ps1

$ErrorActionPreference = "Stop"
$projeto = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $env:LOCALAPPDATA "RPA_NF_ESP\venv"
$python = Join-Path $venv "Scripts\python.exe"

function Executar([string]$descricao, [scriptblock]$comando) {
    Write-Host "==> $descricao"
    & $comando
    if ($LASTEXITCODE -ne 0) { throw "Falhou: $descricao (codigo $LASTEXITCODE)" }
}

if (-not (Test-Path $python)) {
    Executar "Criando ambiente Python em $venv" { py -3 -m venv $venv }
}
Executar "Atualizando pip" { & $python -m pip install --quiet --upgrade pip }
Executar "Instalando dependencias" { & $python -m pip install --quiet -r (Join-Path $projeto "requirements.txt") }
Executar "Instalando o navegador Chromium do Playwright" { & $python -m playwright install chromium }

$env_arquivo = Join-Path $projeto ".env"
if (-not (Test-Path $env_arquivo)) {
    Copy-Item (Join-Path $projeto ".env.example") $env_arquivo
    Write-Host "ATENCAO: preencha usuario e senha do Espiao em $env_arquivo" -ForegroundColor Yellow
}
$config_arquivo = Join-Path $projeto "config\config.toml"
if (-not (Test-Path $config_arquivo)) {
    Copy-Item (Join-Path $projeto "config\config.example.toml") $config_arquivo
    Write-Host "ATENCAO: ajuste empresas e pastas em $config_arquivo" -ForegroundColor Yellow
}
Executar "Rodando os testes" { & $python -m pytest -q -p no:cacheprovider (Join-Path $projeto "tests") }
Write-Host "Instalacao concluida." -ForegroundColor Green
