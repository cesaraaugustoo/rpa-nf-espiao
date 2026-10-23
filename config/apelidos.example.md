# Apelidos de fornecedor (MODELO)

Formato da lista de apelidos lida pelo RPA. O apelido é o que vai no nome do arquivo:
`APELIDO DDMMAA_RPA.pdf`. Os CNPJs e nomes abaixo são **fictícios**.

**A chave é a raiz do CNPJ (8 dígitos)**: o apelido é da empresa, não da filial.
A coluna da raiz é reconhecida pelo conteúdo (8 dígitos), não pela posição.

| Raiz CNPJ | Apelido | Razão social do emitente |
|---|---|---|
| 99887766 | ACME | ACME ALIMENTOS LTDA |
| 12312312 | CAFÉ BOM | CAFE BOM COMERCIO LTDA |

## Apelidos sugeridos, a confirmar

Usados só quando `usar_sugeridos = true` no config. Os subtítulos (`###`) herdam a seção.

### Com nota no sistema (1)

| Apelido sugerido | Raiz CNPJ | Razão social | Confiança |
|---|---|---|---|
| GASBOM | 55443322 | GASBOM DISTRIBUIDORA S A | alta |
