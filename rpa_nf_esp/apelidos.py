"""Mapa CNPJ do emitente -> apelido do fornecedor (o que vai no nome do arquivo).

Fonte principal: docs/negocio/11-apelidos-fornecedores.md do repositório do dashboard.
- Tabela confirmada:   | Raiz CNPJ | Apelido | Razão social |
- Tabelas sugeridas:   | Apelido sugerido | Raiz CNPJ | ... |   (seção "Apelidos sugeridos")
A coluna da raiz é reconhecida pelo conteúdo (8 dígitos), não pela posição.

Precedência: extras por CNPJ completo > extras por raiz > confirmados > sugeridos.
Sem apelido, vale a razão social do emitente, como pedido.
"""

import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config, ErroConfiguracao
from .documentos import so_digitos

_RAIZ = re.compile(r"\d{8}")
_MARCAS_MD = re.compile(r"[*`]|\[CONFIRMAR\]", re.IGNORECASE)


@dataclass(frozen=True)
class Resolucao:
    nome: str
    origem: str  # "extra (CNPJ)", "extra (raiz)", "confirmado", "sugerido", "razão social"


def _limpar(celula: str) -> str:
    return re.sub(r"\s+", " ", _MARCAS_MD.sub("", celula)).strip()


def ler_markdown(texto: str) -> tuple[dict[str, str], dict[str, str]]:
    """Devolve (confirmados, sugeridos), ambos raiz -> apelido."""
    confirmados: dict[str, str] = {}
    sugeridos: dict[str, str] = {}
    em_sugeridos = False
    for linha in texto.splitlines():
        s = linha.strip()
        if s.startswith("#"):
            # A seção vem do título de nível 1-2; subtítulos (###) herdam.
            if len(s) - len(s.lstrip("#")) <= 2:
                em_sugeridos = "sugerid" in s.lower()
            continue
        if not s.startswith("|"):
            continue
        celulas = [c.strip() for c in s.strip("|").split("|")]
        if len(celulas) < 2:
            continue
        if _RAIZ.fullmatch(celulas[0]):
            raiz, apelido = celulas[0], celulas[1]
        elif _RAIZ.fullmatch(celulas[1]):
            raiz, apelido = celulas[1], celulas[0]
        else:
            continue  # cabeçalho, separador ou linha sem raiz
        apelido = _limpar(apelido)
        if not apelido:
            continue
        destino = sugeridos if em_sugeridos else confirmados
        destino.setdefault(raiz, apelido)
    return confirmados, sugeridos


def ler_extras(caminho: Path) -> dict[str, str]:
    """CSV `cnpj;apelido;observacao` com CNPJ completo (14) ou raiz (8)."""
    extras: dict[str, str] = {}
    if not caminho.is_file():
        return extras
    with caminho.open(encoding="utf-8-sig", newline="") as f:
        for i, linha in enumerate(csv.DictReader(f, delimiter=";"), start=2):
            cnpj = so_digitos(linha.get("cnpj") or "")
            apelido = (linha.get("apelido") or "").strip()
            if len(cnpj) not in (8, 14) or not apelido:
                raise ErroConfiguracao(f"{caminho.name}, linha {i}: informe CNPJ (8 ou 14 dígitos) e apelido")
            extras[cnpj] = apelido
    return extras


class MapaApelidos:
    def __init__(self, confirmados: dict[str, str], sugeridos: dict[str, str], extras: dict[str, str]):
        self._confirmados = confirmados
        self._sugeridos = sugeridos
        self._extras = extras

    def resolver(self, cnpj_emitente: str, razao_social: str) -> Resolucao:
        cnpj = so_digitos(cnpj_emitente)
        raiz = cnpj[:8]
        if cnpj in self._extras:
            return Resolucao(self._extras[cnpj], "extra (CNPJ)")
        if raiz in self._extras:
            return Resolucao(self._extras[raiz], "extra (raiz)")
        if raiz in self._confirmados:
            return Resolucao(self._confirmados[raiz], "confirmado")
        if raiz in self._sugeridos:
            return Resolucao(self._sugeridos[raiz], "sugerido")
        return Resolucao(razao_social.strip(), "razão social")

    def __len__(self) -> int:
        return len(self._confirmados) + len(self._sugeridos) + len(self._extras)


def carregar(cfg: Config, log: logging.Logger) -> MapaApelidos:
    texto = None
    try:
        texto = cfg.apelidos_fonte.read_text(encoding="utf-8")
        if not cfg.apelidos_copia.is_file() or cfg.apelidos_copia.read_text(encoding="utf-8") != texto:
            cfg.apelidos_copia.parent.mkdir(parents=True, exist_ok=True)
            cfg.apelidos_copia.write_text(texto, encoding="utf-8")
            log.info("Cópia local dos apelidos atualizada a partir de %s", cfg.apelidos_fonte)
    except OSError as e:
        log.warning("Fonte de apelidos indisponível (%s); usando a cópia local %s", e, cfg.apelidos_copia)
        try:
            texto = cfg.apelidos_copia.read_text(encoding="utf-8")
        except OSError as e2:
            raise ErroConfiguracao(f"nenhuma lista de apelidos legível: {e2}") from e2

    confirmados, sugeridos = ler_markdown(texto)
    if not confirmados:
        raise ErroConfiguracao("a lista de apelidos não tem nenhuma linha válida na tabela confirmada")
    if not cfg.apelidos_usar_sugeridos:
        sugeridos = {}
    extras = ler_extras(cfg.apelidos_extras)
    log.info(
        "Apelidos carregados: %d confirmados, %d sugeridos, %d extras",
        len(confirmados), len(sugeridos), len(extras),
    )
    return MapaApelidos(confirmados, sugeridos, extras)
