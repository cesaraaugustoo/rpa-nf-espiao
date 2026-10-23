"""Fluxo do executor com um Espião simulado: retentativas, deduplicação e "Sem XML".

Todos os dados são fictícios (ver conftest.py).
"""

import logging
from datetime import date, timedelta
from pathlib import Path

from conftest import CNPJ_ACME_FILIAL, CNPJ_LOJA_CENTRO, gerar_chave

from rpa_nf_esp.apelidos import MapaApelidos
from rpa_nf_esp.config import Config, Empresa
from rpa_nf_esp.espiao import NotaEspiao
from rpa_nf_esp.executor import Executor
from rpa_nf_esp.ledger import Ledger, Status

HOJE = date(2026, 10, 23)
EMPRESA = Empresa(CNPJ_LOJA_CENTRO, "Centro", "EMPRESA EXEMPLO CENTRO LTDA", "LOJA CENTRO")
CHAVE = gerar_chave(CNPJ_ACME_FILIAL, 2830984)
LOG = logging.getLogger("teste")


def _nota(emissao=HOJE, tem_xml=True) -> NotaEspiao:
    return NotaEspiao(CHAVE, "2830984", "1", emissao, CNPJ_ACME_FILIAL, "ACME ALIMENTOS LTDA",
                      EMPRESA.cnpj, "Autorizada", "438.35", tem_xml)


class EspiaoFalso:
    def __init__(self, notas, falhas_no_download=0):
        self.notas = notas
        self.falhas_restantes = falhas_no_download
        self.downloads = 0

    def pesquisar(self, empresa, desde, ate):
        self.ultimo_desde = desde
        return [n for n in self.notas if desde <= n.emissao <= ate]

    def pesquisar_com_retentativa(self, empresa, desde, ate):
        return self.pesquisar(empresa, desde, ate)

    def baixar_danfe(self, nota, pasta):
        self.downloads += 1
        if self.falhas_restantes:
            self.falhas_restantes -= 1
            raise RuntimeError("site instável")
        pasta.mkdir(parents=True, exist_ok=True)
        arq = pasta / f"{nota.chave}-danfe.pdf"
        arq.write_bytes(b"%PDF-1.3\n" + b"0" * 2048)
        return arq

    def capturar_tela(self, contexto):
        return None

    def fechar_menus(self):
        pass

    def recuperar(self):
        pass


class EspiaoForaDoAr(EspiaoFalso):
    def pesquisar(self, empresa, desde, ate):
        raise RuntimeError("Timeout 30000ms exceeded")


def _config(raiz: Path) -> Config:
    return Config(
        url_login="https://exemplo.invalid", usuario="u", senha="s", headless=True, timeout_ms=1000,
        timeout_download_ms=1000, dias_retroativos=1, max_retentativas=5, dias_aguardar_xml=6,
        baixar_canceladas=False, raiz_notas=raiz, sufixo_rpa="_RPA", dir_dados=raiz / "_dados",
        dir_logs=raiz / "_logs", dias_manter_logs=90, apelidos_fonte=raiz / "x.md",
        apelidos_copia=raiz / "y.md", apelidos_usar_sugeridos=True, apelidos_extras=raiz / "z.csv",
        empresas=(EMPRESA,),
    )


def _rodar(tmp_path: Path, ledger: Ledger, espiao: EspiaoFalso, hoje=HOJE):
    ledger.iniciar_execucao(False)
    ex = Executor(_config(tmp_path), LOG, ledger, ensaio=False, empresas=(EMPRESA,), hoje=hoje)
    ex.apelidos = MapaApelidos({"99887766": "ACME"}, {}, {CNPJ_ACME_FILIAL: "ACME FILIAL"})
    ex.staging = tmp_path / "_staging"
    return ex._processar_empresa(espiao, EMPRESA)


def _sem_validar_pdf(monkeypatch):
    monkeypatch.setattr("rpa_nf_esp.executor.validar_danfe", lambda arq, chave: True)


def test_falha_e_retentada_nas_proximas_execucoes_e_depois_desiste(tmp_path: Path, monkeypatch):
    _sem_validar_pdf(monkeypatch)
    ledger = Ledger(tmp_path / "_dados" / "t.sqlite3")
    espiao = EspiaoFalso([_nota()], falhas_no_download=99)
    for _ in range(6):                       # 1ª tentativa + 5 novas tentativas
        _rodar(tmp_path, ledger, espiao)
    assert ledger.obter(CHAVE).status == Status.DESISTIDA
    assert espiao.downloads == 6
    _rodar(tmp_path, ledger, espiao)         # desistida: não tenta mais
    assert espiao.downloads == 6


def test_retentativa_recupera_nota_fora_da_janela(tmp_path: Path, monkeypatch):
    _sem_validar_pdf(monkeypatch)
    ledger = Ledger(tmp_path / "_dados" / "t.sqlite3")
    espiao = EspiaoFalso([_nota()], falhas_no_download=1)
    r1 = _rodar(tmp_path, ledger, espiao)
    assert r1.falhas == 1 and ledger.obter(CHAVE).status == Status.PENDENTE
    # Três dias depois a nota já saiu da janela "hoje + ontem", mas a pendência alarga a pesquisa.
    r2 = _rodar(tmp_path, ledger, espiao, hoje=HOJE + timedelta(days=3))
    assert espiao.ultimo_desde == HOJE and r2.baixadas == 1
    assert (tmp_path / "2026" / "OUTUBRO 26 - LOJA CENTRO" / "ACME FILIAL 231026_RPA.pdf").is_file()
    assert ledger.obter(CHAVE).status == Status.BAIXADA


def test_nao_baixa_duas_vezes(tmp_path: Path, monkeypatch):
    _sem_validar_pdf(monkeypatch)
    ledger = Ledger(tmp_path / "_dados" / "t.sqlite3")
    espiao = EspiaoFalso([_nota()])
    _rodar(tmp_path, ledger, espiao)
    _rodar(tmp_path, ledger, espiao)
    assert espiao.downloads == 1
    assert [p.name for p in (tmp_path / "2026").glob("*/*.pdf")] == ["ACME FILIAL 231026_RPA.pdf"]


def test_sem_xml_nao_gasta_tentativa(tmp_path: Path):
    ledger = Ledger(tmp_path / "_dados" / "t.sqlite3")
    espiao = EspiaoFalso([_nota(tem_xml=False)])
    for _ in range(8):
        r = _rodar(tmp_path, ledger, espiao)
    assert r.aguardando_xml == 1 and espiao.downloads == 0
    assert ledger.obter(CHAVE).status == Status.AGUARDANDO_XML and ledger.obter(CHAVE).falhas == 0


def test_pesquisa_fora_do_ar_nao_gasta_tentativas(tmp_path: Path, monkeypatch):
    _sem_validar_pdf(monkeypatch)
    ledger = Ledger(tmp_path / "_dados" / "t.sqlite3")
    _rodar(tmp_path, ledger, EspiaoFalso([_nota()], falhas_no_download=1))
    assert ledger.obter(CHAVE).falhas == 1
    for _ in range(10):                       # site fora do ar por 10 execuções
        assert _rodar(tmp_path, ledger, EspiaoForaDoAr([])).erro_geral
    assert ledger.obter(CHAVE).status == Status.PENDENTE and ledger.obter(CHAVE).falhas == 1
    assert _rodar(tmp_path, ledger, EspiaoFalso([_nota()])).baixadas == 1


def test_aguardando_xml_sobrevive_a_pesquisa_fora_do_ar(tmp_path: Path):
    ledger = Ledger(tmp_path / "_dados" / "t.sqlite3")
    _rodar(tmp_path, ledger, EspiaoFalso([_nota(tem_xml=False)]))
    for _ in range(10):
        _rodar(tmp_path, ledger, EspiaoForaDoAr([]))
    assert ledger.obter(CHAVE).status == Status.AGUARDANDO_XML and ledger.obter(CHAVE).falhas == 0


def test_xml_que_nunca_chega_desiste_pela_idade(tmp_path: Path):
    ledger = Ledger(tmp_path / "_dados" / "t.sqlite3")
    espiao = EspiaoFalso([_nota(tem_xml=False)])
    _rodar(tmp_path, ledger, espiao)
    r = _rodar(tmp_path, ledger, espiao, hoje=HOJE + timedelta(days=7))
    assert r.desistidas == 1 and ledger.obter(CHAVE).status == Status.DESISTIDA
