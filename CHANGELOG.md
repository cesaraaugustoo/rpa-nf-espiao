# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/); versões seguem
[SemVer](https://semver.org/lang/pt-BR/).

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
