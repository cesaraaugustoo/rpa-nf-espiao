"""Registro persistente (SQLite) de cada nota tratada, das execuções e do índice de PDFs.

É a memória do RPA entre execuções: garante que a mesma NF não seja baixada duas vezes
e controla as novas tentativas de quem falhou.

Ciclo de vida de uma nota (coluna status):

    PENDENTE        falhou; é tentada de novo nas próximas execuções
    AGUARDANDO_XML  a SEFAZ ainda só entregou o resumo; não há DANFE para baixar
    BAIXADA         salva pelo RPA na pasta do mês              (final)
    JA_EXISTIA      o PDF já estava na pasta (baixado à mão)     (final)
    CANCELADA       NF cancelada na SEFAZ, ignorada              (final)
    DESISTIDA       esgotou as tentativas; precisa de ação manual (final, reversível com --reprocessar)

XML de uma nota BAIXADA (salvo ao lado do PDF, com o mesmo nome), coluna xml:

    NULL            ainda não salvo; tentado de novo nas próximas execuções (xml_falhas conta as falhas)
    caminho         salvo
    ''              desistido (tentativas esgotadas ou PDF não encontrado; motivo em xml_erro)
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path


class Status(StrEnum):
    PENDENTE = "PENDENTE"
    AGUARDANDO_XML = "AGUARDANDO_XML"
    BAIXADA = "BAIXADA"
    JA_EXISTIA = "JA_EXISTIA"
    CANCELADA = "CANCELADA"
    DESISTIDA = "DESISTIDA"


EM_ABERTO = (Status.PENDENTE, Status.AGUARDANDO_XML)
CONCLUIDAS = (Status.BAIXADA, Status.JA_EXISTIA)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS execucoes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    inicio      TEXT NOT NULL,
    fim         TEXT,
    ensaio      INTEGER NOT NULL DEFAULT 0,
    situacao    TEXT,
    resumo      TEXT
);
CREATE TABLE IF NOT EXISTS notas (
    chave             TEXT PRIMARY KEY,
    empresa_cnpj      TEXT NOT NULL,
    emitente_cnpj     TEXT,
    emitente_nome     TEXT,
    numero            TEXT,
    serie             TEXT,
    emissao           TEXT NOT NULL,
    valor             TEXT,
    situacao_sefaz    TEXT,
    status            TEXT NOT NULL,
    falhas            INTEGER NOT NULL DEFAULT 0,
    ultimo_erro       TEXT,
    arquivo           TEXT,
    primeira_execucao INTEGER,
    ultima_execucao   INTEGER,
    criado_em         TEXT NOT NULL,
    atualizado_em     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_notas_status ON notas (status, empresa_cnpj);
CREATE TABLE IF NOT EXISTS cobertura (
    empresa_cnpj  TEXT PRIMARY KEY,
    desde         TEXT NOT NULL,   -- dia da primeira pesquisa completa (início da responsabilidade do RPA)
    ate           TEXT NOT NULL,   -- dia da última pesquisa completa
    atualizado_em TEXT NOT NULL
);
DROP TABLE IF EXISTS indice_pdf;  -- v1 indexava também chaves citadas de outras notas
CREATE TABLE IF NOT EXISTS indice_pdf_v2 (
    caminho   TEXT PRIMARY KEY,
    tamanho   INTEGER NOT NULL,
    mtime_ns  INTEGER NOT NULL,
    chaves    TEXT NOT NULL
);
"""

# Colunas acrescentadas depois da v1.3.0: criadas no banco existente na primeira abertura.
_COLUNAS_NOTAS = {"xml": "TEXT", "xml_falhas": "INTEGER NOT NULL DEFAULT 0", "xml_erro": "TEXT"}


@dataclass(frozen=True)
class RegistroNota:
    chave: str
    empresa_cnpj: str
    emitente_cnpj: str | None
    emitente_nome: str | None
    numero: str | None
    emissao: date
    status: Status
    falhas: int
    ultimo_erro: str | None
    arquivo: str | None
    xml: str | None = None


@dataclass(frozen=True)
class DadosNota:
    """O que o ledger precisa saber de uma nota para registrá-la."""

    chave: str
    empresa_cnpj: str
    emitente_cnpj: str
    emitente_nome: str
    numero: str
    serie: str | None
    emissao: date
    valor: str | None
    situacao_sefaz: str | None


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Ledger:
    def __init__(self, caminho: Path):
        caminho.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(caminho, timeout=30)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.executescript(_SCHEMA)
        existentes = {l["name"] for l in self._con.execute("PRAGMA table_info(notas)")}
        with self._con:
            for nome, tipo in _COLUNAS_NOTAS.items():
                if nome not in existentes:
                    self._con.execute(f"ALTER TABLE notas ADD COLUMN {nome} {tipo}")
        self.execucao_id: int | None = None

    def fechar(self) -> None:
        self._con.close()

    # ------------------------------------------------------------------ execuções

    def iniciar_execucao(self, ensaio: bool) -> int:
        with self._con:
            cur = self._con.execute(
                "INSERT INTO execucoes (inicio, ensaio) VALUES (?, ?)", (_agora(), int(ensaio))
            )
        self.execucao_id = cur.lastrowid
        return self.execucao_id

    def finalizar_execucao(self, situacao: str, resumo: dict) -> None:
        with self._con:
            self._con.execute(
                "UPDATE execucoes SET fim = ?, situacao = ?, resumo = ? WHERE id = ?",
                (_agora(), situacao, json.dumps(resumo, ensure_ascii=False), self.execucao_id),
            )

    def _execucoes(self, onde: str, parametros: tuple, ordem: str, limite: int = -1) -> list[dict]:
        linhas = self._con.execute(
            f"SELECT id, inicio, fim, situacao, resumo FROM execucoes WHERE ensaio = 0 AND {onde} "
            f"ORDER BY id {ordem} LIMIT ?", (*parametros, limite),
        ).fetchall()
        return [{**dict(l), "resumo": json.loads(l["resumo"]) if l["resumo"] else None} for l in linhas]

    def execucoes_anteriores(self, limite: int) -> list[dict]:
        """Execuções reais já finalizadas antes da atual, da mais recente para a mais antiga."""
        return self._execucoes("fim IS NOT NULL AND id < ?", (self.execucao_id or 1 << 62,), "DESC", limite)

    def execucoes_do_dia(self, dia: date) -> list[dict]:
        """Execuções reais iniciadas no dia, em ordem (as interrompidas têm situacao None)."""
        return self._execucoes("inicio LIKE ?", (f"{dia.isoformat()}%",), "ASC")

    # ---------------------------------------------------------------------- notas

    @staticmethod
    def _registro(linha: sqlite3.Row) -> RegistroNota:
        return RegistroNota(
            chave=linha["chave"],
            empresa_cnpj=linha["empresa_cnpj"],
            emitente_cnpj=linha["emitente_cnpj"],
            emitente_nome=linha["emitente_nome"],
            numero=linha["numero"],
            emissao=date.fromisoformat(linha["emissao"]),
            status=Status(linha["status"]),
            falhas=linha["falhas"],
            ultimo_erro=linha["ultimo_erro"],
            arquivo=linha["arquivo"],
            xml=linha["xml"],
        )

    def obter(self, chave: str) -> RegistroNota | None:
        linha = self._con.execute("SELECT * FROM notas WHERE chave = ?", (chave,)).fetchone()
        return self._registro(linha) if linha else None

    def em_aberto(self, empresa_cnpj: str | None = None) -> list[RegistroNota]:
        sql = "SELECT * FROM notas WHERE status IN (?, ?)"
        params: list = [s.value for s in EM_ABERTO]
        if empresa_cnpj:
            sql += " AND empresa_cnpj = ?"
            params.append(empresa_cnpj)
        return [self._registro(l) for l in self._con.execute(sql + " ORDER BY emissao", params)]

    def listar(self, status: tuple[Status, ...]) -> list[RegistroNota]:
        marcadores = ",".join("?" * len(status))
        sql = f"SELECT * FROM notas WHERE status IN ({marcadores}) ORDER BY empresa_cnpj, emissao"
        return [self._registro(l) for l in self._con.execute(sql, [s.value for s in status])]

    def _gravar(self, dados: DadosNota, status: Status, *, falhas: int, erro: str | None, arquivo: str | None) -> None:
        agora = _agora()
        with self._con:
            self._con.execute(
                """
                INSERT INTO notas (chave, empresa_cnpj, emitente_cnpj, emitente_nome, numero, serie, emissao,
                                   valor, situacao_sefaz, status, falhas, ultimo_erro, arquivo,
                                   primeira_execucao, ultima_execucao, criado_em, atualizado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (chave) DO UPDATE SET
                    emitente_cnpj = excluded.emitente_cnpj, emitente_nome = excluded.emitente_nome,
                    numero = excluded.numero, serie = excluded.serie, emissao = excluded.emissao,
                    valor = excluded.valor, situacao_sefaz = excluded.situacao_sefaz,
                    status = excluded.status, falhas = excluded.falhas, ultimo_erro = excluded.ultimo_erro,
                    arquivo = COALESCE(excluded.arquivo, notas.arquivo),
                    ultima_execucao = excluded.ultima_execucao, atualizado_em = excluded.atualizado_em
                """,
                (
                    dados.chave, dados.empresa_cnpj, dados.emitente_cnpj, dados.emitente_nome,
                    dados.numero, dados.serie, dados.emissao.isoformat(), dados.valor, dados.situacao_sefaz,
                    status.value, falhas, erro, arquivo,
                    self.execucao_id, self.execucao_id, agora, agora,
                ),
            )

    def marcar(self, dados: DadosNota, status: Status, *, arquivo: str | None = None, erro: str | None = None) -> None:
        anterior = self.obter(dados.chave)
        self._gravar(dados, status, falhas=anterior.falhas if anterior else 0, erro=erro, arquivo=arquivo)

    def registrar_falha(self, dados: DadosNota, motivo: str, max_retentativas: int) -> RegistroNota:
        """Conta uma tentativa malsucedida. A 1ª falha + `max_retentativas` novas tentativas; depois, DESISTIDA."""
        anterior = self.obter(dados.chave)
        falhas = (anterior.falhas if anterior else 0) + 1
        status = Status.DESISTIDA if falhas > max_retentativas else Status.PENDENTE
        self._gravar(dados, status, falhas=falhas, erro=motivo[:1000], arquivo=None)
        return self.obter(dados.chave)

    def registrar_falha_por_chave(self, chave: str, motivo: str, max_retentativas: int) -> RegistroNota | None:
        r = self.obter(chave)
        if r is None:
            return None
        dados = DadosNota(
            chave=r.chave, empresa_cnpj=r.empresa_cnpj, emitente_cnpj=r.emitente_cnpj or "",
            emitente_nome=r.emitente_nome or "", numero=r.numero or "", serie=None,
            emissao=r.emissao, valor=None, situacao_sefaz=None,
        )
        return self.registrar_falha(dados, motivo, max_retentativas)

    def encerrar(self, chave: str, motivo: str) -> None:
        """Marca DESISTIDA sem contar tentativa (ex.: XML que nunca chegou, nota fora do alcance)."""
        with self._con:
            self._con.execute(
                "UPDATE notas SET status = ?, ultimo_erro = ?, ultima_execucao = ?, atualizado_em = ? WHERE chave = ?",
                (Status.DESISTIDA.value, motivo[:1000], self.execucao_id, _agora(), chave),
            )

    def reprocessar(self, chave: str) -> bool:
        """Devolve uma nota DESISTIDA (ou qualquer outra) para a fila, zerando as falhas."""
        with self._con:
            cur = self._con.execute(
                "UPDATE notas SET status = ?, falhas = 0, ultimo_erro = 'reprocessamento manual', atualizado_em = ? "
                "WHERE chave = ?",
                (Status.PENDENTE.value, _agora(), chave),
            )
        return cur.rowcount > 0

    # -------------------------------------------------------------------- XML

    def xml_pendentes(self, empresa_cnpj: str, desde: date) -> list[RegistroNota]:
        """Notas baixadas pelo RPA, emitidas a partir de `desde`, cujo XML ainda não foi salvo."""
        sql = ("SELECT * FROM notas WHERE status = ? AND xml IS NULL AND empresa_cnpj = ? AND emissao >= ? "
               "ORDER BY emissao")
        params = (Status.BAIXADA.value, empresa_cnpj, desde.isoformat())
        return [self._registro(l) for l in self._con.execute(sql, params)]

    def marcar_xml(self, chave: str, caminho: str) -> None:
        self._atualizar_xml(chave, "xml = ?, xml_erro = NULL", (caminho,))

    def registrar_falha_xml(self, chave: str, motivo: str) -> int:
        """Conta uma tentativa malsucedida de baixar o XML e devolve o total de falhas."""
        self._atualizar_xml(chave, "xml_falhas = xml_falhas + 1, xml_erro = ?", (motivo[:1000],))
        return self._con.execute("SELECT xml_falhas FROM notas WHERE chave = ?", (chave,)).fetchone()[0]

    def desistir_xml(self, chave: str, motivo: str) -> None:
        self._atualizar_xml(chave, "xml = '', xml_erro = ?", (motivo[:1000],))

    def _atualizar_xml(self, chave: str, campos: str, valores: tuple) -> None:
        with self._con:
            self._con.execute(f"UPDATE notas SET {campos}, atualizado_em = ? WHERE chave = ?",
                              (*valores, _agora(), chave))

    # -------------------------------------------------------------- cobertura

    def cobertura(self, empresa_cnpj: str) -> tuple[date, date] | None:
        """(primeiro dia, último dia) em que a empresa foi pesquisada por completo, ou None."""
        linha = self._con.execute(
            "SELECT desde, ate FROM cobertura WHERE empresa_cnpj = ?", (empresa_cnpj,)
        ).fetchone()
        return (date.fromisoformat(linha["desde"]), date.fromisoformat(linha["ate"])) if linha else None

    def registrar_cobertura(self, empresa_cnpj: str, dia: date) -> None:
        with self._con:
            self._con.execute(
                """
                INSERT INTO cobertura (empresa_cnpj, desde, ate, atualizado_em) VALUES (?, ?, ?, ?)
                ON CONFLICT (empresa_cnpj) DO UPDATE SET
                    ate = MAX(cobertura.ate, excluded.ate), atualizado_em = excluded.atualizado_em
                """,
                (empresa_cnpj, dia.isoformat(), dia.isoformat(), _agora()),
            )

    # ------------------------------------------------------------- índice de PDFs

    def cache_pdf(self, caminho: str, tamanho: int, mtime_ns: int) -> list[str] | None:
        linha = self._con.execute(
            "SELECT chaves FROM indice_pdf_v2 WHERE caminho = ? AND tamanho = ? AND mtime_ns = ?",
            (caminho, tamanho, mtime_ns),
        ).fetchone()
        return json.loads(linha["chaves"]) if linha else None

    def salvar_cache_pdf(self, caminho: str, tamanho: int, mtime_ns: int, chaves: list[str]) -> None:
        with self._con:
            self._con.execute(
                "INSERT OR REPLACE INTO indice_pdf_v2 (caminho, tamanho, mtime_ns, chaves) VALUES (?, ?, ?, ?)",
                (caminho, tamanho, mtime_ns, json.dumps(chaves)),
            )
