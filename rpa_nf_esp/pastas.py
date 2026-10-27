"""Pastas de notas fiscais: localização da pasta do mês e virada de mês/ano.

Estrutura (espelha a organização que já existe em NOTAS FISCAIS):

    NOTAS FISCAIS\\2026\\OUTUBRO 26 - LOJA CENTRO        <- mês corrente, solto no ano
    NOTAS FISCAIS\\2026\\2026\\SETEMBRO 26 - LOJA CENTRO  <- meses fechados do ano corrente
    NOTAS FISCAIS\\2025\\DEZEMBRO 25 - LOJA CENTRO        <- anos anteriores: tudo solto (achatado)

Virada de mês: os meses soltos anteriores ao corrente vão para <ano>\\<ano>\\ e as pastas
do mês novo são criadas.
Virada de ano: o que estava em <ano anterior>\\<ano anterior>\\ volta para <ano anterior>\\
(achatado, como 2024 e 2025), e nasce a pasta do ano novo com as pastas de janeiro.

Tudo é idempotente: rodar de novo não muda nada. Pastas que não são de mês (CLOUDMONITOR,
TESTE, ...) nunca são tocadas, e nada é sobrescrito: conflito de nome é registrado no log
e deixado como está para decisão manual.
"""

import logging
import os
import re
import time
from datetime import date
from pathlib import Path

from .config import Empresa
from .nomes import renomear_sem_sobrescrever, sem_acento

MESES = (
    "JANEIRO", "FEVEREIRO", "MARÇO", "ABRIL", "MAIO", "JUNHO",
    "JULHO", "AGOSTO", "SETEMBRO", "OUTUBRO", "NOVEMBRO", "DEZEMBRO",
)
_MES_POR_NOME = {sem_acento(m): i for i, m in enumerate(MESES, start=1)}
_RE_PASTA_MES = re.compile(
    r"^(" + "|".join(_MES_POR_NOME) + r")(?![A-Z])\s*(\d{2})?(?!\d)"
)


def nome_pasta_mes(ano: int, mes: int, rotulo: str) -> str:
    return f"{MESES[mes - 1]} {ano % 100:02d} - {rotulo}"


def chave_comparacao(nome: str) -> str:
    """Forma canônica para comparar nomes de pasta: sem acento, caixa, espaço ou pontuação."""
    return re.sub(r"[^A-Z0-9]", "", sem_acento(nome).upper())


def interpretar_pasta_mes(nome: str) -> tuple[int, int | None] | None:
    """'NOVEMBRO 25- LOJA CENTRO' -> (11, 25). None se não for pasta de mês."""
    m = _RE_PASTA_MES.match(sem_acento(nome).upper().strip())
    if not m:
        return None
    return _MES_POR_NOME[m.group(1)], (int(m.group(2)) if m.group(2) else None)


class GerenciadorPastas:
    def __init__(self, raiz: Path, empresas: tuple[Empresa, ...], log: logging.Logger, ensaio: bool = False):
        self.raiz = raiz
        self.empresas = empresas
        self.log = log
        self.ensaio = ensaio

    # ------------------------------------------------------------ pasta do mês

    def _pais(self, ano: int, mes: int, hoje: date) -> tuple[Path, list[Path]]:
        """(pai canônico onde criar, pais onde procurar uma pasta já existente)."""
        dir_ano = self.raiz / str(ano)
        arquivo_ano = dir_ano / str(ano)
        if (ano, mes) == (hoje.year, hoje.month):
            return dir_ano, [dir_ano, arquivo_ano]
        if ano == hoje.year:
            return arquivo_ano, [arquivo_ano, dir_ano]
        return dir_ano, [dir_ano, arquivo_ano]

    def localizar(self, empresa: Empresa, ano: int, mes: int, hoje: date) -> Path | None:
        _, candidatos = self._pais(ano, mes, hoje)
        for rotulo in (empresa.pasta, *empresa.aliases_pasta):
            alvo = chave_comparacao(nome_pasta_mes(ano, mes, rotulo))
            for pai in candidatos:
                if not pai.is_dir():
                    continue
                for d in pai.iterdir():
                    if d.is_dir() and chave_comparacao(d.name) == alvo:
                        return d
        return None

    def pasta_do_mes(self, empresa: Empresa, emissao: date, hoje: date, criar: bool = True) -> Path:
        existente = self.localizar(empresa, emissao.year, emissao.month, hoje)
        if existente:
            return existente
        pai, _ = self._pais(emissao.year, emissao.month, hoje)
        destino = pai / nome_pasta_mes(emissao.year, emissao.month, empresa.pasta)
        if criar:
            self._criar(destino)
        return destino

    # -------------------------------------------------------------- manutenção

    def manutencao(self, hoje: date) -> int:
        """Aplica a virada de ano e de mês e garante as pastas do mês corrente. Devolve nº de ações."""
        self.log.info("Verificando estrutura de pastas em %s", self.raiz)
        acoes = self._achatar_anos_anteriores(hoje)
        acoes += self._arquivar_meses_fechados(hoje)
        for empresa in self.empresas:
            if not self.localizar(empresa, hoje.year, hoje.month, hoje):
                self._criar(self.raiz / str(hoje.year) / nome_pasta_mes(hoje.year, hoje.month, empresa.pasta))
                acoes += 1
        if acoes == 0:
            self.log.info("Estrutura de pastas em dia (%s %d)", MESES[hoje.month - 1], hoje.year)
        return acoes

    def _achatar_anos_anteriores(self, hoje: date) -> int:
        acoes = 0
        for dir_ano in sorted(self.raiz.iterdir()):
            if not (dir_ano.is_dir() and re.fullmatch(r"\d{4}", dir_ano.name)):
                continue
            if int(dir_ano.name) >= hoje.year:
                continue
            interno = dir_ano / dir_ano.name
            if not interno.is_dir():
                continue
            self.log.info("Virada de ano: trazendo os meses de %s para %s", interno, dir_ano)
            for item in sorted(interno.iterdir()):
                acoes += self._mover(item, dir_ano / item.name)
            acoes += self._remover_se_vazia(interno)
        return acoes

    def _arquivar_meses_fechados(self, hoje: date) -> int:
        dir_ano = self.raiz / str(hoje.year)
        if not dir_ano.is_dir():
            self._criar(dir_ano)
            return 1
        acoes = 0
        arquivo = dir_ano / str(hoje.year)
        for item in sorted(dir_ano.iterdir()):
            if not item.is_dir():
                continue
            info = interpretar_pasta_mes(item.name)
            if info is None:
                continue
            mes, aa = info
            if aa is not None and aa != hoje.year % 100:
                self.log.warning("Pasta de mês com ano diferente de %d, deixada no lugar: %s", hoje.year, item)
                continue
            if mes < hoje.month:
                if acoes == 0:
                    self.log.info("Virada de mês: arquivando meses fechados em %s", arquivo)
                acoes += self._mover(item, arquivo / item.name)
        return acoes

    # ------------------------------------------------------------- operações

    def _criar(self, pasta: Path) -> None:
        if pasta.is_dir():
            return
        if self.ensaio:
            self.log.info("[ensaio] criaria a pasta %s", pasta)
            return
        pasta.mkdir(parents=True, exist_ok=True)
        self.log.info("Pasta criada: %s", pasta)

    def manutencao_segura(self, hoje: date) -> None:
        """Manutenção que nunca impede os downloads: um erro de disco/OneDrive vira ERRO no log
        e a virada é tentada de novo na próxima execução (ela é idempotente)."""
        try:
            self.manutencao(hoje)
        except OSError as e:
            self.log.error("Virada de mês/ano não concluída (%s). Os downloads seguem; "
                           "nova tentativa na próxima execução.", e)

    def _mover(self, origem: Path, destino: Path) -> int:
        if destino.exists():
            if origem.is_dir() and destino.is_dir():
                self.log.info("Destino já existe, mesclando conteúdo: %s -> %s", origem, destino)
                acoes = 0
                for filho in sorted(origem.iterdir()):
                    acoes += self._mover(filho, destino / filho.name)
                return acoes + self._remover_se_vazia(origem)
            self.log.warning("CONFLITO: já existe %s; mantido também %s (resolver à mão)", destino, origem)
            return 0
        if self.ensaio:
            self.log.info("[ensaio] moveria %s -> %s", origem, destino)
            return 1
        for tentativa in range(1, 4):
            try:
                destino.parent.mkdir(parents=True, exist_ok=True)
                if origem.is_dir():
                    os.rename(origem, destino)  # pasta: o destino foi conferido acima
                else:
                    renomear_sem_sobrescrever(origem, destino)
                self.log.info("Movido: %s -> %s", origem, destino)
                return 1
            except FileExistsError:
                self.log.warning("CONFLITO: %s apareceu durante a movimentação; mantido %s (resolver à mão)",
                                 destino, origem)
                return 0
            except OSError as e:
                # OneDrive/Explorer seguram arquivos por instantes durante a sincronização.
                if tentativa == 3:
                    self.log.error("Não foi possível mover %s -> %s: %s. Nova tentativa na próxima execução.",
                                   origem, destino, e)
                    return 0
                time.sleep(2 * tentativa)
        return 0

    def _remover_se_vazia(self, pasta: Path) -> int:
        if self.ensaio:
            return 0
        try:
            if pasta.is_dir() and not any(pasta.iterdir()):
                pasta.rmdir()
                self.log.info("Pasta vazia removida: %s", pasta)
                return 1
        except OSError as e:
            self.log.warning("Não foi possível remover a pasta vazia %s: %s", pasta, e)
        return 0
