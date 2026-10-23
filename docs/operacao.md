# Operação

Manual do dia a dia: o que acompanhar, como agir em cada situação e como resolver problemas.
Para entender o funcionamento interno, veja [arquitetura.md](arquitetura.md).

## Rotina

O RPA roda sozinho de hora em hora. No dia a dia, basta:

1. **Abrir o log do dia** (`logs\rpa_AAAA-MM-DD.txt`) e procurar `ERRO` ou `AVISO`.
   Cada execução termina com um resumo por empresa:

   ```
   RESUMO DA EXECUÇÃO
     LOJA CENTRO   5 na janela | 1 baixada(s) | 2 já existia(m) | 1 aguardando XML | 0 falha(s) | 0 desistida(s)
                   + ACME 231026_RPA.pdf
   ```

2. **Uma vez por semana**, rodar a conferência: `EXECUTAR_AGORA.bat --conferir`.

## O que cada mensagem significa

| No log | Significado | Ação |
|---|---|---|
| `BAIXADA: ...` | DANFE salvo na pasta | nenhuma |
| `Já está na pasta como '...'` | alguém já tinha salvo a nota (o RPA reconheceu pela chave) | nenhuma |
| `AVISO \| Aguardando XML` | a SEFAZ só entregou o resumo; não existe DANFE ainda | nenhuma: o RPA baixa quando chegar |
| `AVISO \| Possível duplicata: ... PDF sem texto` | há um PDF digitalizado com o mesmo nome; o RPA baixou o oficial também | conferir e apagar a cópia que sobrar, se quiser |
| `ERRO \| FALHA (1ª): ...` | o download falhou; será tentado nas próximas execuções | nenhuma, a menos que se repita |
| `ERRO \| DESISTIDA ... Baixar manualmente` | esgotou as tentativas, ou o XML nunca chegou | baixar à mão; ou `--reprocessar <chave>` |
| `ERRO \| Pesquisa da empresa ... falhou` | site instável ou sessão disputada; não gasta tentativas | nenhuma, a menos que se repita por horas |
| `AVISO \| CONFLITO: ...` | na virada de mês/ano, já existia um arquivo/pasta com o mesmo nome | decidir à mão qual manter |
| `FATAL \| ... Confira ESPIAO_USUARIO/ESPIAO_SENHA` | o Espião recusou o login | corrigir o `.env` |

Cada erro de tela gera uma captura em `logs\capturas\`.

## Pendências

```powershell
EXECUTAR_AGORA.bat --pendencias
```

Lista as notas `PENDENTE`, `AGUARDANDO_XML` e `DESISTIDA`, com empresa, emissão, número, emitente,
chave e último erro. Para devolver uma nota à fila (zera as tentativas):

```powershell
EXECUTAR_AGORA.bat --reprocessar 31261099887766000105550010000000011123456782
```

## Conferência

```powershell
EXECUTAR_AGORA.bat --conferir        # últimos 29 dias (máximo do site)
EXECUTAR_AGORA.bat --conferir 7      # últimos 7 dias
```

Somente leitura: não baixa, não move e não grava nada nas pastas de notas. Gera
`logs\conferencia_AAAA-MM-DD_HHMM.txt`.

| Situação | Significado |
|---|---|
| `OK` | está na pasta certa (mês da emissão, empresa destinatária) |
| `FALTANDO` | não está em nenhuma pasta. Se for de hoje/ontem é responsabilidade do RPA (`[RPA]`); se for mais antiga, era manual |
| `FORA DO LUGAR` | está só na pasta de outra empresa ou de outro mês |
| `DUPLICADA` | a mesma nota está em mais de um arquivo |
| `SEM CONFIRMAÇÃO` | há um PDF digitalizado (sem texto) com o nome esperado; provavelmente é ela |
| `SEM XML` / `CANCELADA` | não há DANFE para baixar |
| `NOME DIVERGENTE` / `RPA EM PASTA ERRADA` | arquivo do RPA com nome ou lugar errado (não deveria ocorrer) |

O resultado final só fica `ATENÇÃO` quando há problema marcado `[RPA]`.

## Virada de mês e de ano

É automática, na primeira execução do mês. Para ver antes o que será feito, sem mexer em nada:

```powershell
"%LOCALAPPDATA%\RPA_NF_ESP\venv\Scripts\python.exe" -m rpa_nf_esp --ensaio --somente-pastas --hoje 2026-11-01
```

## Mudanças comuns

| Quero… | Onde |
|---|---|
| trocar a senha do Espião | `.env` |
| incluir/remover uma empresa | `config\config.toml`, bloco `[[empresas]]` |
| mudar o nome da pasta de uma loja | `pasta` (e `aliases_pasta` para aceitar o nome antigo) |
| apelido próprio para uma filial | `config\apelidos_extras.csv` (CNPJ completo; precedência máxima) |
| olhar só hoje / mais dias para trás | `dias_retroativos` |
| mudar o número de tentativas | `max_retentativas` |
| mudar horário ou frequência | `scripts\agendar_tarefa.ps1 -Inicio 06:00 -Horas 15` |

## Solução de problemas

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| Nada no log no horário agendado | tarefa desativada, ou cadastrada só com usuário logado e ninguém logado | `Get-ScheduledTaskInfo -TaskName "RPA NF Espiao"`; recadastrar com `agendar_tarefa.ps1` como administrador |
| `Execução ignorada: outra execução está em andamento` | a execução anterior ainda roda | normal se ocasional; se constante, verificar travamento nas capturas |
| Pesquisa falhando sempre na mesma empresa | empresa removida da conta ou sem permissão | conferir a empresa no Espião; `--visivel --empresa <CNPJ>` para acompanhar |
| Muitos `Possível duplicata` | a equipe salva PDFs digitalizados antes do RPA | combinar o fluxo, ou apagar as cópias excedentes |
| Você e o RPA se atrapalham no Espião | mesmo usuário logado nos dois | criar um usuário só para o RPA (*Cadastros > Usuários*) |
| `Fonte de apelidos indisponível` | caminho da fonte inacessível | o RPA usa a cópia local; corrigir `[apelidos] fonte` |
| `Virada de mês/ano não concluída` | OneDrive ocupado/pausado | nada: é refeita na hora seguinte; os downloads não param |

## Arquivos locais (não versionados)

| Caminho | Conteúdo |
|---|---|
| `.env` | credenciais |
| `config\config.toml` | configuração real |
| `logs\` | logs, capturas e relatórios de conferência |
| `%LOCALAPPDATA%\RPA_NF_ESP\dados\rpa.sqlite3` | banco de controle (notas e execuções) |
| `%LOCALAPPDATA%\RPA_NF_ESP\venv\` | ambiente Python |
