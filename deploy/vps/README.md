# RPA na VPS (Linux)

Roda o RPA num servidor sempre ligado, gravando direto no OneDrive. Resolve os casos em que o PC
não roda: desligado, suspenso ou com o usuário deslogado (sem OneDrive sincronizando).

## Arquitetura

```
rpa-nf.timer  (07h-20h, de hora em hora, Persistent)
   └─ rpa-nf.service  (oneshot, usuário rpa-nf)
        ├─ copia a lista de apelidos do gbrain (como root, só leitura)
        ├─ confere que o OneDrive está montado
        └─ python -m rpa_nf_esp
rpa-nf-onedrive.service  (rclone mount "NOTAS FISCAIS" em /home/rpa-nf/notas)
rpa-nf-resumo.timer  (20h20) └─ rpa-nf-resumo.service  (resumo do dia por e-mail)
```

Alertas por e-mail (Gmail) saem do próprio RPA: nota desistida, pesquisa falhando 3 execuções
seguidas, erro fatal, e o resumo do dia. Configuração: `ALERTA_*` no `.env` (ver `.env.example`).
Se o resumo das 20h20 não chegar, a VPS ou o agendamento parou.

| Caminho | Dono / permissão | Conteúdo |
|---|---|---|
| `/opt/rpa-nf/` | root, só leitura para os demais | código, `venv/`, `ms-playwright/` (Chromium), `bin/rclone` |
| `/opt/rpa-nf/.env` | root:rpa-nf `640` | credenciais do Espião |
| `/opt/rpa-nf/config/config.toml` | root:rpa-nf `640` | configuração com caminhos do Linux |
| `/home/rpa-nf/` | rpa-nf `700` | `dados/` (SQLite), `logs/`, `notas/` (montagem) |
| `/home/rpa-nf/.config/rclone/rclone.conf` | rpa-nf `600` | acesso ao OneDrive |

O usuário `rpa-nf` não tem `sudo` nem login (shell `nologin`, senha travada). A montagem do
OneDrive é visível só para ele (FUSE sem `allow_other`): nem o root lista a pasta.

## Instalação

Como root, num Ubuntu com Python 3.12+:

```bash
apt-get install -y python3-venv fuse3
useradd --create-home --shell /usr/sbin/nologin rpa-nf && passwd -l rpa-nf && chmod 700 /home/rpa-nf
install -d -o rpa-nf -g rpa-nf -m 700 /home/rpa-nf/dados /home/rpa-nf/logs

# código (arquivos versionados) em /opt/rpa-nf, do root
python3 -m venv /opt/rpa-nf/venv
/opt/rpa-nf/venv/bin/pip install -r /opt/rpa-nf/requirements.txt
export PLAYWRIGHT_BROWSERS_PATH=/opt/rpa-nf/ms-playwright
/opt/rpa-nf/venv/bin/playwright install-deps chromium
/opt/rpa-nf/venv/bin/playwright install --only-shell chromium
```

**rclone:** versão oficial em `/opt/rpa-nf/bin/rclone`, com o SHA-256 conferido contra o
`SHA256SUMS` de `downloads.rclone.org`. A versão do Ubuntu 26.04 (1.60) é anterior às mudanças
de identificação do OneDrive pessoal.

**Autorização do OneDrive**, feita no PC (abre o navegador) e nunca exibida na tela:

```powershell
rclone config create rpa-onedrive onedrive access_scopes "Files.Read Files.ReadWrite offline_access" --config rclone-rpa.conf
```

Se o rclone entrar em loop com `ObjectHandle is Invalid` ao escolher o drive, encerre-o: o token
já foi gravado. Acrescente ao arquivo `drive_id` (de `GET /me/drive`, campo `id`) e
`drive_type = personal`, envie para `/home/rpa-nf/.config/rclone/rclone.conf` (`600`, do
`rpa-nf`) e apague a cópia local.

**Lista de apelidos:** se ela é mantida por outro serviço da máquina (por exemplo, uma cópia de
repositório legível só pelo dono), informe o caminho em `/etc/default/rpa-nf` (root, `644`):

```bash
echo 'APELIDOS_FONTE=/caminho/da/lista/11-apelidos-fornecedores.md' > /etc/default/rpa-nf
```

O `rpa-nf.service` copia o arquivo, como root, para `/home/rpa-nf/apelidos-fonte.md` antes de cada
execução. Sem o arquivo, o RPA usa a última cópia local.

**Serviços:**

```bash
cp rpa-nf-onedrive.service rpa-nf.service rpa-nf.timer rpa-nf-resumo.service rpa-nf-resumo.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now rpa-nf-onedrive.service
systemctl enable --now rpa-nf.timer rpa-nf-resumo.timer
```

## Troca do PC para a VPS

Os dois nunca podem rodar ao mesmo tempo (bancos separados gerariam cópias):

1. `Disable-ScheduledTask -TaskName "RPA NF Espiao"` no PC.
2. Copiar o banco do PC (`%LOCALAPPDATA%\RPA_NF_ESP\dados\rpa.sqlite3`, via `sqlite3.backup`)
   para `/home/rpa-nf/dados/`. O índice de PDFs (`indice_pdf_v2`) pode ser esvaziado: ele é
   refeito na primeira execução.
3. `python -m rpa_nf_esp --ensaio` como `rpa-nf`: não deve propor nenhuma nota já salva.

Para voltar ao PC: `systemctl disable --now rpa-nf.timer` na VPS, copiar o banco de volta e
`Enable-ScheduledTask -TaskName "RPA NF Espiao"` no PC.

## Operação

```bash
systemctl list-timers rpa-nf.timer                  # próxima execução
systemctl status rpa-nf.service rpa-nf-onedrive.service
tail -50 /home/rpa-nf/logs/rpa_$(date +%F).txt      # log do dia (como root)
systemctl start rpa-nf.service                      # rodar agora

# comandos do RPA (pendências, conferência, reprocessar)
cd /opt/rpa-nf && runuser -u rpa-nf -- env HOME=/home/rpa-nf \
    PLAYWRIGHT_BROWSERS_PATH=/opt/rpa-nf/ms-playwright venv/bin/python -m rpa_nf_esp --pendencias
```

Atualizar o código: copiar os arquivos versionados para `/opt/rpa-nf` (do root) e rodar os
testes como `rpa-nf` (`venv/bin/python -m pytest -q -p no:cacheprovider`, com `pytest` instalado no venv).
