"""Validação do DANFE baixado e índice das chaves de acesso dos PDFs já existentes.

O índice é o que impede baixar de novo uma nota que alguém já salvou à mão: a chave de
acesso está impressa no DANFE, então lemos o texto dos PDFs da pasta e comparamos chaves,
independentemente do nome que o arquivo recebeu. O resultado fica em cache no ledger
(por caminho + tamanho + data de modificação), para não reler o mesmo PDF a cada execução.
"""

import logging
import re
from pathlib import Path

from pypdf import PdfReader

from .documentos import chave_valida
from .ledger import Ledger

TAMANHO_MINIMO = 1024
_GRUPOS = re.compile(r"(?<!\d)(?:\d{4}[  ]?){10}\d{4}(?!\d)")
_CORRIDA = re.compile(r"(?<!\d)\d{44}(?!\d)")
_NUMERO_NF = re.compile(r"N[º°o]?\s*\.?\s*:?\s*(\d{1,3}(?:\.\d{3}){1,2}|\d{1,9})(?![\d.])")


class PdfInvalido(Exception):
    pass


def chaves_proprias(texto: str) -> list[str]:
    """A chave de acesso da própria nota, a partir do texto do DANFE.

    Um DANFE pode citar chaves de outras notas (ex.: "DF-e Ref." em Informações Complementares).
    Com uma só chave válida no texto, ela é a da nota. Com várias, vale a que tem o mesmo número
    da NF impresso no cabeçalho ("Nº. 000.019.095"). Se ainda assim for ambíguo, devolve vazio:
    o arquivo é tratado como ilegível, nunca como prova de que outra nota já está na pasta.
    """
    candidatas = [re.sub(r"\D", "", m) for m in _GRUPOS.findall(texto)] + _CORRIDA.findall(texto)
    todas = list(dict.fromkeys(c for c in candidatas if chave_valida(c)))
    if len(todas) <= 1:
        return todas
    numeros = {int(n.replace(".", "")) for n in _NUMERO_NF.findall(texto)}
    proprias = [c for c in todas if int(c[25:34]) in numeros]
    return proprias if len(proprias) == 1 else []


def extrair_chaves(caminho: Path, paginas: int = 2) -> list[str]:
    """Chave de acesso da própria nota (lista vazia se não houver texto legível ou for ambíguo)."""
    leitor = PdfReader(caminho)
    return chaves_proprias("\n".join((p.extract_text() or "") for p in leitor.pages[:paginas]))


def validar_danfe(caminho: Path, chave_esperada: str) -> bool:
    """Garante que o arquivo é um PDF íntegro do DANFE certo.

    Devolve True se a chave foi confirmada no texto; False se o PDF não tem texto legível
    (aceito, pois o download já foi casado pela chave). Levanta PdfInvalido se o arquivo
    não for PDF, estiver corrompido ou trouxer a chave de outra nota.
    """
    tamanho = caminho.stat().st_size
    if tamanho < TAMANHO_MINIMO:
        raise PdfInvalido(f"arquivo muito pequeno ({tamanho} bytes)")
    with caminho.open("rb") as f:
        cabecalho = f.read(5)
    if cabecalho != b"%PDF-":
        raise PdfInvalido(f"o arquivo não é PDF (começa com {cabecalho!r})")
    try:
        chaves = extrair_chaves(caminho)
    except Exception as e:  # pypdf levanta tipos variados para PDF corrompido
        raise PdfInvalido(f"PDF ilegível: {e}") from e
    if not chaves:
        return False
    if chave_esperada not in chaves:
        raise PdfInvalido(f"o PDF é de outra nota (chaves no documento: {', '.join(chaves)})")
    return True


class IndiceChaves:
    def __init__(self, ledger: Ledger, log: logging.Logger):
        self._ledger = ledger
        self._log = log
        self._pastas: dict[Path, dict[str, str]] = {}
        self._ilegiveis: dict[Path, list[str]] = {}

    def chaves_na_pasta(self, pasta: Path) -> dict[str, str]:
        """chave -> nome do arquivo, para todos os PDFs da pasta."""
        if pasta in self._pastas:
            return self._pastas[pasta]
        mapa: dict[str, str] = {}
        ilegiveis: list[str] = []
        if pasta.is_dir():
            for arq in sorted(pasta.iterdir()):
                if arq.suffix.lower() != ".pdf" or not arq.is_file():
                    continue
                chaves = self.chaves_do_arquivo(arq)
                if not chaves:
                    ilegiveis.append(arq.name)
                for chave in chaves:
                    mapa.setdefault(chave, arq.name)
        self._pastas[pasta] = mapa
        self._ilegiveis[pasta] = ilegiveis
        return mapa

    def ilegiveis_na_pasta(self, pasta: Path) -> list[str]:
        """PDFs da pasta sem chave de acesso legível (digitalizados, fotos, arquivos que não são PDF)."""
        self.chaves_na_pasta(pasta)
        return self._ilegiveis.get(pasta, [])

    def registrar(self, arquivo: Path, chave: str) -> None:
        self._pastas.setdefault(arquivo.parent, {})[chave] = arquivo.name

    def chaves_do_arquivo(self, arq: Path) -> list[str]:
        try:
            st = arq.stat()
        except OSError as e:
            self._log.warning("Não foi possível ler %s para conferir duplicidade: %s", arq, e)
            return []
        em_cache = self._ledger.cache_pdf(str(arq), st.st_size, st.st_mtime_ns)
        if em_cache is not None:
            return em_cache
        try:
            chaves = extrair_chaves(arq)
        except OSError as e:
            # Acesso ao arquivo falhou (ex.: arquivo "somente online" do OneDrive com o OneDrive
            # fechado). Não vai para o cache: na próxima execução o arquivo é lido de novo.
            self._log.warning("Não foi possível ler %s para conferir duplicidade: %s", arq, e)
            return []
        except Exception as e:
            self._log.debug("PDF sem texto legível (ignorado no índice): %s (%s)", arq.name, e)
            chaves = []
        self._ledger.salvar_cache_pdf(str(arq), st.st_size, st.st_mtime_ns, chaves)
        return chaves
