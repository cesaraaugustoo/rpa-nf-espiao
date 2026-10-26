"""Conferência (somente leitura): o que o Espião lista x o que está nas pastas, por empresa.

Para cada empresa, pesquisa os últimos N dias no Espião e, para cada nota, procura a chave de
acesso dentro dos PDFs das pastas de mês das 4 empresas. Não baixa, não move e não grava nada
nas pastas de notas; o resultado vai para o log e para logs/conferencia_AAAA-MM-DD_HHMM.txt.

Situações de cada nota:
    OK              está na pasta certa (mês da emissão, empresa destinatária)
    FALTANDO        não está em nenhuma pasta
    FORA DO LUGAR   está só na pasta de outra empresa ou de outro mês
    DUPLICADA       a mesma nota está em mais de um arquivo
    SEM CONFIRMAÇÃO há um PDF sem texto (digitalizado) com o nome esperado; provavelmente é ela
    SEM XML         o Espião ainda não tem o XML, então não há DANFE (não é erro)
    CANCELADA       cancelada na SEFAZ (não é baixada)

Também confere cada arquivo "_RPA": se a nota dele pertence àquela empresa e se o nome bate com
o padrão (apelido + data de emissão). Problemas que envolvem o RPA são marcados [RPA]; os
demais (arquivos salvos à mão, notas anteriores ao início do RPA) aparecem como observação.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .apelidos import MapaApelidos
from .config import Config, Empresa
from .espiao import ALCANCE_MAXIMO_DIAS, NotaEspiao, resumir_erro, sessao_espiao
from .ledger import Ledger
from .nomes import base_nome, normalizar
from .pastas import GerenciadorPastas
from .pdf import IndiceChaves

PROBLEMAS = ("FALTANDO", "FORA DO LUGAR", "DUPLICADA", "NOME DIVERGENTE", "RPA EM PASTA ERRADA")


@dataclass
class Achado:
    empresa: str
    situacao: str
    nota: NotaEspiao | None
    detalhe: str
    do_rpa: bool = False  # o problema envolve trabalho do RPA (e não um arquivo salvo à mão)

    @property
    def problema(self) -> bool:
        return self.situacao in PROBLEMAS


class Conferencia:
    def __init__(self, cfg: Config, log: logging.Logger, ledger: Ledger, apelidos: MapaApelidos,
                 hoje: date, dias: int):
        self.cfg = cfg
        self.log = log
        self.apelidos = apelidos
        self.hoje = hoje
        self.dias = min(dias, ALCANCE_MAXIMO_DIAS)
        self.pastas = GerenciadorPastas(cfg.raiz_notas, cfg.empresas, log, ensaio=True)
        self.ledger = ledger
        self.indice = IndiceChaves(ledger, log)

    def executar(self, empresas: tuple[Empresa, ...]) -> tuple[bool, Path]:
        desde = self.hoje - timedelta(days=self.dias)
        notas_por_empresa: dict[str, list[NotaEspiao]] = {}
        self.nao_conferidas: dict[str, str] = {}
        with sessao_espiao(self.cfg, self.log) as espiao:
            for e in empresas:
                try:
                    notas_por_empresa[e.cnpj] = espiao.pesquisar_com_retentativa(e, desde, self.hoje)
                except Exception as erro:
                    self.log.error("Empresa %s NÃO CONFERIDA: %s", e.rotulo, resumir_erro(erro))
                    self.nao_conferidas[e.cnpj] = resumir_erro(erro)
                    notas_por_empresa[e.cnpj] = []
                    try:
                        espiao.recuperar()
                    except Exception:
                        pass

        meses = sorted({(n.emissao.year, n.emissao.month) for ns in notas_por_empresa.values() for n in ns}
                       | {(self.hoje.year, self.hoje.month)})
        arquivos_por_chave, pasta_de = self._indexar(meses)
        dona = {n.chave: e for e in empresas for n in notas_por_empresa[e.cnpj]}

        achados: list[Achado] = []
        for empresa in empresas:
            notas = notas_por_empresa[empresa.cnpj]
            for nota in sorted(notas, key=lambda n: (n.emissao, n.emitente_nome)):
                achados.append(self._situacao(empresa, nota, arquivos_por_chave, pasta_de))
            achados += self._conferir_arquivos_rpa(empresa, notas, meses, dona)

        return self._relatorio(empresas, notas_por_empresa, achados, desde)

    # ----------------------------------------------------------------- índice

    def _indexar(self, meses) -> tuple[dict[str, list[Path]], dict[Path, tuple[str, int, int]]]:
        """chave -> arquivos onde aparece; pasta -> (cnpj da empresa, ano, mês)."""
        arquivos_por_chave: dict[str, list[Path]] = defaultdict(list)
        pasta_de: dict[Path, tuple[str, int, int]] = {}
        for empresa in self.cfg.empresas:
            for ano, mes in meses:
                pasta = self.pastas.localizar(empresa, ano, mes, self.hoje)
                if not pasta:
                    continue
                pasta_de[pasta] = (empresa.cnpj, ano, mes)
                for arq in sorted(pasta.iterdir()):
                    if arq.is_file() and arq.suffix.lower() == ".pdf":
                        for chave in self.indice.chaves_do_arquivo(arq):
                            arquivos_por_chave[chave].append(arq)
        return arquivos_por_chave, pasta_de

    def _eh_do_rpa(self, arq: Path) -> bool:
        return normalizar(arq.stem).endswith(normalizar(self.cfg.sufixo_rpa))

    # --------------------------------------------------------------- situação

    def _inicio_responsabilidade(self, empresa: Empresa) -> date:
        """Notas emitidas a partir deste dia são responsabilidade do RPA: desde a primeira pesquisa
        completa da empresa (com a margem de dias_retroativos). Sem histórico, hoje e ontem."""
        cobertura = self.ledger.cobertura(empresa.cnpj)
        inicio = cobertura[0] if cobertura else self.hoje
        return inicio - timedelta(days=self.cfg.dias_retroativos)

    def _situacao(self, empresa, nota, arquivos_por_chave, pasta_de) -> Achado:
        arquivos = arquivos_por_chave.get(nota.chave, [])
        certos = [a for a in arquivos
                  if pasta_de.get(a.parent) == (empresa.cnpj, nota.emissao.year, nota.emissao.month)]
        nomes = ", ".join(f"{a.parent.name}\\{a.name}" for a in arquivos)
        envolve_rpa = any(self._eh_do_rpa(a) for a in arquivos)
        if len(arquivos) > 1:
            return Achado(empresa.pasta, "DUPLICADA", nota, nomes, do_rpa=envolve_rpa)
        if certos:
            return Achado(empresa.pasta, "OK", nota, certos[0].name)
        if arquivos:
            return Achado(empresa.pasta, "FORA DO LUGAR", nota, nomes, do_rpa=envolve_rpa)
        if nota.cancelada:
            return Achado(empresa.pasta, "CANCELADA", nota, nota.situacao)
        if not nota.tem_xml:
            return Achado(empresa.pasta, "SEM XML", nota, "o RPA baixa quando o XML chegar")
        pasta = self.pastas.localizar(empresa, nota.emissao.year, nota.emissao.month, self.hoje)
        if pasta:
            apelido = self.apelidos.resolver(nota.chave[6:20], nota.emitente_nome).nome
            esperado = normalizar(base_nome(apelido, nota.emissao))
            provaveis = [n for n in self.indice.ilegiveis_na_pasta(pasta)
                         if normalizar(Path(n).stem).startswith(esperado)]
            if provaveis:
                return Achado(empresa.pasta, "SEM CONFIRMAÇÃO", nota,
                              f"provavelmente em '{', '.join(provaveis)}' (PDF sem texto, chave ilegível)")
        if nota.emissao >= self._inicio_responsabilidade(empresa):
            detalhe = "período coberto pelo RPA: será baixada na próxima execução (ou falhou, veja o log)"
            return Achado(empresa.pasta, "FALTANDO", nota, detalhe, do_rpa=True)
        return Achado(empresa.pasta, "FALTANDO", nota, "anterior ao início do RPA: baixar à mão")

    def _conferir_arquivos_rpa(self, empresa: Empresa, notas: list[NotaEspiao], meses, dona) -> list[Achado]:
        achados = []
        por_chave = {n.chave: n for n in notas}
        for ano, mes in meses:
            pasta = self.pastas.localizar(empresa, ano, mes, self.hoje)
            if not pasta:
                continue
            for arq in sorted(pasta.iterdir()):
                if not (arq.is_file() and arq.suffix.lower() == ".pdf" and self._eh_do_rpa(arq)):
                    continue
                chaves = self.indice.chaves_do_arquivo(arq)
                nota = next((por_chave[c] for c in chaves if c in por_chave), None)
                if nota is None:
                    outra = next((dona[c] for c in chaves if c in dona), None)
                    if outra is not None:
                        achados.append(Achado(empresa.pasta, "RPA EM PASTA ERRADA", None,
                                              f"{pasta.name}\\{arq.name}: a nota é de {outra.pasta}", do_rpa=True))
                    continue
                apelido = self.apelidos.resolver(nota.chave[6:20], nota.emitente_nome).nome
                esperado = normalizar(base_nome(apelido, nota.emissao))
                if not normalizar(arq.stem).startswith(esperado):
                    achados.append(Achado(empresa.pasta, "NOME DIVERGENTE", nota,
                                          f"{arq.name} (esperado começar com '{esperado}')", do_rpa=True))
        return achados

    # -------------------------------------------------------------- relatório

    def _relatorio(self, empresas, notas_por_empresa, achados: list[Achado], desde: date) -> tuple[bool, Path]:
        linhas = [
            f"CONFERÊNCIA ESPIÃO x PASTAS - {datetime.now():%d/%m/%Y %H:%M}",
            f"Período pesquisado: {desde:%d/%m/%Y} a {self.hoje:%d/%m/%Y}",
            "",
        ]
        for empresa in empresas:
            meus = [a for a in achados if a.empresa == empresa.pasta]
            contagem: dict[str, int] = defaultdict(int)
            for a in meus:
                contagem[a.situacao] += 1
            if empresa.cnpj in self.nao_conferidas:
                linhas.append(f"=== {empresa.pasta} ({empresa.cnpj}) - NÃO CONFERIDA: {self.nao_conferidas[empresa.cnpj]}")
                linhas.append("")
                continue
            total = len(notas_por_empresa[empresa.cnpj])
            linhas.append(f"=== {empresa.pasta} ({empresa.cnpj}) - {total} nota(s) no Espião")
            linhas.append("    " + (" | ".join(f"{k}: {v}" for k, v in sorted(contagem.items())) or "nenhuma nota"))
            for a in meus:
                if a.situacao == "OK":
                    continue
                n = a.nota
                ident = f"{n.emissao:%d/%m/%Y} NF {n.numero:>9} {n.emitente_nome[:40]:<40}" if n else ""
                marca = "  [RPA]" if a.do_rpa else ""
                linhas.append(f"    {a.situacao:<19} {ident} {a.detalhe}{marca}")
                if n:
                    linhas.append(f"    {'':<19} chave {n.chave}")
            linhas.append("")

        problemas = [a for a in achados if a.problema]
        do_rpa = [a for a in problemas if a.do_rpa]
        ok = not do_rpa and not self.nao_conferidas
        if self.nao_conferidas:
            resultado = f"INCOMPLETA - {len(self.nao_conferidas)} empresa(s) não conferida(s); rode de novo."
        elif ok:
            resultado = "OK - tudo o que é responsabilidade do RPA está correto."
        else:
            resultado = f"ATENÇÃO - {len(do_rpa)} problema(s) envolvendo o RPA (marcados com [RPA])."
        linhas.append("RESULTADO: " + resultado)
        manuais = len(problemas) - len(do_rpa)
        if manuais:
            linhas.append(f"Obs.: {manuais} problema(s) fora do alcance do RPA (arquivos salvos à mão ou notas "
                          "anteriores ao início do RPA). Estão listados acima, sem a marca [RPA].")

        self.cfg.dir_logs.mkdir(parents=True, exist_ok=True)
        caminho = self.cfg.dir_logs / f"conferencia_{datetime.now():%Y-%m-%d_%H%M}.txt"
        caminho.write_text("\n".join(linhas) + "\n", encoding="utf-8")
        for linha in linhas:
            self.log.info(linha)
        return ok, caminho
