"""Geradores de documentos fiscais FICTÍCIOS para os testes.

Nenhum teste usa CNPJ ou chave de acesso real: tudo é gerado aqui, com dígito verificador
calculado de forma independente do código de produção (serve de oráculo para ele).
"""


def _dv(corpo: str) -> int:
    soma, peso = 0, 2
    for digito in reversed(corpo):
        soma += int(digito) * peso
        peso = 2 if peso == 9 else peso + 1
    resto = soma % 11
    return 0 if resto < 2 else 11 - resto


def gerar_cnpj(base12: str) -> str:
    d1 = _dv(base12)
    return base12 + str(d1) + str(_dv(base12 + str(d1)))


def gerar_chave(cnpj_emitente: str, numero: int, aamm: str = "2610", serie: int = 1,
                uf: str = "31", codigo: str = "12345678") -> str:
    corpo = f"{uf}{aamm}{cnpj_emitente}55{serie:03d}{numero:09d}1{codigo}"
    assert len(corpo) == 43
    return corpo + str(_dv(corpo))


# Empresas destinatárias (lojas) e fornecedores fictícios
CNPJ_LOJA_CENTRO = gerar_cnpj("112223330001")      # 11222333000181
CNPJ_LOJA_NORTE = gerar_cnpj("445556660001")       # 44555666000181
CNPJ_ACME = gerar_cnpj("998877660001")             # raiz 99887766
CNPJ_ACME_FILIAL = gerar_cnpj("998877660029")      # mesma raiz, outra filial
CNPJ_GASBOM = gerar_cnpj("554433220001")           # raiz 55443322 (apelido sugerido)
