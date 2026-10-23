# Cadastra o RPA no Agendador de Tarefas do Windows: de hora em hora, das 07:00 as 20:00.
# Roda com pythonw (sem janela piscando). Se o PC estiver desligado no horario, roda quando ligar.
# Se uma execucao ainda estiver em andamento, a seguinte e ignorada.
#
# Por padrao roda MESMO COM O USUARIO DESLOGADO, sem guardar senha (modo S4U do Windows).
# O S4U nao acessa pastas de rede do Windows; o RPA nao precisa: usa internet e o disco local.
# Arquivos gravados na pasta do OneDrive com o usuario deslogado sobem quando ele entrar de novo.
#
#   powershell -ExecutionPolicy Bypass -File scripts\agendar_tarefa.ps1              (deslogado, sem senha)
#   powershell -ExecutionPolicy Bypass -File scripts\agendar_tarefa.ps1 -ComSenha    (deslogado, com senha)
#   powershell -ExecutionPolicy Bypass -File scripts\agendar_tarefa.ps1 -SoLogado
#   powershell -ExecutionPolicy Bypass -File scripts\agendar_tarefa.ps1 -Remover
#
# -ComSenha pede a senha do Windows (conta Microsoft: a senha da conta, nao o PIN). Ela fica
# guardada pelo proprio Windows no Agendador; este script nao grava a senha em lugar nenhum.
# Nesse modo, se a senha do Windows mudar, rode o script de novo.

param(
    [string]$Nome = "RPA NF Espiao",
    [string]$Inicio = "07:00",
    [int]$Horas = 13,
    [switch]$ComSenha,
    [switch]$SoLogado,
    [switch]$Remover
)

$ErrorActionPreference = "Stop"

if ($Remover) {
    Unregister-ScheduledTask -TaskName $Nome -Confirm:$false
    Write-Host "Tarefa '$Nome' removida."
    return
}

$projeto = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $env:LOCALAPPDATA "RPA_NF_ESP\venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Ambiente nao instalado. Rode scripts\instalar.ps1 antes." }

$acao = New-ScheduledTaskAction -Execute $pythonw -Argument "-m rpa_nf_esp" -WorkingDirectory $projeto
$gatilho = New-ScheduledTaskTrigger -Daily -At $Inicio
$repeticao = New-ScheduledTaskTrigger -Once -At $Inicio `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Hours $Horas -Minutes 1)
$gatilho.Repetition = $repeticao.Repetition

$config = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $SoLogado -and -not $admin) {
    throw "Para rodar com o usuario deslogado, abra o PowerShell com 'Executar como administrador' e rode este script de novo. (Ou use -SoLogado.)"
}
$usuarioId = "$env:USERDOMAIN\$env:USERNAME"
$descricao = "Baixa os DANFEs do Espiao NF-e para NOTAS FISCAIS. Logs em $projeto\logs"

if ($ComSenha) {
    $seguro = Read-Host "Senha do Windows de $usuarioId (conta Microsoft: senha da conta, nao o PIN)" -AsSecureString
    $senha = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($seguro))
    # O Windows valida a senha aqui: se estiver errada, o cadastro falha e nada e alterado.
    Register-ScheduledTask -TaskName $Nome -Action $acao -Trigger $gatilho -Settings $config `
        -User $usuarioId -Password $senha -RunLevel Limited -Description $descricao -Force | Out-Null
    $senha = $null
    $modo = "mesmo com o usuario deslogado, com senha"
} else {
    $tipo = if ($SoLogado) { "Interactive" } else { "S4U" }
    $usuario = New-ScheduledTaskPrincipal -UserId $usuarioId -LogonType $tipo -RunLevel Limited
    Register-ScheduledTask -TaskName $Nome -Action $acao -Trigger $gatilho -Settings $config -Principal $usuario `
        -Description $descricao -Force | Out-Null
    $modo = if ($SoLogado) { "so com o usuario logado" } else { "mesmo com o usuario deslogado, sem senha (S4U)" }
}

$t = Get-ScheduledTask -TaskName $Nome
$info = Get-ScheduledTaskInfo -TaskName $Nome
Write-Host "Tarefa '$Nome' cadastrada ($modo): de hora em hora, $Inicio por $Horas h. Estado: $($t.State). Proxima execucao: $($info.NextRunTime)"
