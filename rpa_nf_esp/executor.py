"""Orquestração de uma execução completa do RPA."""

import logging
import shutil
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

from . import apelidos as mod_apelidos
from .apelidos import MapaApelidos
from .config import Config, Empresa
from .documentos import ChaveAcesso
from .espiao import ALCANCE_MAXIMO_DIAS, EspiaoNFe, NotaEspiao, SemXml, resumir_erro, sessao_espiao
from .ledger import CONCLUIDAS, DadosNota, Ledger, Status
from .nomes import base_nome, gravar_sem_sobrescrever, normalizar
from .pastas import GerenciadorPastas
from .pdf import IndiceChaves, validar_danfe

@dataclass
class ResumoEmpresa:
    empresa: str
    na_janela: int = 0
    baixadas: int = 0
    ja_existiam: int = 0
    aguardando_xml: int = 0
    falhas: int = 0
    desistidas: int = 0
    canceladas: int = 0
    erro_geral: str | None = None
    arquivos: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.erro_geral is None and self.falhas == 0 and self.desistidas == 0


def _dados(nota: NotaEspiao, empresa: Empresa) -> DadosNota:
    return DadosNota(
        chave=nota.chave, empresa_cnpj=empresa.cnpj, emitente_cnpj=nota.emitente_cnpj,
        emitente_nome=nota.emitente_nome, numero=nota.numero, serie=nota.serie,
        emissao=nota.emissao, valor=nota.valor, situacao_sefaz=nota.situacao,
    )


def _numero_ordenacao(nota: NotaEspiao) -> int:
    return int(nota.numero) if nota.numero.isdigit() else 0


class Executor:
    def __init__(self, cfg: Config, log: logging.Logger, ledger: Ledger, *, ensaio: bool,
                 empresas: tuple[Empresa, ...], hoje: date):
        self.cfg = cfg
        self.log = log
        self.ledger = ledger
        self.ensaio = ensaio
        self.empresas = empresas
        self.hoje = hoje
        self.pastas = GerenciadorPastas(cfg.raiz_notas, cfg.empresas, log, ensaio)
        self.indice = IndiceChaves(ledger, log)
        self.apelidos: MapaApelidos | None = None
        self.staging = cfg.dir_dados / "staging"
        self._sem_xml: list[NotaEspiao] = []  # notas "Sem XML" da empresa em processamento

    # --------------------------------------------------------------- execução

    def executar(self, somente_pastas: bool = False) -> list[ResumoEmpresa]:
        if somente_pastas:
            self.pastas.manutencao(self.hoje)
            return []
        self.pastas.manutencao_segura(self.hoje)
        self.apelidos = mod_apelidos.carregar(self.cfg, self.log)
        self.staging = self.cfg.dir_dados / "staging" / str(self.ledger.execucao_id)
        try:
            return self._processar_site()
        finally:
            shutil.rmtree(self.staging, ignore_errors=True)

    def _processar_site(self) -> list[ResumoEmpresa]:
        with sessao_espiao(self.cfg, self.log) as espiao:
            return [self._processar_empresa(espiao, empresa) for empresa in self.empresas]

    # ---------------------------------------------------------------- empresa

    def _processar_empresa(self, espiao: EspiaoNFe, empresa: Empresa) -> ResumoEmpresa:
        resumo = ResumoEmpresa(empresa=empresa.pasta)
        self.log.info("-" * 70)
        self.log.info("Empresa %s - %s", empresa.rotulo, empresa.razao_social)

        limite = self.hoje - timedelta(days=ALCANCE_MAXIMO_DIAS)
        inicio_janela = self._inicio_janela(empresa, limite)
        em_aberto = self.ledger.em_aberto(empresa.cnpj)
        alcancaveis = [r for r in em_aberto if r.emissao >= limite]
        for r in em_aberto:
            if r.emissao < limite:
                self._encerrar(r, f"emissão há mais de {ALCANCE_MAXIMO_DIAS} dias, fora do alcance da pesquisa", resumo)
        desde = min([inicio_janela] + [r.emissao for r in alcancaveis])
        if alcancaveis:
            self.log.info("%d nota(s) em aberto de execuções anteriores serão tentadas de novo", len(alcancaveis))

        try:
            notas = espiao.pesquisar_com_retentativa(empresa, desde, self.hoje)
        except Exception as e:
            # Falha na pesquisa é falha do site, não do download: não gasta as tentativas das notas
            # em aberto. Elas e as notas novas da janela entram de novo na próxima execução.
            resumo.erro_geral = f"pesquisa falhou: {resumir_erro(e)}"
            self.log.error("Pesquisa da empresa %s falhou: %s. Será refeita na próxima execução.",
                           empresa.rotulo, resumir_erro(e))
            self.log.debug("Detalhes", exc_info=True)
            espiao.capturar_tela(f"pesquisa_{empresa.cnpj}")
            self._tentar_recuperar(espiao)
            return resumo

        por_chave = {n.chave: n for n in notas}
        alvos = {n.chave: n for n in notas if inicio_janela <= n.emissao <= self.hoje}
        resumo.na_janela = len(alvos)
        for r in alcancaveis:
            if r.chave in por_chave:
                alvos.setdefault(r.chave, por_chave[r.chave])
            elif r.status == Status.PENDENTE:
                self._falha_sem_nota(r.chave, "a nota não apareceu na pesquisa do Espião", resumo)
            elif (self.hoje - r.emissao).days > self.cfg.dias_aguardar_xml:
                self._encerrar(r, f"XML não disponibilizado pela SEFAZ em {self.cfg.dias_aguardar_xml} dias", resumo)
            else:
                self.log.warning("Nota aguardando XML não apareceu na pesquisa: NF %s de %s - %s",
                                 r.numero, f"{r.emissao:%d/%m/%Y}", r.emitente_nome)
        self.log.info(
            "%d nota(s) no período pesquisado; %d emitida(s) de %s a %s; %d alvo(s) neste ciclo",
            len(notas), resumo.na_janela, f"{inicio_janela:%d/%m}", f"{self.hoje:%d/%m}", len(alvos),
        )

        self._sem_xml: list[NotaEspiao] = []
        ordenados = sorted(alvos.values(), key=lambda n: (n.emissao, n.emitente_cnpj, _numero_ordenacao(n)))
        for nota in ordenados:
            self._processar_nota_protegido(espiao, empresa, nota, resumo)
        if self._sem_xml:
            self._investigar_sem_xml(espiao, empresa, desde, resumo)
        # Todas as notas do período foram vistas: as que falharam já estão no ledger como pendentes.
        if not self.ensaio:
            self.ledger.registrar_cobertura(empresa.cnpj, self.hoje)
        return resumo

    def _processar_nota_protegido(self, espiao: EspiaoNFe, empresa: Empresa, nota: NotaEspiao,
                                  resumo: ResumoEmpresa) -> None:
        try:
            self._processar_nota(espiao, empresa, nota, resumo)
        except Exception as e:
            self.log.debug("Erro inesperado na NF %s (%s)", nota.numero, nota.chave, exc_info=True)
            self._registrar_falha(nota, empresa, f"erro inesperado: {resumir_erro(e)}", resumo)
            self._tentar_recuperar(espiao)

    def _investigar_sem_xml(self, espiao: EspiaoNFe, empresa: Empresa, desde: date, resumo: ResumoEmpresa) -> None:
        """Para cada nota "Sem XML", aciona "Investigar XML" (o Espião busca o XML na SEFAZ na hora),
        pesquisa de novo e baixa no mesmo ciclo as que passaram a ter XML. Qualquer falha aqui só
        mantém a nota aguardando: ela é verificada de novo na próxima execução."""
        if self.ensaio:
            for nota in self._sem_xml:
                self.log.info("[ensaio] acionaria 'Investigar XML' na NF %s - %s", nota.numero, nota.emitente_nome)
            return
        investigadas = []
        for nota in self._sem_xml:
            try:
                texto = espiao.investigar_xml(nota)
                self.log.info("Investigar XML acionado: NF %s - %s. Resposta do site: %s",
                              nota.numero, nota.emitente_nome, texto[:300] or "(sem texto)")
                investigadas.append(nota)
            except Exception as e:
                self.log.warning("Não foi possível acionar 'Investigar XML' na NF %s: %s. "
                                 "Segue aguardando a SEFAZ.", nota.numero, resumir_erro(e))
                self.log.debug("Detalhes", exc_info=True)
                espiao.capturar_tela(f"investigar_{nota.chave}")
                self._tentar_recuperar(espiao)
        if not investigadas:
            return
        try:
            atuais = {n.chave: n for n in espiao.pesquisar_com_retentativa(empresa, desde, self.hoje)}
        except Exception as e:
            self.log.warning("Pesquisa após investigar o XML falhou (%s); as notas seguem aguardando.", resumir_erro(e))
            self._tentar_recuperar(espiao)
            return
        for nota in investigadas:
            atual = atuais.get(nota.chave)
            if atual is not None and atual.tem_xml:
                self.log.info("XML disponível após investigar: NF %s - %s", nota.numero, nota.emitente_nome)
                resumo.aguardando_xml -= 1
                self._processar_nota_protegido(espiao, empresa, atual, resumo)
            else:
                self.log.warning("Aguardando XML: NF %s - %s continua sem XML mesmo após investigar. "
                                 "Nova tentativa na próxima execução.", nota.numero, nota.emitente_nome)

    def _inicio_janela(self, empresa: Empresa, limite: date) -> date:
        """Primeiro dia de emissão que esta execução precisa cobrir.

        Normalmente é ontem (dias_retroativos = 1). Se a última pesquisa completa da empresa foi
        há mais tempo (computador desligado ou suspenso, internet fora, site fora do ar), a janela
        recua até ela, com a mesma margem: nenhuma nota emitida no intervalo fica para trás.
        """
        cobertura = self.ledger.cobertura(empresa.cnpj)
        referencia = min(self.hoje, cobertura[1]) if cobertura else self.hoje
        inicio = referencia - timedelta(days=self.cfg.dias_retroativos)
        if cobertura and cobertura[1] < self.hoje - timedelta(days=1):
            self.log.warning("Última pesquisa completa desta empresa foi em %s (sem execução desde então). "
                             "Cobrindo as notas emitidas desde %s.", f"{cobertura[1]:%d/%m/%Y}", f"{max(inicio, limite):%d/%m/%Y}")
        if inicio < limite:
            self.log.error("O intervalo sem execução passa de %d dias: notas emitidas antes de %s precisam ser "
                           "conferidas à mão (EXECUTAR_AGORA.bat --conferir).", ALCANCE_MAXIMO_DIAS, f"{limite:%d/%m/%Y}")
            inicio = limite
        return inicio

    def _tentar_recuperar(self, espiao: EspiaoNFe) -> None:
        try:
            espiao.recuperar()
        except Exception as e:
            self.log.error("Não foi possível recuperar a sessão: %s", resumir_erro(e))

    # ------------------------------------------------------------------- nota

    def _processar_nota(self, espiao: EspiaoNFe, empresa: Empresa, nota: NotaEspiao, resumo: ResumoEmpresa) -> None:
        rotulo = f"NF {nota.numero} de {nota.emissao:%d/%m/%Y} - {nota.emitente_nome}"
        registro = self.ledger.obter(nota.chave)
        if registro and registro.status in CONCLUIDAS:
            self.log.debug("Já tratada em execução anterior (%s): %s", registro.status, rotulo)
            return
        if registro and registro.status in (Status.DESISTIDA, Status.CANCELADA):
            self.log.info("Ignorada (%s anteriormente; use --reprocessar para tentar de novo): %s",
                          registro.status, rotulo)
            return

        try:
            chave = ChaveAcesso(nota.chave)
        except ValueError as e:
            self._registrar_falha(nota, empresa, str(e), resumo)
            return
        if chave.cnpj_emitente != nota.emitente_cnpj:
            self.log.warning("CNPJ do emitente difere entre a chave (%s) e o site (%s); vale a chave",
                             chave.cnpj_emitente, nota.emitente_cnpj)

        if nota.cancelada and not self.cfg.baixar_canceladas:
            self.log.info("Ignorada, nota %s: %s", nota.situacao, rotulo)
            if not self.ensaio:
                self.ledger.marcar(_dados(nota, empresa), Status.CANCELADA)
            resumo.canceladas += 1
            return

        pasta = self.pastas.pasta_do_mes(empresa, nota.emissao, self.hoje, criar=not self.ensaio)
        existente = self.indice.chaves_na_pasta(pasta).get(nota.chave)
        if existente:
            self.log.info("Já está na pasta como '%s': %s", existente, rotulo)
            if not self.ensaio:
                self.ledger.marcar(_dados(nota, empresa), Status.JA_EXISTIA, arquivo=str(pasta / existente))
            resumo.ja_existiam += 1
            return

        resolucao = self.apelidos.resolver(chave.cnpj_emitente, nota.emitente_nome)
        base = base_nome(resolucao.nome, nota.emissao)

        if not nota.tem_xml:
            self._aguardar_xml(nota, empresa, rotulo, resumo)
            return

        suspeitos = [n for n in self.indice.ilegiveis_na_pasta(pasta)
                     if normalizar(Path(n).stem).startswith(normalizar(base))]
        if suspeitos:
            self.log.warning("Possível duplicata: '%s' já está na pasta, mas é um PDF sem texto (digitalizado?) "
                             "e não dá para ler a chave. Conferir após o download: %s", "', '".join(suspeitos), rotulo)

        if self.ensaio:
            self.log.info("[ensaio] baixaria %s -> %s\\%s%s.pdf (apelido: %s)",
                          rotulo, pasta.name, base, self.cfg.sufixo_rpa, resolucao.origem)
            return

        try:
            temporario = espiao.baixar_danfe(nota, self.staging)
            confirmada = validar_danfe(temporario, nota.chave)
            if not confirmada:
                self.log.warning("PDF sem texto legível; chave conferida só pelo nome do download: %s", rotulo)
            destino = gravar_sem_sobrescrever(temporario, pasta, base, self.cfg.sufixo_rpa)
            temporario.unlink(missing_ok=True)
        except SemXml:
            self._aguardar_xml(nota, empresa, rotulo, resumo)
            return
        except Exception as e:  # PdfInvalido, erro do site, erro de disco: todos viram nova tentativa
            espiao.capturar_tela(f"download_{nota.chave}")
            espiao.fechar_menus()
            self.log.debug("Detalhes da falha no download", exc_info=True)
            self._registrar_falha(nota, empresa, resumir_erro(e), resumo)
            return

        self.indice.registrar(destino, nota.chave)
        self.ledger.marcar(_dados(nota, empresa), Status.BAIXADA, arquivo=str(destino))
        resumo.baixadas += 1
        resumo.arquivos.append(destino.name)
        self.log.info("BAIXADA: %s -> %s\\%s (apelido %s)", rotulo, pasta.name, destino.name, resolucao.origem)

    def _aguardar_xml(self, nota: NotaEspiao, empresa: Empresa, rotulo: str, resumo: ResumoEmpresa) -> None:
        idade = (self.hoje - nota.emissao).days
        if idade > self.cfg.dias_aguardar_xml:
            motivo = f"XML não disponibilizado pela SEFAZ em {idade} dias"
            self.log.error("DESISTIDA: %s - %s. Baixar manualmente.", rotulo, motivo)
            if not self.ensaio:
                self.ledger.marcar(_dados(nota, empresa), Status.DESISTIDA, erro=motivo)
            resumo.desistidas += 1
            return
        self.log.info("Sem XML no Espião (só o resumo da SEFAZ): %s", rotulo)
        if not self.ensaio:
            self.ledger.marcar(_dados(nota, empresa), Status.AGUARDANDO_XML, erro="sem XML no Espião")
        resumo.aguardando_xml += 1
        self._sem_xml.append(nota)

    def _registrar_falha(self, nota: NotaEspiao, empresa: Empresa, motivo: str, resumo: ResumoEmpresa) -> None:
        rotulo = f"NF {nota.numero} de {nota.emissao:%d/%m/%Y} - {nota.emitente_nome}"
        if self.ensaio:
            self.log.error("[ensaio] falha em %s: %s", rotulo, motivo)
            resumo.falhas += 1
            return
        registro = self.ledger.registrar_falha(_dados(nota, empresa), motivo, self.cfg.max_retentativas)
        self._contabilizar_falha(registro, rotulo, motivo, resumo)

    def _encerrar(self, registro, motivo: str, resumo: ResumoEmpresa) -> None:
        rotulo = f"NF {registro.numero} de {registro.emissao:%d/%m/%Y} - {registro.emitente_nome}"
        resumo.desistidas += 1
        self.log.error("DESISTIDA: %s - %s. Baixar manualmente.", rotulo, motivo)
        if not self.ensaio:
            self.ledger.encerrar(registro.chave, motivo)

    def _falha_sem_nota(self, chave: str, motivo: str, resumo: ResumoEmpresa) -> None:
        if self.ensaio:
            self.log.warning("[ensaio] pendência %s não tratada: %s", chave, motivo)
            return
        registro = self.ledger.registrar_falha_por_chave(chave, motivo, self.cfg.max_retentativas)
        if registro:
            rotulo = f"NF {registro.numero} de {registro.emissao:%d/%m/%Y} - {registro.emitente_nome}"
            self._contabilizar_falha(registro, rotulo, motivo, resumo)

    def _contabilizar_falha(self, registro, rotulo: str, motivo: str, resumo: ResumoEmpresa) -> None:
        if registro.status == Status.DESISTIDA:
            resumo.desistidas += 1
            self.log.error("DESISTIDA após %d tentativas: %s - %s. Baixar manualmente.",
                           registro.falhas, rotulo, motivo)
        else:
            resumo.falhas += 1
            restantes = self.cfg.max_retentativas - registro.falhas + 1
            self.log.error("FALHA (%dª): %s - %s. Nova tentativa nas próximas execuções (%d restante(s)).",
                           registro.falhas, rotulo, motivo, restantes)


def resumo_para_log(resumos: list[ResumoEmpresa]) -> list[str]:
    linhas = ["RESUMO DA EXECUÇÃO"]
    for r in resumos:
        partes = [
            f"{r.na_janela} na janela", f"{r.baixadas} baixada(s)", f"{r.ja_existiam} já existia(m)",
            f"{r.aguardando_xml} aguardando XML", f"{r.falhas} falha(s)", f"{r.desistidas} desistida(s)",
        ]
        if r.canceladas:
            partes.append(f"{r.canceladas} cancelada(s)")
        linhas.append(f"  {r.empresa:<25} " + " | ".join(partes))
        if r.erro_geral:
            linhas.append(f"  {'':<25} ERRO: {r.erro_geral}")
        for a in r.arquivos:
            linhas.append(f"  {'':<25} + {a}")
    return linhas


def resumo_para_json(resumos: list[ResumoEmpresa]) -> dict:
    return {"empresas": [asdict(r) for r in resumos]}
