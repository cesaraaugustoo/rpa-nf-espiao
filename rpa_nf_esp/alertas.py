"""Alertas por e-mail (Gmail): o que pede ação na hora e o resumo do dia.

Configuração no .env (sem ela, os alertas ficam desligados e o RPA segue normalmente):

    ALERTA_EMAIL_PARA      destinatário(s), separados por vírgula
    ALERTA_SMTP_USUARIO    conta Gmail que envia
    ALERTA_SMTP_SENHA      senha de app dessa conta (Conta Google > Segurança > Senhas de app)

O que gera e-mail na hora:
- nota desistida (precisa ser baixada à mão);
- pesquisa de uma empresa falhando na 3ª execução seguida (um e-mail por sequência, não por hora);
- erro fatal (senha, configuração), só na primeira execução da sequência.
Falha isolada de download não gera e-mail: é retentada sozinha e aparece no resumo do dia.

Um erro no envio nunca derruba o RPA: vira AVISO no log.
"""

import logging
import os
import smtplib
import socket
from datetime import date, datetime
from email.message import EmailMessage

from .ledger import RegistroNota, Status

SMTP_HOST, SMTP_PORTA = "smtp.gmail.com", 465
EXECUCOES_SEGUIDAS_COM_ERRO = 3


class Alertas:
    def __init__(self, log: logging.Logger):
        self.log = log
        self.para = [e.strip() for e in os.environ.get("ALERTA_EMAIL_PARA", "").split(",") if e.strip()]
        self.usuario = os.environ.get("ALERTA_SMTP_USUARIO", "").strip()
        # O Google mostra a senha de app em blocos ("abcd efgh ..."); os espaços não fazem parte dela.
        self._senha = os.environ.get("ALERTA_SMTP_SENHA", "").replace(" ", "")

    @property
    def ativo(self) -> bool:
        return bool(self.para and self.usuario and self._senha)

    def enviar(self, assunto: str, corpo: str) -> bool:
        if not self.ativo:
            self.log.info("Alerta por e-mail não enviado (ALERTA_* não configurado no .env): %s", assunto)
            return False
        msg = EmailMessage()
        msg["Subject"] = f"[RPA NF] {assunto}"
        msg["From"] = self.usuario
        msg["To"] = ", ".join(self.para)
        msg.set_content(f"{corpo}\n\n--\nRPA NF Espião em {socket.gethostname()}, {datetime.now():%d/%m/%Y %H:%M}.")
        try:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORTA, timeout=30) as smtp:
                smtp.login(self.usuario, self._senha)
                smtp.send_message(msg)
        except Exception as e:  # o alerta nunca pode derrubar o RPA
            self.log.warning("Não foi possível enviar o alerta por e-mail (%s): %s", type(e).__name__, e)
            return False
        self.log.info("Alerta por e-mail enviado para %s: %s", msg["To"], assunto)
        return True


def alerta_da_execucao(situacao: str, erro: str | None, resumos: list[dict],
                       anteriores: list[dict]) -> tuple[str, str] | None:
    """Decide se a execução que acabou pede um e-mail. Devolve (assunto, corpo) ou None.

    `resumos`: um dict por empresa (`resumo_para_json`); `anteriores`: execuções finalizadas
    antes desta, da mais recente para a mais antiga, cada uma com "situacao" e "resumo".
    """
    partes: list[str] = []
    assuntos: list[str] = []

    if situacao == "FATAL" and not (anteriores and anteriores[0]["situacao"] == "FATAL"):
        assuntos.append("RPA parado por erro")
        partes.append(f"A execução terminou com erro e não processou as notas:\n  {erro}\n"
                      "O RPA tenta de novo a cada hora; este e-mail não se repete enquanto o erro continuar.")

    for r in resumos:
        if r.get("desistencias"):
            assuntos.append(f"baixar à mão ({r['empresa']})")
            linhas = "\n".join(f"  - {d}" for d in r["desistencias"])
            partes.append(f"{r['empresa']}: nota(s) que o RPA não conseguiu baixar. Baixe à mão no Espião:\n{linhas}")

        if r.get("erro_geral"):
            seguidas = 1
            for ant in anteriores:
                empresas = {e["empresa"]: e for e in (ant.get("resumo") or {}).get("empresas", [])}
                if not (empresas.get(r["empresa"]) or {}).get("erro_geral"):
                    break
                seguidas += 1
            if seguidas == EXECUCOES_SEGUIDAS_COM_ERRO:
                assuntos.append(f"pesquisa falhando ({r['empresa']})")
                partes.append(f"{r['empresa']}: a pesquisa no Espião falhou nas últimas {seguidas} execuções.\n"
                              f"  Último erro: {r['erro_geral']}\n"
                              "As notas desse período serão baixadas quando a pesquisa voltar a funcionar.")

    if not partes:
        return None
    return "ATENÇÃO: " + "; ".join(assuntos), "\n\n".join(partes)


def resumo_do_dia(dia: date, execucoes: list[dict], em_aberto: list[RegistroNota],
                  nomes: dict[str, str]) -> tuple[str, str]:
    """Resumo enviado no fim do dia. `execucoes`: as do dia, sem ensaio; `nomes`: CNPJ -> pasta."""
    if not execucoes:
        return (f"ATENÇÃO: nenhuma execução em {dia:%d/%m/%Y}",
                "O RPA não rodou nenhuma vez hoje. Verifique a VPS (serviço rpa-nf.timer) ou o computador.")

    por_situacao: dict[str, int] = {}
    empresas: dict[str, dict] = {}
    for ex in execucoes:
        situacao = ex["situacao"] or "INTERROMPIDA"
        por_situacao[situacao] = por_situacao.get(situacao, 0) + 1
        for r in (ex.get("resumo") or {}).get("empresas", []):
            e = empresas.setdefault(r["empresa"], {"arquivos": [], "desistencias": [], "erros": 0})
            e["arquivos"] += r.get("arquivos", [])
            e["desistencias"] += r.get("desistencias", [])
            e["erros"] += bool(r.get("erro_geral"))

    total = sum(len(e["arquivos"]) for e in empresas.values())
    problemas = any(e["desistencias"] for e in empresas.values()) or any(s != "OK" for s in por_situacao)
    linhas = [f"Execuções: {len(execucoes)} ("
              + ", ".join(f"{n} {s}" for s, n in sorted(por_situacao.items())) + ")",
              f"Última: {execucoes[-1]['inicio'][11:16]}", ""]
    for nome, e in empresas.items():
        linhas.append(f"{nome}: {len(e['arquivos'])} baixada(s)"
                      + (f", pesquisa falhou em {e['erros']} execução(ões)" if e["erros"] else ""))
        linhas += [f"  + {a}" for a in e["arquivos"]]
        linhas += [f"  ! BAIXAR À MÃO: {d}" for d in e["desistencias"]]
    if em_aberto:
        linhas += ["", "Em aberto (o RPA continua tentando sozinho):"]
        for r in em_aberto:
            motivo = "aguardando XML da SEFAZ" if r.status == Status.AGUARDANDO_XML else f"{r.falhas} falha(s)"
            linhas.append(f"  - {nomes.get(r.empresa_cnpj, r.empresa_cnpj)}: NF {r.numero} de "
                          f"{r.emissao:%d/%m/%Y} - {r.emitente_nome} ({motivo})")
    assunto = f"Resumo de {dia:%d/%m/%Y}: {total} nota(s) baixada(s)" + (" - ATENÇÃO" if problemas else " - tudo OK")
    return assunto, "\n".join(linhas)
