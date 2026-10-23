"""Validação de CNPJ e da chave de acesso da NF-e (44 dígitos).

Layout da chave: cUF(2) AAMM(4) CNPJ(14) modelo(2) série(3) número(9) tpEmis(1) código(8) DV(1).
O CNPJ do emitente sai da própria chave, que é assinada pela SEFAZ: é a fonte mais confiável
para chegar ao apelido do fornecedor.
"""

import re
from dataclasses import dataclass

_SO_DIGITOS = re.compile(r"\D")


def so_digitos(texto: str) -> str:
    return _SO_DIGITOS.sub("", texto or "")


def _dv_mod11(corpo: str, peso_max: int) -> int:
    soma, peso = 0, 2
    for digito in reversed(corpo):
        soma += int(digito) * peso
        peso = 2 if peso == peso_max else peso + 1
    resto = soma % 11
    return 0 if resto < 2 else 11 - resto


def cnpj_valido(cnpj: str) -> bool:
    if not re.fullmatch(r"\d{14}", cnpj or "") or len(set(cnpj)) == 1:
        return False
    dv1 = _dv_mod11(cnpj[:12], 9)
    dv2 = _dv_mod11(cnpj[:12] + str(dv1), 9)
    return cnpj[12:] == f"{dv1}{dv2}"


def formatar_cnpj(cnpj: str) -> str:
    c = so_digitos(cnpj)
    if len(c) != 14:
        return cnpj
    return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}"


def chave_valida(chave: str) -> bool:
    if not re.fullmatch(r"\d{44}", chave or ""):
        return False
    return _dv_mod11(chave[:43], 9) == int(chave[43])


@dataclass(frozen=True)
class ChaveAcesso:
    valor: str

    def __post_init__(self) -> None:
        if not chave_valida(self.valor):
            raise ValueError(f"chave de acesso inválida: {self.valor!r}")

    @property
    def cnpj_emitente(self) -> str:
        return self.valor[6:20]

    @property
    def modelo(self) -> str:
        return self.valor[20:22]

    @property
    def serie(self) -> str:
        return str(int(self.valor[22:25]))

    @property
    def numero(self) -> str:
        return str(int(self.valor[25:34]))
