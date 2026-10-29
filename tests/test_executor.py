"""Fluxo do executor com um Espião simulado: retentativas, deduplicação e "Sem XML".

Todos os dados são fictícios (ver conftest.py).
"""

import logging
from dataclasses import replace
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


def xml_da_nota(chave: str) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8"?><nfeProc xmlns="http://www.portalfiscal.inf.br/nfe">'
            f'<NFe><infNFe versao="4.00" Id="NFe{chave}"><ide/></infNFe></NFe></nfeProc>').encode()


class EspiaoFalso:
    def __init__(self, notas, falhas_no_download=0, xml_ao_investigar=False, investigar_quebra=False,
                 falhas_no_xml=0, xml_de_outra_nota=False):
        self.notas = notas
        self.falhas_restantes = falhas_no_download
        self.downloads = 0
        self.xml_ao_investigar = xml_ao_investigar
        self.investigar_quebra = investigar_quebra
        self.investigadas = []
        self.falhas_xml_restantes = falhas_no_xml
        self.xml_de_outra_nota = xml_de_outra_nota
        self.downloads_xml = 0

    def investigar_xml(self, nota):
        self.investigadas.append(nota.chave)
        if self.investigar_quebra:
            raise RuntimeError("botão 'Investigar XML' não encontrado")
        if self.xml_ao_investigar:
            self.notas = [replace(n, tem_xml=True) if n.chave == nota.chave else n for n in self.notas]
        return "Consulta realizada"

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

    def baixar_xml(self, nota, pasta):
        self.downloads_xml += 1
        if self.falhas_xml_restantes:
            self.falhas_xml_restantes -= 1
            raise RuntimeError("menu de download não abriu")
        pasta.mkdir(parents=True, exist_ok=True)
        arq = pasta / f"{nota.chave}.xml"
        arq.write_bytes(xml_da_nota(gerar_chave(CNPJ_ACME_FILIAL, 1) if self.xml_de_outra_nota else nota.chave))
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
    espiao = EspiaoFalso([_nota()], falhas_no_download=3)
    for d in range(3):                       # falha em 3 dias seguidos; a cobertura avança normalmente
        _rodar(tmp_path, ledger, espiao, hoje=HOJE + timedelta(days=d))
    assert ledger.obter(CHAVE).status == Status.PENDENTE and ledger.obter(CHAVE).falhas == 3
    # No 4º dia a cobertura começaria em HOJE+1; só a pendência explica recuar até a emissão.
    r = _rodar(tmp_path, ledger, espiao, hoje=HOJE + timedelta(days=3))
    assert espiao.ultimo_desde == HOJE and r.baixadas == 1
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


# ------------------------------------------------- cobertura sem lacunas

def _nota_de(emissao: date, numero: int) -> NotaEspiao:
    return NotaEspiao(gerar_chave(CNPJ_ACME_FILIAL, numero), str(numero), "1", emissao, CNPJ_ACME_FILIAL,
                      "ACME ALIMENTOS LTDA", EMPRESA.cnpj, "Autorizada", "10.00", True)


def test_computador_desligado_no_fim_de_semana_nao_perde_nota(tmp_path: Path, monkeypatch):
    _sem_validar_pdf(monkeypatch)
    ledger = Ledger(tmp_path / "_dados" / "t.sqlite3")
    sexta, sabado, segunda = date(2026, 10, 23), date(2026, 10, 24), date(2026, 10, 26)
    _rodar(tmp_path, ledger, EspiaoFalso([]), hoje=sexta)            # última execução: sexta
    espiao = EspiaoFalso([_nota_de(sabado, 1001)])                  # nota emitida no sábado
    r = _rodar(tmp_path, ledger, espiao, hoje=segunda)              # PC volta a ligar na segunda
    assert espiao.ultimo_desde == sexta - timedelta(days=1)         # cobre desde a última execução, com margem
    assert r.baixadas == 1


def test_site_fora_do_ar_por_dias_nao_perde_nota(tmp_path: Path, monkeypatch):
    _sem_validar_pdf(monkeypatch)
    ledger = Ledger(tmp_path / "_dados" / "t.sqlite3")
    _rodar(tmp_path, ledger, EspiaoFalso([]), hoje=HOJE)
    for d in (1, 2, 3):                                             # pesquisa falha por 3 dias: cobertura não avança
        _rodar(tmp_path, ledger, EspiaoForaDoAr([]), hoje=HOJE + timedelta(days=d))
    r = _rodar(tmp_path, ledger, EspiaoFalso([_nota_de(HOJE + timedelta(days=1), 1002)]), hoje=HOJE + timedelta(days=4))
    assert r.baixadas == 1


def test_execucao_horaria_normal_mantem_a_janela_curta(tmp_path: Path):
    ledger = Ledger(tmp_path / "_dados" / "t.sqlite3")
    _rodar(tmp_path, ledger, EspiaoFalso([]), hoje=HOJE)
    espiao = EspiaoFalso([])
    _rodar(tmp_path, ledger, espiao, hoje=HOJE)                     # mesma data: hoje + ontem
    assert espiao.ultimo_desde == HOJE - timedelta(days=1)
    _rodar(tmp_path, ledger, espiao, hoje=HOJE + timedelta(days=1))  # dia seguinte: ainda cobre anteontem
    assert espiao.ultimo_desde == HOJE - timedelta(days=1)


def test_primeira_execucao_sem_historico_usa_hoje_e_ontem(tmp_path: Path):
    ledger = Ledger(tmp_path / "_dados" / "t.sqlite3")
    espiao = EspiaoFalso([])
    _rodar(tmp_path, ledger, espiao, hoje=HOJE)
    assert espiao.ultimo_desde == HOJE - timedelta(days=1)
    assert ledger.cobertura(EMPRESA.cnpj) == (HOJE, HOJE)


# ------------------------------------------------- investigar XML

def test_sem_xml_e_investigado_e_baixado_no_mesmo_ciclo(tmp_path: Path, monkeypatch):
    _sem_validar_pdf(monkeypatch)
    ledger = Ledger(tmp_path / "_dados" / "t.sqlite3")
    espiao = EspiaoFalso([_nota(tem_xml=False)], xml_ao_investigar=True)
    r = _rodar(tmp_path, ledger, espiao)
    assert espiao.investigadas == [CHAVE]
    assert r.baixadas == 1 and r.aguardando_xml == 0
    assert ledger.obter(CHAVE).status == Status.BAIXADA


def test_investigar_sem_resultado_mantem_aguardando(tmp_path: Path):
    ledger = Ledger(tmp_path / "_dados" / "t.sqlite3")
    espiao = EspiaoFalso([_nota(tem_xml=False)], xml_ao_investigar=False)
    r = _rodar(tmp_path, ledger, espiao)
    assert espiao.investigadas == [CHAVE] and r.aguardando_xml == 1 and espiao.downloads == 0
    assert ledger.obter(CHAVE).status == Status.AGUARDANDO_XML and ledger.obter(CHAVE).falhas == 0


def test_falha_ao_investigar_nao_derruba_nem_gasta_tentativa(tmp_path: Path):
    ledger = Ledger(tmp_path / "_dados" / "t.sqlite3")
    r = _rodar(tmp_path, ledger, EspiaoFalso([_nota(tem_xml=False)], investigar_quebra=True))
    assert r.erro_geral is None and r.aguardando_xml == 1 and r.falhas == 0
    assert ledger.obter(CHAVE).status == Status.AGUARDANDO_XML and ledger.obter(CHAVE).falhas == 0


# ------------------------------------------------- XML ao lado do PDF

PASTA_MES = Path("2026") / "OUTUBRO 26 - LOJA CENTRO"


def test_xml_salvo_ao_lado_do_pdf_com_o_mesmo_nome(tmp_path: Path, monkeypatch):
    _sem_validar_pdf(monkeypatch)
    ledger = Ledger(tmp_path / "_dados" / "t.sqlite3")
    espiao = EspiaoFalso([_nota()])
    r = _rodar(tmp_path, ledger, espiao)
    pasta = tmp_path / PASTA_MES
    assert sorted(p.name for p in pasta.iterdir()) == ["ACME FILIAL 231026_RPA.pdf", "ACME FILIAL 231026_RPA.xml"]
    assert r.baixadas == 1 and r.xmls == 1 and r.ok
    assert ledger.obter(CHAVE).xml == str(pasta / "ACME FILIAL 231026_RPA.xml")
    _rodar(tmp_path, ledger, espiao)                                # nada a fazer na execução seguinte
    assert espiao.downloads == 1 and espiao.downloads_xml == 1


def test_falha_no_xml_mantem_o_pdf_e_tenta_so_o_xml_depois(tmp_path: Path, monkeypatch):
    _sem_validar_pdf(monkeypatch)
    ledger = Ledger(tmp_path / "_dados" / "t.sqlite3")
    espiao = EspiaoFalso([_nota()], falhas_no_xml=1)
    r = _rodar(tmp_path, ledger, espiao)
    assert r.baixadas == 1 and r.xmls == 0 and r.ok              # o PDF vale; XML não conta como falha da nota
    assert ledger.obter(CHAVE).status == Status.BAIXADA and ledger.obter(CHAVE).xml is None
    r = _rodar(tmp_path, ledger, espiao)
    assert r.xmls == 1 and espiao.downloads == 1 and espiao.downloads_xml == 2
    assert (tmp_path / PASTA_MES / "ACME FILIAL 231026_RPA.xml").is_file()


def test_xml_desiste_apos_as_tentativas_e_avisa(tmp_path: Path, monkeypatch):
    _sem_validar_pdf(monkeypatch)
    ledger = Ledger(tmp_path / "_dados" / "t.sqlite3")
    espiao = EspiaoFalso([_nota()], falhas_no_xml=99)
    resumos = [_rodar(tmp_path, ledger, espiao) for _ in range(7)]
    assert espiao.downloads_xml == 6                                # 1ª tentativa + 5 novas
    assert resumos[5].desistencias and "XML" in resumos[5].desistencias[0]
    assert ledger.obter(CHAVE).xml == "" and ledger.obter(CHAVE).status == Status.BAIXADA


def test_xml_de_outra_nota_e_recusado(tmp_path: Path, monkeypatch):
    _sem_validar_pdf(monkeypatch)
    ledger = Ledger(tmp_path / "_dados" / "t.sqlite3")
    r = _rodar(tmp_path, ledger, EspiaoFalso([_nota()], xml_de_outra_nota=True))
    assert r.xmls == 0 and not (tmp_path / PASTA_MES / "ACME FILIAL 231026_RPA.xml").exists()
    assert ledger.obter(CHAVE).xml is None


def test_xml_de_nota_baixada_antes_da_funcao(tmp_path: Path, monkeypatch):
    """Notas baixadas por versões anteriores ganham o XML; a pesquisa recua até a emissão delas."""
    _sem_validar_pdf(monkeypatch)
    ledger = Ledger(tmp_path / "_dados" / "t.sqlite3")
    antiga = _nota_de(HOJE, 3001)
    _rodar(tmp_path, ledger, EspiaoFalso([antiga]))
    _rodar(tmp_path, ledger, EspiaoFalso([antiga]), hoje=HOJE + timedelta(days=4))  # cobertura avança
    ledger._con.execute("UPDATE notas SET xml = NULL")              # como no banco anterior a esta versão
    (tmp_path / PASTA_MES / "ACME FILIAL 231026_RPA.xml").unlink()
    espiao = EspiaoFalso([antiga])
    r = _rodar(tmp_path, ledger, espiao, hoje=HOJE + timedelta(days=5))
    assert espiao.ultimo_desde == HOJE and espiao.downloads == 0 and r.xmls == 1
    assert (tmp_path / PASTA_MES / "ACME FILIAL 231026_RPA.xml").is_file()


def test_xml_nao_baixado_se_o_pdf_sumiu(tmp_path: Path, monkeypatch):
    _sem_validar_pdf(monkeypatch)
    ledger = Ledger(tmp_path / "_dados" / "t.sqlite3")
    espiao = EspiaoFalso([_nota()], falhas_no_xml=1)
    _rodar(tmp_path, ledger, espiao)
    (tmp_path / PASTA_MES / "ACME FILIAL 231026_RPA.pdf").unlink()  # alguém moveu o PDF
    r = _rodar(tmp_path, ledger, espiao)
    assert espiao.downloads_xml == 1 and r.xmls == 0 and r.ok
    assert ledger.obter(CHAVE).xml == ""
    assert not list((tmp_path / PASTA_MES).glob("*.xml"))


def test_xml_ja_presente_e_so_registrado(tmp_path: Path, monkeypatch):
    _sem_validar_pdf(monkeypatch)
    ledger = Ledger(tmp_path / "_dados" / "t.sqlite3")
    espiao = EspiaoFalso([_nota()], falhas_no_xml=1)
    _rodar(tmp_path, ledger, espiao)
    (tmp_path / PASTA_MES / "ACME FILIAL 231026_RPA.xml").write_bytes(xml_da_nota(CHAVE))  # salvo à mão
    r = _rodar(tmp_path, ledger, espiao)
    assert r.xmls == 1 and espiao.downloads_xml == 1
