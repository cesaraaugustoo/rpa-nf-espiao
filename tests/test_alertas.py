"""Alertas por e-mail: quando avisar, quando não repetir, resumo do dia e envio."""

import logging
from datetime import date
from pathlib import Path

import pytest

from rpa_nf_esp import alertas as mod
from rpa_nf_esp.alertas import Alertas, alerta_da_execucao, resumo_do_dia
from rpa_nf_esp.ledger import Ledger, RegistroNota, Status

LOG = logging.getLogger("teste")


def _empresa(nome="LOJA", **kw):
    return {"empresa": nome, "arquivos": [], "desistencias": [], "erro_geral": None, **kw}


def _execucao(situacao="OK", *empresas, inicio="2026-10-27T09:00:00"):
    return {"situacao": situacao, "inicio": inicio, "resumo": {"empresas": list(empresas)}}


# ------------------------------------------------------------ alerta da execução

def test_execucao_normal_nao_gera_email():
    assert alerta_da_execucao("OK", None, [_empresa()], []) is None
    assert alerta_da_execucao("COM FALHAS", None, [_empresa()], []) is None  # falha isolada: só no resumo


def test_nota_desistida_gera_email_com_a_nota():
    assunto, corpo = alerta_da_execucao("COM FALHAS", None,
                                        [_empresa("LOJA CENTRO", desistencias=["NF 123 de 20/10/2026 - ACME - erro"])], [])
    assert "baixar à mão (LOJA CENTRO)" in assunto
    assert "NF 123 de 20/10/2026 - ACME" in corpo


def test_pesquisa_falhando_avisa_uma_vez_na_terceira_seguida():
    falhou = _empresa("LOJA CENTRO", erro_geral="pesquisa falhou: site fora do ar")
    anteriores_com_erro = [_execucao("COM FALHAS", falhou), _execucao("COM FALHAS", falhou), _execucao("COM FALHAS", falhou)]
    assert alerta_da_execucao("COM FALHAS", None, [falhou], anteriores_com_erro[:1]) is None       # 2ª seguida
    assunto, _ = alerta_da_execucao("COM FALHAS", None, [falhou], anteriores_com_erro[:2])         # 3ª seguida
    assert "pesquisa falhando (LOJA CENTRO)" in assunto
    assert alerta_da_execucao("COM FALHAS", None, [falhou], anteriores_com_erro) is None           # 4ª: não repete
    # o erro de outra empresa não conta na sequência desta
    outra = _execucao("COM FALHAS", _empresa("LOJA CENTRO"), _empresa("LOJA SUL", erro_geral="x"))
    assert alerta_da_execucao("COM FALHAS", None, [falhou], [anteriores_com_erro[0], outra]) is None


def test_erro_fatal_avisa_so_no_inicio_da_sequencia():
    assunto, corpo = alerta_da_execucao("FATAL", "credenciais recusadas", [], [_execucao("OK")])
    assert "RPA parado por erro" in assunto and "credenciais recusadas" in corpo
    assert alerta_da_execucao("FATAL", "credenciais recusadas", [], [_execucao("FATAL")]) is None


# ------------------------------------------------------------------ resumo do dia

def test_resumo_sem_execucao_e_alerta():
    assunto, _ = resumo_do_dia(date(2026, 10, 27), [], [], {})
    assert assunto.startswith("ATENÇÃO: nenhuma execução")


def test_resumo_do_dia_soma_as_execucoes_e_lista_o_que_ficou_em_aberto():
    execucoes = [
        _execucao("OK", _empresa("LOJA CENTRO", arquivos=["ACME 271026_RPA.pdf"]), inicio="2026-10-27T07:00:00"),
        _execucao("OK", _empresa("LOJA CENTRO", arquivos=["BETA 271026_RPA.pdf"]), inicio="2026-10-27T08:00:00"),
    ]
    aberta = RegistroNota(chave="1" * 44, empresa_cnpj="11222333000181", emitente_cnpj=None, emitente_nome="GAMA",
                          numero="77", emissao=date(2026, 10, 27), status=Status.AGUARDANDO_XML, falhas=0,
                          ultimo_erro=None, arquivo=None)
    assunto, corpo = resumo_do_dia(date(2026, 10, 27), execucoes, [aberta], {"11222333000181": "LOJA CENTRO"})
    assert assunto == "Resumo de 27/10/2026: 2 nota(s) baixada(s) - tudo OK"
    assert "ACME 271026_RPA.pdf" in corpo and "BETA 271026_RPA.pdf" in corpo
    assert "Última: 08:00" in corpo
    assert "LOJA CENTRO: NF 77 de 27/10/2026 - GAMA (aguardando XML da SEFAZ)" in corpo


def test_resumo_com_falha_ou_desistida_pede_atencao():
    execucoes = [_execucao("COM FALHAS", _empresa("LOJA CENTRO", desistencias=["NF 1 - erro"]))]
    assunto, corpo = resumo_do_dia(date(2026, 10, 27), execucoes, [], {})
    assert assunto.endswith("ATENÇÃO")
    assert "BAIXAR À MÃO: NF 1 - erro" in corpo


# ------------------------------------------------------------------------- envio

class SmtpFalso:
    enviados: list = []

    def __init__(self, host, porta, timeout):
        assert (host, porta) == (mod.SMTP_HOST, mod.SMTP_PORTA)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, usuario, senha):
        self.credenciais = (usuario, senha)

    def send_message(self, msg):
        SmtpFalso.enviados.append((self.credenciais, msg))


@pytest.fixture
def configurado(monkeypatch):
    monkeypatch.setenv("ALERTA_EMAIL_PARA", "a@exemplo.com, b@exemplo.com")
    monkeypatch.setenv("ALERTA_SMTP_USUARIO", "rpa@exemplo.com")
    monkeypatch.setenv("ALERTA_SMTP_SENHA", "abcd efgh ijkl mnop")
    SmtpFalso.enviados = []
    monkeypatch.setattr(mod.smtplib, "SMTP_SSL", SmtpFalso)


def test_sem_configuracao_nao_envia(monkeypatch):
    for nome in ("ALERTA_EMAIL_PARA", "ALERTA_SMTP_USUARIO", "ALERTA_SMTP_SENHA"):
        monkeypatch.delenv(nome, raising=False)
    monkeypatch.setattr(mod.smtplib, "SMTP_SSL", lambda *a, **k: pytest.fail("não deveria conectar"))
    assert Alertas(LOG).enviar("x", "y") is False


def test_envio_pelo_gmail(configurado):
    assert Alertas(LOG).enviar("assunto", "corpo") is True
    (credenciais, msg), = SmtpFalso.enviados
    assert credenciais == ("rpa@exemplo.com", "abcdefghijklmnop")  # sem os espaços da senha de app
    assert msg["To"] == "a@exemplo.com, b@exemplo.com"
    assert msg["Subject"] == "[RPA NF] assunto"


def test_falha_no_envio_nao_derruba_o_rpa(configurado, monkeypatch):
    def recusa(*a, **k):
        raise OSError("sem internet")
    monkeypatch.setattr(mod.smtplib, "SMTP_SSL", recusa)
    assert Alertas(LOG).enviar("assunto", "corpo") is False


# ------------------------------------------------------------------------ ledger

def test_execucoes_anteriores_e_do_dia(tmp_path: Path):
    ledger = Ledger(tmp_path / "rpa.sqlite3")
    for situacao, ensaio in (("OK", False), ("FATAL", False), ("OK", True)):
        ledger.iniciar_execucao(ensaio)
        ledger.finalizar_execucao(situacao, {"empresas": []})
    ledger.iniciar_execucao(False)  # a execução atual, ainda sem fim
    assert [e["situacao"] for e in ledger.execucoes_anteriores(5)] == ["FATAL", "OK"]  # sem ensaio, sem a atual
    assert [e["situacao"] for e in ledger.execucoes_do_dia(date.today())] == ["OK", "FATAL", None]
    assert ledger.execucoes_do_dia(date(2000, 1, 1)) == []
    ledger.fechar()
