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
from .ledger import CONCLUIDAS, DadosNota, Ledger, RegistroNota, Status
from .nomes import base_nome, gravar_ao_lado, gravar_sem_sobrescrever, normalizar
from .pastas import GerenciadorPastas
from .pdf import IndiceChaves, validar_danfe
from .xml_nfe import validar_xml_nfe

@dataclass
class ResumoEmpresa:
    empresa: str
    na_janela: int = 0
    baixadas: int = 0
    xmls: int = 0                                        # XMLs salvos ao lado do PDF (também de notas anteriores)
    ja_existiam: int = 0
    aguardando_xml: int = 0
    falhas: int = 0
    desistidas: int = 0
    canceladas: int = 0
    erro_geral: str | None = None
    arquivos: list[str] = field(default_factory=list)
    desistencias: list[str] = field(default_factory=list)  # "NF ... - motivo", para o alerta por e-mail

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
        self._tela_suja = False               # um download de XML falhou: a tela precisa ser restaurada

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
        xml_pendentes = self.ledger.xml_pendentes(empresa.cnpj, limite)
        desde = min([inicio_janela] + [r.emissao for r in alcancaveis] + [r.emissao for r in xml_pendentes])
        if alcancaveis:
            self.log.info("%d nota(s) em aberto de execuções anteriores serão tentadas de novo", len(alcancaveis))
        if xml_pendentes:
            self.log.info("%d nota(s) já baixada(s) ainda sem o XML ao lado do PDF", len(xml_pendentes))

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
        self._tela_suja = False
        for nota in ordenados:
            self._processar_nota_protegido(espiao, empresa, nota, resumo)
            if self._tela_suja:
                self._restaurar_tela(espiao, empresa, desde)
        for i, registro in enumerate(xml_pendentes):
            try:
                self._xml_pendente(espiao, empresa, registro, por_chave.get(registro.chave), resumo)
            except Exception as e:
                self.log.debug("Erro inesperado no XML da NF %s (%s)", registro.numero, registro.chave, exc_info=True)
                self._falha_xml(registro.chave, f"NF {registro.numero} - {registro.emitente_nome}",
                                f"erro inesperado: {resumir_erro(e)}", resumo)
                self._tela_suja = True
            if self._tela_suja and not self._restaurar_tela(espiao, empresa, desde):
                self.log.warning("%d XML(s) pendente(s) de %s ficam para a próxima execução, sem gastar tentativa.",
                                 len(xml_pendentes) - i - 1, empresa.rotulo)
                break
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

    def _restaurar_tela(self, espiao: EspiaoNFe, empresa: Empresa, desde: date) -> bool:
        """Depois de uma falha no meio de um download do XML (sessão derrubada por um login com a
        mesma conta, aviso aberto...), volta à tela de NF-e e refaz a pesquisa. Sem isso as notas
        seguintes da empresa falhariam todas por "linha não encontrada", gastando tentativas."""
        self._tela_suja = False
        self._tentar_recuperar(espiao)
        try:
            espiao.pesquisar_com_retentativa(empresa, desde, self.hoje)
            return True
        except Exception as e:
            self.log.warning("A pesquisa de %s não voltou depois da falha: %s", empresa.rotulo, resumir_erro(e))
            return False

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
            self.log.info("[ensaio] baixaria %s -> %s\\%s%s.pdf + .xml (apelido: %s)",
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
        self._baixar_xml(espiao, nota, destino, rotulo, resumo)

    # -------------------------------------------------------------------- XML

    def _xml_pendente(self, espiao: EspiaoNFe, empresa: Empresa, registro: RegistroNota,
                      nota: NotaEspiao | None, resumo: ResumoEmpresa) -> None:
        """XML de uma nota baixada em execução anterior (falha anterior ou nota de antes desta função)."""
        rotulo = f"NF {registro.numero} de {registro.emissao:%d/%m/%Y} - {registro.emitente_nome}"
        pdf = self._localizar_pdf(empresa, registro)
        if pdf is None:
            self.log.warning("XML não baixado: o PDF de %s não está mais na pasta (renomeado ou movido?). "
                             "Baixar o XML à mão, se precisar.", rotulo)
            if not self.ensaio:
                self.ledger.desistir_xml(registro.chave, "PDF não encontrado na pasta")
            return
        if nota is None:
            self._falha_xml(registro.chave, rotulo, "a nota não apareceu na pesquisa do Espião", resumo)
            return
        self._baixar_xml(espiao, nota, pdf, rotulo, resumo)

    def _localizar_pdf(self, empresa: Empresa, registro: RegistroNota) -> Path | None:
        """PDF da nota na pasta do mês (pela chave dentro do arquivo, mesmo se renomeado) ou no caminho gravado."""
        pasta = self.pastas.pasta_do_mes(empresa, registro.emissao, self.hoje, criar=False)
        nome = self.indice.chaves_na_pasta(pasta).get(registro.chave)
        if nome:
            return pasta / nome
        salvo = Path(registro.arquivo) if registro.arquivo else None
        return salvo if salvo and salvo.is_file() else None

    def _baixar_xml(self, espiao: EspiaoNFe, nota: NotaEspiao, pdf: Path, rotulo: str, resumo: ResumoEmpresa) -> None:
        """Salva o XML ao lado do PDF, com o mesmo nome. Falha aqui não desfaz o PDF: o XML fica
        pendente e é tentado de novo nas próximas execuções."""
        destino = pdf.with_suffix(".xml")
        if self.ensaio:
            self.log.info("[ensaio] baixaria o XML de %s -> %s\\%s", rotulo, pdf.parent.name, destino.name)
            return
        try:
            if destino.exists():  # já estava lá (salvo à mão): só confere e registra
                validar_xml_nfe(destino, nota.chave)
            else:
                temporario = espiao.baixar_xml(nota, self.staging)
                validar_xml_nfe(temporario, nota.chave)
                destino = gravar_ao_lado(temporario, pdf, ".xml")
                temporario.unlink(missing_ok=True)
        except Exception as e:
            espiao.capturar_tela(f"xml_{nota.chave}")
            self.log.debug("Detalhes da falha no XML", exc_info=True)
            self._falha_xml(nota.chave, rotulo, resumir_erro(e), resumo)
            self._tela_suja = True
            return
        self.ledger.marcar_xml(nota.chave, str(destino))
        resumo.xmls += 1
        self.log.info("XML: %s -> %s\\%s", rotulo, destino.parent.name, destino.name)

    def _falha_xml(self, chave: str, rotulo: str, motivo: str, resumo: ResumoEmpresa) -> None:
        if self.ensaio:
            self.log.warning("[ensaio] XML de %s não seria baixado: %s", rotulo, motivo)
            return
        falhas = self.ledger.registrar_falha_xml(chave, motivo)
        if falhas > self.cfg.max_retentativas:
            self.ledger.desistir_xml(chave, motivo)
            resumo.desistencias.append(f"{rotulo} - XML: {motivo} (após {falhas} tentativas)")
            self.log.error("XML DESISTIDO após %d tentativas: %s - %s. Baixar o XML à mão.", falhas, rotulo, motivo)
        else:
            self.log.warning("XML não baixado (%dª falha): %s - %s. Nova tentativa na próxima execução.",
                             falhas, rotulo, motivo)

    def _aguardar_xml(self, nota: NotaEspiao, empresa: Empresa, rotulo: str, resumo: ResumoEmpresa) -> None:
        idade = (self.hoje - nota.emissao).days
        if idade > self.cfg.dias_aguardar_xml:
            motivo = f"XML não disponibilizado pela SEFAZ em {idade} dias"
            self.log.error("DESISTIDA: %s - %s. Baixar manualmente.", rotulo, motivo)
            if not self.ensaio:
                self.ledger.marcar(_dados(nota, empresa), Status.DESISTIDA, erro=motivo)
            resumo.desistidas += 1
            resumo.desistencias.append(f"{rotulo} - {motivo}")
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
        resumo.desistencias.append(f"{rotulo} - {motivo}")
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
            resumo.desistencias.append(f"{rotulo} - {motivo} (após {registro.falhas} tentativas)")
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
            f"{r.na_janela} na janela", f"{r.baixadas} baixada(s)", f"{r.xmls} XML",
            f"{r.ja_existiam} já existia(m)",
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
