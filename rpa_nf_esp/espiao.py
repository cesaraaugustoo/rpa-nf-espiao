"""Automação da tela Histórico > NF-e do Espião NF-e (app.espiaonfe.com.br).

Decisões de robustez:
- A lista de notas vem da resposta da própria pesquisa (POST espiaoCtrl, ação "pesqNfe",
  JSON em base64), capturada no clique em "Pesquisar". É a fonte da verdade: diz exatamente
  quais notas existem, inclusive "nenhuma", sem depender de tempo de renderização. Se o
  formato mudar, cai para a leitura da tabela na tela, localizando colunas pelo cabeçalho.
- Cada linha é localizada pela chave de acesso (única), nunca pela posição.
- O nome do arquivo entregue pelo site é "<chave>-danfe.pdf" (DANFE) ou "<chave>.xml" (XML);
  se não contiver a chave da linha clicada, o download é recusado.
"""

import base64
import json
import logging
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import Locator, Page, expect, sync_playwright
from playwright.sync_api import Error as ErroPlaywright
from playwright.sync_api import TimeoutError as TempoEsgotado

from .config import Config, Empresa
from .documentos import chave_valida, so_digitos

# Atalhos do seletor de período e quantos dias para trás cada um alcança.
ATALHOS_PERIODO = (("Hoje", 0), ("Últimos 7 dias", 6), ("Últimos 30 dias", 29))
ALCANCE_MAXIMO_DIAS = ATALHOS_PERIODO[-1][1]

_ACAO_PESQUISA = "ll=" + base64.b64encode(b"pesqNfe").decode().rstrip("=")
_TEXTO_MENU_DANFE = "Download Danfe em PDF"
_TEXTO_MENU_XML = "Download do XML"
TENTATIVAS_LOGIN = 3


class ErroEspiao(Exception):
    """Falha na interação com o site."""


class CredenciaisRecusadas(ErroEspiao):
    """O site recusou usuário/senha. Não adianta tentar de novo."""


class SemXml(ErroEspiao):
    """A nota só tem o resumo da SEFAZ; ainda não há DANFE para baixar."""


@dataclass(frozen=True)
class NotaEspiao:
    chave: str
    numero: str
    serie: str | None
    emissao: date
    emitente_cnpj: str
    emitente_nome: str
    destinatario_cnpj: str
    situacao: str
    valor: str | None
    tem_xml: bool

    @property
    def cancelada(self) -> bool:
        return "cancel" in self.situacao.lower()


def resumir_erro(e: BaseException) -> str:
    """Primeira linha da mensagem: o Playwright anexa um 'call log' longo que só polui o TXT."""
    texto = str(e).strip().splitlines()
    return f"{type(e).__name__}: {texto[0] if texto else ''}".strip()


@contextmanager
def sessao_espiao(cfg: Config, log: logging.Logger):
    """Abre o navegador, faz login (com novas tentativas) e entrega a tela de NF-e pronta."""
    with sync_playwright() as pw:
        navegador = pw.chromium.launch(headless=cfg.headless)
        contexto = navegador.new_context(
            accept_downloads=True,
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            viewport={"width": 1600, "height": 1000},
        )
        try:
            espiao = EspiaoNFe(contexto.new_page(), cfg, log, cfg.dir_logs / "capturas")
            espiao.entrar()
            yield espiao
        finally:
            contexto.close()
            navegador.close()


def decodificar_resposta(texto: str) -> list[dict]:
    bruto = texto.strip()
    if bruto.startswith('"'):
        bruto = json.loads(bruto)
    dados = json.loads(base64.b64decode(bruto, validate=True))
    if not isinstance(dados, list):
        raise ValueError(f"resposta inesperada: {type(dados).__name__}")
    return dados


def nota_da_api(d: dict) -> NotaEspiao:
    chave = so_digitos(str(d.get("chave_acesso", "")))
    if not chave_valida(chave):
        raise ValueError(f"chave inválida na resposta: {d.get('chave_acesso')!r}")
    if d.get("emissao"):
        emissao = datetime.strptime(str(d["emissao"]).strip()[:10], "%d/%m/%Y").date()
    else:
        emissao = date.fromisoformat(str(d["emissao_order"])[:10])
    arquivo_nfe = str(d.get("arquivo_nfe", "")).strip().lower()
    return NotaEspiao(
        chave=chave,
        numero=str(d.get("nota") or int(chave[25:34])),
        serie=str(d["serie"]) if d.get("serie") not in (None, "", "-") else None,
        emissao=emissao,
        emitente_cnpj=so_digitos(str(d.get("emitente_cnpj") or chave[6:20])),
        emitente_nome=str(d.get("emitente_nome") or "").strip(),
        destinatario_cnpj=so_digitos(str(d.get("destinatario_cnpj_cpf") or d.get("cnpj_cpf_responsavel") or "")),
        situacao=str(d.get("situacao_atual") or ""),
        valor=str(d["total"]) if d.get("total") is not None else None,
        tem_xml=arquivo_nfe not in ("f", "false", "0"),
    )


class EspiaoNFe:
    def __init__(self, page: Page, cfg: Config, log: logging.Logger, dir_capturas: Path):
        self.page = page
        self.cfg = cfg
        self.log = log
        self.dir_capturas = dir_capturas
        self.url_nfe = urljoin(cfg.url_login, "/history/nfe")
        page.set_default_timeout(cfg.timeout_ms)
        # Avisos/novidades em modal podem surgir a qualquer momento e cobrir a tela.
        self._modal = page.locator(".modal.show").first
        page.add_locator_handler(self._modal, self._fechar_modal, no_wait_after=True)

    # ------------------------------------------------------------------ sessão

    def entrar(self) -> None:
        """Login + tela de NF-e, com novas tentativas para falhas passageiras (nunca para senha recusada)."""
        for tentativa in range(1, TENTATIVAS_LOGIN + 1):
            try:
                self.login()
                self.abrir_nfe()
                return
            except CredenciaisRecusadas:
                self.capturar_tela("login_recusado")
                raise
            except Exception as e:
                self.capturar_tela(f"login_falhou_{tentativa}")
                if tentativa == TENTATIVAS_LOGIN:
                    raise
                espera = 10 * tentativa
                self.log.warning("Login falhou (tentativa %d/%d): %s. Nova tentativa em %ds",
                                 tentativa, TENTATIVAS_LOGIN, resumir_erro(e), espera)
                time.sleep(espera)

    def login(self) -> None:
        pg = self.page
        self.log.info("Acessando %s", self.cfg.url_login)
        pg.goto(self.cfg.url_login, wait_until="domcontentloaded")
        pg.locator("#formEmail").fill(self.cfg.usuario)
        pg.locator("#formPassword").fill(self.cfg.senha)
        pg.locator("#btn-login").click()
        try:
            pg.locator("#nav-3").wait_for(state="visible", timeout=self.cfg.timeout_ms)
        except TempoEsgotado:
            mensagem = self._mensagens_visiveis()
            if pg.locator("#formPassword").is_visible() and re.search(r"senha|inv[aá]lid|incorret", mensagem, re.I):
                raise CredenciaisRecusadas(f"login recusado pelo site: {mensagem}") from None
            raise ErroEspiao(f"login não concluído (mensagem na tela: {mensagem or 'nenhuma'})") from None
        self.log.info("Login efetuado")

    def abrir_nfe(self) -> None:
        pg = self.page
        menu = pg.locator("#nav-3")
        if menu.is_visible():
            menu.click()
        else:
            pg.goto(self.url_nfe, wait_until="domcontentloaded")
        pg.wait_for_url("**/history/nfe**")
        self._controle_select("Empresas").wait_for(state="visible")
        self._aguardar_tela_estavel()

    def recuperar(self) -> None:
        """Volta para um estado conhecido depois de um erro: tela NF-e, logando de novo se preciso."""
        pg = self.page
        self.log.info("Recuperando a sessão: recarregando a tela de NF-e")
        pg.goto(self.url_nfe, wait_until="domcontentloaded")
        try:
            self._controle_select("Empresas").wait_for(state="visible", timeout=15000)
            self._aguardar_tela_estavel()
        except TempoEsgotado:
            if pg.locator("#formEmail").is_visible():
                self.login()
            self.abrir_nfe()

    def _aguardar_tela_estavel(self) -> None:
        """Depois de carregar, a tela restaura sozinha a última empresa usada (de forma assíncrona).
        Mexer no seletor antes disso faz a empresa antiga voltar por cima da escolhida."""
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except TempoEsgotado:
            self.log.debug("Rede não ficou ociosa em 15s; seguindo")
        controle = self._controle_select("Empresas")
        anterior = None
        for _ in range(10):
            atual = controle.inner_text()
            if atual == anterior:
                return
            anterior = atual
            self.page.wait_for_timeout(300)

    # --------------------------------------------------------------- pesquisa

    def pesquisar(self, empresa: Empresa, desde: date, ate: date) -> list[NotaEspiao]:
        pg = self.page
        self._selecionar_empresa(empresa.cnpj)
        self._garantir_tipo("Recebidas")
        periodo = self._definir_periodo(desde, ate)
        if not self._empresa_selecionada(empresa.cnpj):  # mudou enquanto ajustávamos tipo/período
            self._selecionar_empresa(empresa.cnpj)
        self.log.info("Pesquisando %s, período %s", empresa.rotulo, periodo)

        with pg.expect_response(self._eh_resposta_pesquisa, timeout=self.cfg.timeout_ms * 2) as info:
            self._botao("Pesquisar").click()
        resposta = info.value
        if not resposta.ok:
            raise ErroEspiao(f"pesquisa respondeu HTTP {resposta.status}")

        try:
            notas = [nota_da_api(d) for d in decodificar_resposta(resposta.text())]
        except (ValueError, KeyError, TypeError) as e:
            self.log.warning("Resposta da pesquisa em formato inesperado (%s); lendo a tabela da tela", e)
            notas = self._ler_tabela()

        alheias = [n for n in notas if n.destinatario_cnpj and n.destinatario_cnpj != empresa.cnpj]
        if alheias:
            raise ErroEspiao(
                f"a pesquisa trouxe {len(alheias)} nota(s) de outro destinatário "
                f"(ex.: {alheias[0].destinatario_cnpj}); seleção de empresa não confirmada"
            )
        if notas:
            self._aguardar_tabela(len(notas))
        return notas

    def pesquisar_com_retentativa(self, empresa: Empresa, desde: date, ate: date) -> list[NotaEspiao]:
        """Pesquisa; se falhar, recarrega a tela (logando de novo se preciso) e tenta mais uma vez."""
        try:
            return self.pesquisar(empresa, desde, ate)
        except Exception as e:
            self.log.warning("Pesquisa de %s falhou (%s); recarregando a tela e tentando de novo",
                             empresa.rotulo, resumir_erro(e))
            self.capturar_tela(f"pesquisa_{empresa.cnpj}_1")
            self.recuperar()
            return self.pesquisar(empresa, desde, ate)

    @staticmethod
    def _eh_resposta_pesquisa(resposta) -> bool:
        if "espiaoCtrl" not in resposta.url or resposta.request.method != "POST":
            return False
        try:
            return _ACAO_PESQUISA in (resposta.request.post_data or "")
        except (UnicodeDecodeError, ErroPlaywright):
            return False

    def _controle_select(self, rotulo: str) -> Locator:
        return self.page.locator(
            f"xpath=//label[normalize-space()='{rotulo}']"
            "/following::div[contains(concat(' ', normalize-space(@class), ' '), ' select__control ')][1]"
        )

    def _empresa_selecionada(self, cnpj: str) -> bool:
        escolhidas = self._controle_select("Empresas").locator(".select__multi-value__label").all_inner_texts()
        return len(escolhidas) == 1 and escolhidas[0].strip().startswith(cnpj)

    def _selecionar_empresa(self, cnpj: str, tentativas: int = 4) -> None:
        """Deixa selecionada só a empresa `cnpj`.

        A tela restaura sozinha, de forma assíncrona, a última empresa usada na conta (inclusive
        por outra pessoa logada com o mesmo usuário). Por isso cada tentativa é independente,
        e o resultado só vale se continuar certo depois de uma pausa.
        """
        pg = self.page
        controle = self._controle_select("Empresas")
        for tentativa in range(1, tentativas + 1):
            try:
                limpar = controle.locator(".select__clear-indicator")
                if limpar.count():
                    limpar.click()
                expect(controle.locator(".select__multi-value")).to_have_count(0, timeout=5000)
                campo = controle.locator("input.select__input")
                campo.focus()
                if campo.get_attribute("aria-expanded") != "true":
                    campo.press("ArrowDown")
                campo.press_sequentially(cnpj, delay=25)
                opcao = pg.locator(".select__menu .select__option").filter(has_text=cnpj).first
                opcao.wait_for(state="visible", timeout=8000)
                opcao.click()
                pg.keyboard.press("Escape")
                pg.wait_for_timeout(1000)  # tempo para uma restauração tardia aparecer antes de conferir
                if self._empresa_selecionada(cnpj):
                    return
                motivo = f"seleção não confirmada: {controle.locator('.select__multi-value__label').all_inner_texts()}"
            except (TempoEsgotado, AssertionError) as e:
                motivo = resumir_erro(e)
            self.log.info("Selecionando empresa %s de novo (tentativa %d/%d): %s", cnpj, tentativa, tentativas, motivo)
            pg.keyboard.press("Escape")
            pg.wait_for_timeout(1000)
        raise ErroEspiao(f"não foi possível selecionar a empresa {cnpj} (ela está cadastrada nesta conta?)")

    def _garantir_tipo(self, tipo: str) -> None:
        controle = self._controle_select("Tipo")
        if not controle.count():
            self.log.warning("Campo 'Tipo' não encontrado; seguindo com o padrão da tela")
            return
        atual = controle.locator(".select__single-value")
        if atual.count() and atual.inner_text().strip() == tipo:
            return
        controle.click()
        self.page.locator(".select__menu .select__option").filter(
            has_text=re.compile(rf"^\s*{re.escape(tipo)}\s*$")
        ).first.click()
        expect(controle.locator(".select__single-value")).to_have_text(tipo)

    def _definir_periodo(self, desde: date, ate: date) -> str:
        pg = self.page
        dias = (ate - desde).days
        nome = next((n for n, alcance in ATALHOS_PERIODO if dias <= alcance), None)
        if nome is None:
            raise ErroEspiao(f"período de {dias} dias excede o maior atalho disponível")
        campo = pg.locator("input.rs-date-range-input")
        campo.click()
        popup = pg.locator(".rs-picker-popup")
        self._botao(nome, dentro=popup).click()
        # O atalho aplica e fecha o calendário sozinho. Só confirma com OK se ele continuar aberto
        # depois da animação de fechamento.
        try:
            expect(popup).to_have_count(0, timeout=3000)
        except AssertionError:
            self._botao("OK", dentro=popup).click()
            expect(popup).to_have_count(0, timeout=5000)

        valor = campo.input_value().strip()
        m = re.fullmatch(r"(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})", valor)
        if not m:
            raise ErroEspiao(f"período não aplicado (campo mostra {valor!r})")
        inicio = datetime.strptime(m.group(1), "%d/%m/%Y").date()
        fim = datetime.strptime(m.group(2), "%d/%m/%Y").date()
        if inicio > desde or fim < ate:
            raise ErroEspiao(f"período aplicado ({valor}) não cobre {desde:%d/%m/%Y} a {ate:%d/%m/%Y}")
        return valor

    # ----------------------------------------------------------------- tabela

    def _na_tela(self, seletor: str) -> Locator:
        """Só elementos visíveis: a janela do "Investigar XML", depois de fechada, continua
        escondida na página com uma tabela e uma paginação próprias."""
        return self.page.locator(seletor).filter(visible=True)

    def _paginacao(self) -> Locator:
        return self._na_tela(".MuiTablePagination-displayedRows")

    def _aguardar_tabela(self, total_esperado: int) -> None:
        self._na_tela("tbody tr").first.wait_for(state="visible")
        self._linhas_por_pagina(100)
        try:
            expect(self._paginacao()).to_have_text(re.compile(rf"de {total_esperado}\s*$"), timeout=10000)
        except AssertionError:
            self.log.warning(
                "Tabela mostra '%s', mas a pesquisa retornou %d nota(s); seguindo pela chave de acesso",
                self._paginacao().inner_text(), total_esperado,
            )

    def _linhas_por_pagina(self, quantidade: int) -> None:
        seletor = self._na_tela(".MuiTablePagination-select")
        if not seletor.count() or seletor.inner_text().strip() == str(quantidade):
            return
        seletor.click()
        self.page.locator("li[role='option']").filter(
            has_text=re.compile(rf"^\s*{quantidade}\s*$")
        ).first.click()
        expect(seletor).to_have_text(str(quantidade))

    def _ir_primeira_pagina(self) -> None:
        anterior = self._na_tela("button[aria-label='Ir para a página anterior']")
        while anterior.count() and anterior.is_enabled():
            texto = self._paginacao().inner_text()
            anterior.click()
            expect(self._paginacao()).not_to_have_text(texto)

    def _localizar_linha(self, chave: str) -> Locator:
        self._ir_primeira_pagina()
        while True:
            linha = self._na_tela("tbody tr").filter(has_text=chave)
            if linha.count():
                return linha.first
            proxima = self._na_tela("button[aria-label='Ir para a próxima página']")
            if not proxima.count() or proxima.is_disabled():
                raise ErroEspiao(f"linha da nota {chave} não encontrada na tabela")
            texto = self._paginacao().inner_text()
            proxima.click()
            expect(self._paginacao()).not_to_have_text(texto)

    def _ler_tabela(self) -> list[NotaEspiao]:
        """Plano B: lê as notas direto da tabela, achando as colunas pelo texto do cabeçalho."""
        try:
            self._na_tela("tbody tr").first.wait_for(state="visible", timeout=10000)
        except TempoEsgotado:
            self.log.warning("Nenhuma linha apareceu na tabela; considerando a pesquisa vazia")
            return []
        self._linhas_por_pagina(100)
        cabecalhos = [re.sub(r"\d+$", "", t).strip().lower() for t in self._na_tela("thead th").all_inner_texts()]

        def coluna(prefixo: str) -> int:
            for i, t in enumerate(cabecalhos):
                if t.startswith(prefixo):
                    return i
            raise ErroEspiao(f"coluna '{prefixo}' não encontrada na tabela (colunas: {cabecalhos})")

        i_emissao, i_nota, i_emitente = coluna("emiss"), coluna("nota"), coluna("emitente")
        i_chave, i_situacao = coluna("chave"), coluna("situa")
        i_dest = next((i for i, t in enumerate(cabecalhos) if t.startswith("destinat")), None)

        notas: list[NotaEspiao] = []
        self._ir_primeira_pagina()
        while True:
            for linha in self._na_tela("tbody tr").all():
                celulas = linha.locator("td").all_inner_texts()
                chave = so_digitos(celulas[i_chave])
                if not chave_valida(chave):
                    continue
                emitente = [p.strip() for p in celulas[i_emitente].splitlines() if p.strip()]
                notas.append(NotaEspiao(
                    chave=chave,
                    numero=celulas[i_nota].strip(),
                    serie=None,
                    emissao=datetime.strptime(celulas[i_emissao].strip()[:10], "%d/%m/%Y").date(),
                    emitente_cnpj=chave[6:20],
                    emitente_nome=emitente[-1] if emitente else "",
                    destinatario_cnpj=so_digitos(celulas[i_dest].splitlines()[0])[:14] if i_dest is not None else "",
                    situacao=celulas[i_situacao].strip(),
                    valor=None,
                    tem_xml=linha.locator("a[aria-label='Download']").count() > 0,
                ))
            proxima = self._na_tela("button[aria-label='Ir para a próxima página']")
            if not proxima.count() or proxima.is_disabled():
                return notas
            texto = self._paginacao().inner_text()
            proxima.click()
            expect(self._paginacao()).not_to_have_text(texto)

    # ------------------------------------------------------- investigar XML

    def investigar_xml(self, nota: NotaEspiao) -> str:
        """Aciona "Investigar XML" numa nota "Sem XML": o Espião busca o XML na SEFAZ na hora,
        em vez de esperar a distribuição automática. Devolve o texto mostrado na janela.

        Depois de fechar a janela é preciso pesquisar de novo para saber se o XML chegou.
        O handler que fecha modais sozinho é suspenso aqui: esta janela é aberta de propósito.
        """
        pg = self.page
        linha = self._localizar_linha(nota.chave)
        botao = linha.locator("a[aria-label='Investigar XML']")
        if not botao.count():
            raise ErroEspiao("a linha da nota não tem o botão 'Investigar XML'")
        pg.remove_locator_handler(self._modal)
        try:
            botao.scroll_into_view_if_needed()
            try:
                # O resgate costuma disparar uma chamada ao servidor; esperar por ela evita fechar cedo.
                with pg.expect_response(lambda r: "espiaoCtrl" in r.url, timeout=30000):
                    botao.click()
            except TempoEsgotado:
                self.log.debug("Nenhuma chamada ao servidor observada ao investigar o XML")
            janela = pg.locator(".modal.show").first
            janela.wait_for(state="visible", timeout=15000)
            carregando = janela.locator(".spinner-border, .spinner-grow, [role='progressbar']")
            try:
                expect(carregando).to_have_count(0, timeout=60000)
            except AssertionError:
                self.log.warning("A janela de 'Investigar XML' continuou carregando por 60s; fechando assim mesmo")
            pg.wait_for_timeout(1500)
            texto = " ".join(janela.inner_text().split())
            self._botao("Fechar", dentro=janela).click()
            expect(pg.locator(".modal.show")).to_have_count(0, timeout=15000)
            return texto
        finally:
            if pg.locator(".modal.show").count():  # erro no meio do caminho: não deixa a janela aberta
                pg.keyboard.press("Escape")
            pg.add_locator_handler(self._modal, self._fechar_modal, no_wait_after=True)

    # --------------------------------------------------------------- download

    def baixar_danfe(self, nota: NotaEspiao, pasta: Path) -> Path:
        return self._baixar(nota, pasta, _TEXTO_MENU_DANFE, f"{nota.chave}-danfe.pdf")

    def baixar_xml(self, nota: NotaEspiao, pasta: Path) -> Path:
        # Depois do download o site abre o aviso "Pronto! XML gerado com sucesso!", que cobre a tela até o OK.
        return self._baixar(nota, pasta, _TEXTO_MENU_XML, f"{nota.chave}.xml", aviso_ms=10000)

    def _baixar(self, nota: NotaEspiao, pasta: Path, item_menu: str, nome: str, aviso_ms: int = 0) -> Path:
        """Abre o menu de download da linha da nota, clica em `item_menu` e salva em `pasta/nome`.
        Com `aviso_ms`, espera esse tempo pelo aviso de conclusão do site e o fecha."""
        pg = self.page
        linha = self._localizar_linha(nota.chave)
        botao = linha.locator("a[aria-label='Download']")
        if not botao.count():
            if linha.get_by_text("Sem XML").count():
                raise SemXml("nota sem XML no Espião (só o resumo da SEFAZ)")
            raise ErroEspiao("botão de download não encontrado na linha da nota")
        botao.scroll_into_view_if_needed()
        botao.click()
        item = self._na_tela("li[role='menuitem']").filter(has_text=item_menu).first
        item.wait_for(state="visible", timeout=10000)
        with pg.expect_download(timeout=self.cfg.timeout_download_ms) as info:
            item.click()
        download = info.value
        if aviso_ms:
            self._fechar_aviso(aviso_ms)
        falha = download.failure()
        if falha:
            raise ErroEspiao(f"download falhou: {falha}")
        if nota.chave not in download.suggested_filename:
            download.delete()
            raise ErroEspiao(f"o site entregou outro arquivo ({download.suggested_filename})")
        pasta.mkdir(parents=True, exist_ok=True)
        destino = pasta / nome
        download.save_as(destino)
        self.fechar_menus()
        return destino

    # ------------------------------------------------------------- utilidades

    def _botao(self, texto: str, dentro: Locator | None = None) -> Locator:
        """Botão pelo texto exato, via CSS.

        Não usar get_by_role nesta tela: depois de um download, o menu MUI do site deixa
        aria-hidden="true" no #root, e todos os botões somem da árvore de acessibilidade
        (continuam visíveis e clicáveis para o usuário).
        """
        base = dentro if dentro is not None else self.page
        return base.locator("button").filter(has_text=re.compile(rf"^\s*{re.escape(texto)}\s*$")).first

    def fechar_menus(self) -> None:
        if self.page.locator("[role='menu']").count():
            self.page.keyboard.press("Escape")
        if self._botao_aviso().is_visible():
            self._fechar_aviso(1000)

    def _botao_aviso(self) -> Locator:
        return self.page.locator(".swal2-container .swal2-confirm").first

    def _fechar_aviso(self, espera_ms: int) -> None:
        """Fecha o aviso SweetAlert do site (ex.: "XML gerado com sucesso!"), que bloqueia os cliques."""
        ok = self._botao_aviso()
        try:
            ok.wait_for(state="visible", timeout=espera_ms)
        except TempoEsgotado:
            self.log.debug("Aviso de conclusão não apareceu em %d ms; seguindo", espera_ms)
            return
        ok.click()
        expect(self.page.locator(".swal2-container")).to_have_count(0, timeout=10000)

    def _fechar_modal(self, modal: Locator) -> None:
        botao = modal.locator("button.close, .btn-close, [aria-label='Close'], [aria-label='Fechar']")
        if botao.count():
            botao.first.click()
        else:
            self.page.keyboard.press("Escape")

    def _mensagens_visiveis(self) -> str:
        textos = []
        for sel in (".alert", ".invalid-feedback", ".Toastify__toast", ".swal2-html-container", ".text-danger"):
            for t in self.page.locator(sel).all_inner_texts():
                t = " ".join(t.split())
                if t:
                    textos.append(t)
        return " | ".join(textos)[:500]

    def capturar_tela(self, contexto: str) -> Path | None:
        try:
            self.dir_capturas.mkdir(parents=True, exist_ok=True)
            nome = re.sub(r"[^\w-]+", "_", contexto)[:60]
            caminho = self.dir_capturas / f"{datetime.now():%Y%m%d_%H%M%S}_{nome}.png"
            self.page.screenshot(path=caminho, full_page=True)
            return caminho
        except Exception as e:
            self.log.debug("Captura de tela falhou: %s", e)
            return None
