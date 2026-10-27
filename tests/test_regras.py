"""Testes das regras de negócio que não dependem do site. Todos os dados são fictícios."""

import base64
import json
import logging
import tomllib
from datetime import date
from pathlib import Path

import pytest
from conftest import (
    CNPJ_ACME, CNPJ_ACME_FILIAL, CNPJ_GASBOM, CNPJ_LOJA_CENTRO, CNPJ_LOJA_NORTE, gerar_chave,
)

from rpa_nf_esp.apelidos import MapaApelidos, ler_markdown
from rpa_nf_esp.config import RAIZ_PROJETO, Empresa
from rpa_nf_esp.documentos import ChaveAcesso, chave_valida, cnpj_valido
from rpa_nf_esp.espiao import decodificar_resposta, nota_da_api
from rpa_nf_esp.ledger import DadosNota, Ledger, Status
from rpa_nf_esp.nomes import (
    base_nome,
    gravar_sem_sobrescrever,
    limpar_para_arquivo,
    proximo_nome,
    renomear_sem_sobrescrever,
)
from rpa_nf_esp.pastas import GerenciadorPastas, interpretar_pasta_mes
from rpa_nf_esp.pdf import PdfInvalido, chaves_proprias, validar_danfe

CHAVE = gerar_chave(CNPJ_ACME_FILIAL, 2830984)
LOG = logging.getLogger("teste")

EMPRESAS = (
    Empresa(CNPJ_LOJA_CENTRO, "Centro", "EMPRESA EXEMPLO CENTRO LTDA", "LOJA CENTRO"),
    Empresa(CNPJ_LOJA_NORTE, "Norte", "EMPRESA EXEMPLO NORTE LTDA", "LOJA NORTE", ("LOJA NORTE LTDA",)),
)


# --------------------------------------------------------------- documentos

def test_chave_e_cnpj():
    assert chave_valida(CHAVE)
    assert not chave_valida(CHAVE[:-1] + str((int(CHAVE[-1]) + 1) % 10))
    ch = ChaveAcesso(CHAVE)
    assert ch.cnpj_emitente == CNPJ_ACME_FILIAL
    assert ch.numero == "2830984"
    assert cnpj_valido(CNPJ_LOJA_CENTRO) and not cnpj_valido(CNPJ_LOJA_CENTRO[:-1] + "0")


# ----------------------------------------------------------------- config

def test_config_de_exemplo_e_valida():
    dados = tomllib.loads((RAIZ_PROJETO / "config" / "config.example.toml").read_text(encoding="utf-8"))
    assert {"espiao", "execucao", "pastas", "apelidos", "empresas"} <= dados.keys()
    assert all(cnpj_valido(e["cnpj"]) for e in dados["empresas"])


# ----------------------------------------------------------------- apelidos

def test_apelidos_do_modelo_documentado():
    texto = (RAIZ_PROJETO / "config" / "apelidos.example.md").read_text(encoding="utf-8")
    confirmados, sugeridos = ler_markdown(texto)
    assert confirmados == {"99887766": "ACME", "12312312": "CAFÉ BOM"}
    assert sugeridos == {"55443322": "GASBOM"}   # subtítulo ### herda a seção de sugeridos


def test_precedencia_dos_apelidos():
    mapa = MapaApelidos({"99887766": "ACME"}, {"55443322": "GASBOM"}, {CNPJ_ACME_FILIAL: "ACME FILIAL"})
    assert mapa.resolver(CNPJ_ACME_FILIAL, "ACME ...").nome == "ACME FILIAL"
    assert mapa.resolver(CNPJ_ACME, "ACME ...").nome == "ACME"
    assert mapa.resolver(CNPJ_GASBOM, "GASBOM DISTRIBUIDORA S A").origem == "sugerido"
    r = mapa.resolver("12345678000199", "FORNECEDOR NOVO LTDA")
    assert (r.nome, r.origem) == ("FORNECEDOR NOVO LTDA", "razão social")


# -------------------------------------------------------------------- nomes

def test_nome_e_sequencia(tmp_path: Path):
    dia = date(2026, 10, 23)
    base = base_nome("CAFÉ BOM", dia)
    assert base == "CAFÉ BOM 231026"
    assert proximo_nome(tmp_path, base, "_RPA") == "CAFÉ BOM 231026_RPA.pdf"
    (tmp_path / "CAFE BOM 231026.pdf").write_bytes(b"x")            # salvo à mão, sem acento
    assert proximo_nome(tmp_path, base, "_RPA") == "CAFÉ BOM 231026 2_RPA.pdf"
    (tmp_path / "CAFÉ BOM 231026 2_RPA.pdf").write_bytes(b"x")
    assert proximo_nome(tmp_path, base, "_RPA") == "CAFÉ BOM 231026 3_RPA.pdf"
    (tmp_path / "ACME 231026.pdf").write_bytes(b"x")                # outro fornecedor não conta
    assert proximo_nome(tmp_path, base_nome("GASBOM", dia), "_RPA") == "GASBOM 231026_RPA.pdf"


def test_nome_limpo_para_windows():
    assert limpar_para_arquivo("DISTRIBUIDORA S/A") == "DISTRIBUIDORA S-A"
    assert limpar_para_arquivo('A:B*C?"D<E>F|G.') == "ABCDEFG"
    assert limpar_para_arquivo("ACME ALIMENTOS LTDA.") == "ACME ALIMENTOS LTDA"
    assert limpar_para_arquivo("CON") == "FORNECEDOR CON"


def test_gravacao_nunca_sobrescreve(tmp_path: Path):
    origem = tmp_path / "origem.pdf"
    origem.write_bytes(b"novo")
    pasta = tmp_path / "destino"
    pasta.mkdir()
    (pasta / "ACME 231026_RPA.pdf").write_bytes(b"antigo")
    destino = gravar_sem_sobrescrever(origem, pasta, "ACME 231026", "_RPA")
    assert destino.name == "ACME 231026 2_RPA.pdf"
    assert (pasta / "ACME 231026_RPA.pdf").read_bytes() == b"antigo"
    assert not list(pasta.glob(".~rpa-*"))


def test_renomear_recusa_destino_existente(tmp_path: Path):
    # No Linux (VPS) o rename comum substituiria o destino sem avisar.
    origem, destino = tmp_path / "novo.pdf", tmp_path / "ACME 231026_RPA.pdf"
    origem.write_bytes(b"novo")
    destino.write_bytes(b"antigo")
    with pytest.raises(FileExistsError):
        renomear_sem_sobrescrever(origem, destino)
    assert destino.read_bytes() == b"antigo"
    assert origem.read_bytes() == b"novo"


def test_temporario_nao_fica_na_pasta_se_a_copia_falhar(tmp_path: Path, monkeypatch):
    origem = tmp_path / "o.pdf"
    origem.write_bytes(b"x")
    pasta = tmp_path / "destino"

    def copia_parcial(_o, d):
        Path(d).write_bytes(b"pela metade")
        raise OSError("disco cheio")

    monkeypatch.setattr("rpa_nf_esp.nomes.shutil.copyfile", copia_parcial)
    with pytest.raises(OSError, match="disco cheio"):
        gravar_sem_sobrescrever(origem, pasta, "ACME 231026", "_RPA")
    assert list(pasta.iterdir()) == []


# ---------------------------------------------------------------------- pdf

def test_validacao_recusa_html_salvo_como_pdf(tmp_path: Path):
    falso = tmp_path / "falso.pdf"
    falso.write_bytes(b"<!DOCTYPE html>" + b" " * 2000)
    with pytest.raises(PdfInvalido, match="não é PDF"):
        validar_danfe(falso, CHAVE)


def test_chave_citada_de_outra_nota_nao_conta():
    propria = gerar_chave(CNPJ_ACME, 7976, aamm="2609")
    citada = gerar_chave(CNPJ_ACME, 7955, aamm="2609")
    agrupada = " ".join(propria[i:i + 4] for i in range(0, 44, 4))
    texto = f"DANFE Nº. 000.007.976 Série 001 CHAVE DE ACESSO {agrupada} INFORMAÇÕES COMPLEMENTARES DF-e Ref.: {citada}"
    assert chaves_proprias(texto) == [propria]
    assert chaves_proprias(f"sem número impresso {propria} {citada}") == []   # ambíguo: tratado como ilegível
    assert chaves_proprias(f"uma só chave {CHAVE}") == [CHAVE]


# ------------------------------------------------------------------- pastas

def _arvore_outubro(raiz: Path) -> None:
    for nome in ("OUTUBRO 26 - LOJA CENTRO", "OUTUBRO 26 - LOJA NORTE", "OUTRA FERRAMENTA", "TESTE"):
        (raiz / "2026" / nome).mkdir(parents=True)
    for nome in ("SETEMBRO 26 - LOJA CENTRO", "SETEMBRO 26 - LOJA NORTE"):
        (raiz / "2026" / "2026" / nome).mkdir(parents=True)
    (raiz / "2026" / "OUTUBRO 26 - LOJA NORTE" / "ACME 311026.pdf").write_bytes(b"x")


def test_interpretar_pasta_mes():
    assert interpretar_pasta_mes("NOVEMBRO 25- LOJA CENTRO") == (11, 25)
    assert interpretar_pasta_mes("MARÇO 26 - LOJA NORTE") == (3, 26)
    assert interpretar_pasta_mes("MAIO - LOJA CENTRO") == (5, None)
    assert interpretar_pasta_mes("OUTRA FERRAMENTA") is None
    assert interpretar_pasta_mes("2026") is None


def test_virada_de_mes(tmp_path: Path):
    _arvore_outubro(tmp_path)
    g = GerenciadorPastas(tmp_path, EMPRESAS, LOG)
    g.manutencao(date(2026, 11, 1))
    ano = tmp_path / "2026"
    assert (ano / "2026" / "OUTUBRO 26 - LOJA NORTE" / "ACME 311026.pdf").is_file()
    assert (ano / "NOVEMBRO 26 - LOJA CENTRO").is_dir() and (ano / "NOVEMBRO 26 - LOJA NORTE").is_dir()
    assert (ano / "OUTRA FERRAMENTA").is_dir() and (ano / "TESTE").is_dir()
    assert not (ano / "OUTUBRO 26 - LOJA NORTE").exists()
    # Nota de 31/10 tratada no dia 01/11 vai para a pasta arquivada de outubro.
    assert g.pasta_do_mes(EMPRESAS[1], date(2026, 10, 31), date(2026, 11, 1)) == ano / "2026" / "OUTUBRO 26 - LOJA NORTE"
    assert g.manutencao(date(2026, 11, 1)) == 0  # idempotente


def test_virada_de_ano_achata_o_ano_anterior(tmp_path: Path):
    _arvore_outubro(tmp_path)
    g = GerenciadorPastas(tmp_path, EMPRESAS, LOG)
    g.manutencao(date(2026, 12, 1))
    g.manutencao(date(2027, 1, 1))
    ano26 = tmp_path / "2026"
    assert not (ano26 / "2026").exists()
    for nome in ("SETEMBRO 26 - LOJA NORTE", "OUTUBRO 26 - LOJA NORTE", "DEZEMBRO 26 - LOJA CENTRO"):
        assert (ano26 / nome).is_dir(), nome
    assert (tmp_path / "2027" / "JANEIRO 27 - LOJA CENTRO").is_dir()
    assert g.pasta_do_mes(EMPRESAS[0], date(2026, 12, 31), date(2027, 1, 1)) == ano26 / "DEZEMBRO 26 - LOJA CENTRO"


def test_alias_de_pasta_e_ensaio_nao_mexe(tmp_path: Path):
    (tmp_path / "2026" / "AGOSTO 26 - LOJA NORTE LTDA").mkdir(parents=True)
    g = GerenciadorPastas(tmp_path, EMPRESAS, LOG, ensaio=True)
    assert g.pasta_do_mes(EMPRESAS[1], date(2026, 8, 5), date(2026, 8, 6)).name == "AGOSTO 26 - LOJA NORTE LTDA"
    g.manutencao(date(2026, 9, 1))
    assert (tmp_path / "2026" / "AGOSTO 26 - LOJA NORTE LTDA").is_dir()  # ensaio não move
    assert not (tmp_path / "2026" / "SETEMBRO 26 - LOJA CENTRO").exists()


def test_conflito_nao_sobrescreve(tmp_path: Path):
    (tmp_path / "2026" / "SETEMBRO 26 - LOJA NORTE").mkdir(parents=True)
    (tmp_path / "2026" / "SETEMBRO 26 - LOJA NORTE" / "A.pdf").write_bytes(b"solto")
    (tmp_path / "2026" / "2026" / "SETEMBRO 26 - LOJA NORTE").mkdir(parents=True)
    (tmp_path / "2026" / "2026" / "SETEMBRO 26 - LOJA NORTE" / "A.pdf").write_bytes(b"arquivado")
    GerenciadorPastas(tmp_path, EMPRESAS, LOG).manutencao(date(2026, 10, 1))
    assert (tmp_path / "2026" / "2026" / "SETEMBRO 26 - LOJA NORTE" / "A.pdf").read_bytes() == b"arquivado"
    assert (tmp_path / "2026" / "SETEMBRO 26 - LOJA NORTE" / "A.pdf").read_bytes() == b"solto"


def test_erro_do_onedrive_na_virada_nao_derruba_a_execucao(tmp_path: Path, monkeypatch):
    _arvore_outubro(tmp_path)

    def rename_quebrado(*_):
        raise OSError(362, "O provedor de arquivos de nuvem não está em execução")

    monkeypatch.setattr("rpa_nf_esp.pastas.os.rename", rename_quebrado)
    monkeypatch.setattr("rpa_nf_esp.pastas.time.sleep", lambda s: None)
    GerenciadorPastas(tmp_path, EMPRESAS, LOG).manutencao_segura(date(2026, 11, 1))   # não levanta
    assert (tmp_path / "2026" / "OUTUBRO 26 - LOJA NORTE").is_dir()                    # fica para a próxima


# ------------------------------------------------------------------- ledger

def _dados() -> DadosNota:
    return DadosNota(CHAVE, CNPJ_LOJA_CENTRO, CNPJ_ACME_FILIAL, "ACME ALIMENTOS LTDA", "2830984", "1",
                     date(2026, 10, 23), "438.35", "Autorizada")


def test_cinco_novas_tentativas_depois_desiste(tmp_path: Path):
    ledger = Ledger(tmp_path / "t.sqlite3")
    ledger.iniciar_execucao(False)
    estados = [ledger.registrar_falha(_dados(), "erro", 5).status for _ in range(6)]
    assert estados == [Status.PENDENTE] * 5 + [Status.DESISTIDA]
    assert ledger.em_aberto(CNPJ_LOJA_CENTRO) == []
    assert ledger.reprocessar(CHAVE)
    assert ledger.obter(CHAVE).status == Status.PENDENTE and ledger.obter(CHAVE).falhas == 0
    ledger.marcar(_dados(), Status.BAIXADA, arquivo="X.pdf")
    assert ledger.obter(CHAVE).status == Status.BAIXADA
    ledger.fechar()


# ------------------------------------------------------------------- espião

def test_decodificacao_da_resposta_da_pesquisa():
    assert decodificar_resposta('\r\n"W10="') == []
    registro = {
        "chave_acesso": CHAVE, "nota": "2830984", "serie": "1", "emissao": "23/10/2026",
        "emitente_cnpj": CNPJ_ACME_FILIAL, "emitente_nome": "ACME ALIMENTOS LTDA",
        "destinatario_cnpj_cpf": CNPJ_LOJA_CENTRO, "situacao_atual": "Autorizada",
        "total": "438.35", "arquivo_nfe": "f",
    }
    corpo = json.dumps(base64.b64encode(json.dumps([registro]).encode()).decode())
    nota = nota_da_api(decodificar_resposta(corpo)[0])
    assert nota.emissao == date(2026, 10, 23) and not nota.tem_xml and not nota.cancelada
