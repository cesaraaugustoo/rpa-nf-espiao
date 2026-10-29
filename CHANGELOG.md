# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/); versões seguem
[SemVer](https://semver.org/lang/pt-BR/).

## [1.4.0] - 2026-10-29

### Adicionado

- XML da nota: depois do PDF, o RPA baixa *Download do XML*, confere que é o XML da mesma nota
  e salva ao lado do PDF com o mesmo nome (`ACME 231026_RPA.xml`). Falha no XML não desfaz o PDF:
  o XML é tentado de novo nas próximas execuções (1 + 5 tentativas, depois entra no alerta).
- Notas baixadas antes desta versão, ainda no alcance da pesquisa (29 dias), ganham o XML na
  primeira execução.

## [1.3.0] - 2026-10-27

### Adicionado

- Alertas por e-mail (Gmail): nota desistida, pesquisa de uma empresa falhando 3 execuções
  seguidas, erro fatal, e resumo do dia às 20h20. Sem repetir o mesmo alerta a cada hora.
  Comandos `--testar-email` e `--resumo-do-dia`.
- Execução em VPS Linux (`deploy/vps/`): serviços systemd de hora em hora, OneDrive montado
  com rclone, usuário dedicado sem privilégios. Resolve PC desligado, suspenso ou deslogado.
- PC suspenso: a tarefa agendada acorda o computador no horário do ciclo (só na tomada) e o RPA
  impede o Windows de voltar a dormir no meio da execução.
- Testes automáticos também no Linux (GitHub Actions).

### Corrigido

- Investigar XML falhava na 2ª nota "Sem XML" da mesma empresa no mesmo ciclo: a janela
  fechada continua escondida na página com uma paginação própria. A tabela agora é lida só
  pelo que está visível.
- No Linux, gravar e mover arquivos nunca sobrescreve um existente (o `rename` do Linux
  substitui o destino sem avisar; o nome passa a ser reservado antes).

## [1.2.0] - 2026-10-26

### Adicionado

- Investigar XML automático: para cada nota "Sem XML", o RPA aciona *Investigar XML* no Espião
  (que busca o XML na SEFAZ na hora), pesquisa de novo e baixa no mesmo ciclo. Antes, a nota
  esperava a distribuição automática da SEFAZ, de 1 a 3 horas nos casos observados.

## [1.1.0] - 2026-10-23

### Adicionado

- Janela sem lacunas: o RPA registra, por empresa, o último dia de pesquisa completa e, depois de
  um período sem execução (computador desligado ou suspenso, internet ou site fora do ar), cobre
  todo o intervalo, até o limite de 29 dias do site.

### Alterado

- A conferência considera responsabilidade do RPA todas as notas emitidas desde o início da
  cobertura de cada empresa, e não só as de hoje e ontem.

## [1.0.0] - 2026-10-23

### Adicionado

- Download automático dos DANFEs das NF-e recebidas no Espião NF-e, por empresa configurada,
  para a pasta do mês de emissão, com o nome `APELIDO DDMMAA_RPA.pdf` e sequência ` 2`, ` 3`
  para notas do mesmo fornecedor no mesmo dia.
- Deduplicação em duas camadas: banco de controle por chave de acesso e leitura da chave
  dentro dos PDFs já presentes na pasta (reconhece arquivos salvos à mão).
- Novas tentativas: download que falha é retentado nas 5 execuções seguintes; notas "Sem XML"
  são vigiadas sem gastar tentativas.
- Virada de mês (arquivamento em `<ano>\<ano>\`) e de ano (achatamento do ano anterior).
- Lista de apelidos de fornecedores em markdown (confirmados e sugeridos) com sobreposição por
  CNPJ completo via CSV.
- Conferência somente leitura Espião x pastas (`--conferir`), com relatório TXT.
- Linha de comando: `--ensaio`, `--visivel`, `--empresa`, `--somente-pastas`, `--pendencias`,
  `--reprocessar`, `--conferir`, `--detalhado`.
- Log TXT diário com resumo por empresa e capturas de tela em caso de erro.
- Scripts de instalação e de agendamento (S4U, com senha ou só logado).
- Testes automatizados com dados fictícios e CI no GitHub Actions (Windows; Python 3.12–3.14).
