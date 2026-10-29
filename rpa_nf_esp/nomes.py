"""Nome do arquivo no padrão da casa e gravação segura na pasta de destino.

Padrão: "APELIDO DDMMAA_RPA.pdf"; a 2ª, 3ª... nota do mesmo fornecedor no mesmo dia
ganha " 2", " 3" antes do sufixo: "ACME 231026 2_RPA.pdf".
A contagem considera também os arquivos salvos à mão (sem "_RPA"), e compara sem acento
e sem caixa: "CAFE BOM 221026.pdf" conta como a 1ª nota de "CAFÉ BOM 221026".
O XML da nota fica ao lado do PDF, com o mesmo nome: "ACME 231026 2_RPA.xml".
"""

import os
import re
import shutil
import time
import unicodedata
import uuid
from datetime import date
from pathlib import Path

_INVALIDOS = re.compile(r'[<>:"|?*\x00-\x1f]')
_RESERVADOS = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
TAMANHO_MAX_NOME = 100


def sem_acento(texto: str) -> str:
    decomposto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in decomposto if not unicodedata.combining(c))


def normalizar(texto: str) -> str:
    return re.sub(r"\s+", " ", sem_acento(texto)).strip().upper()


def limpar_para_arquivo(texto: str) -> str:
    t = texto.replace("/", "-").replace("\\", "-")
    t = _INVALIDOS.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = t[:TAMANHO_MAX_NOME].rstrip(". ")
    if not t or t.upper() in _RESERVADOS:
        t = f"FORNECEDOR {t}".strip()
    return t


def base_nome(apelido: str, emissao: date) -> str:
    return f"{limpar_para_arquivo(apelido)} {emissao:%d%m%y}"


def proximo_nome(pasta: Path, base: str, sufixo: str) -> str:
    padrao = re.compile(
        rf"^{re.escape(normalizar(base))}(?: (\d+))?(?:{re.escape(normalizar(sufixo))})?\.PDF$"
    )
    usados = []
    if pasta.is_dir():
        for arq in pasta.iterdir():
            m = padrao.match(normalizar(arq.name))
            if m:
                usados.append(int(m.group(1) or 1))
    if not usados:
        return f"{base}{sufixo}.pdf"
    return f"{base} {max(usados) + 1}{sufixo}.pdf"


def renomear_sem_sobrescrever(origem: Path, destino: Path) -> None:
    """Renomeia `origem` para `destino`; FileExistsError se o destino já existir.

    No Windows o próprio rename já recusa destino existente. No Linux o rename substitui
    o destino sem avisar, então o nome é reservado antes com uma criação exclusiva
    (que falha se o arquivo existir) e só então o conteúdo toma o lugar da reserva.
    Não usa link físico: a pasta do OneDrive na VPS (rclone mount) não o suporta.
    """
    if os.name == "nt":
        os.rename(origem, destino)
        return
    with open(destino, "xb"):
        pass
    try:
        os.replace(origem, destino)
    except BaseException:
        destino.unlink(missing_ok=True)
        raise


def gravar_sem_sobrescrever(origem: Path, pasta: Path, base: str, sufixo: str) -> Path:
    """Copia `origem` para `pasta` com o próximo nome livre. Nunca sobrescreve um arquivo.

    A cópia vai primeiro para um temporário na própria pasta e depois é renomeada sem
    sobrescrever, o que fecha a janela de corrida com alguém salvando um arquivo de
    mesmo nome ao mesmo tempo.
    """
    pasta.mkdir(parents=True, exist_ok=True)
    temporario = pasta / f".~rpa-{uuid.uuid4().hex}.part"
    try:
        shutil.copyfile(origem, temporario)
        for _ in range(20):
            destino = pasta / proximo_nome(pasta, base, sufixo)
            try:
                renomear_sem_sobrescrever(temporario, destino)
                return destino
            except FileExistsError:
                continue
        raise OSError(f"não foi possível achar um nome livre para {base!r} em {pasta}")
    finally:
        _apagar_temporario(temporario)


def gravar_ao_lado(origem: Path, irmao: Path, extensao: str) -> Path:
    """Copia `origem` para a pasta de `irmao`, com o mesmo nome e outra extensão
    ("ACME 231026_RPA.pdf" -> "ACME 231026_RPA.xml"). FileExistsError se o destino já existir."""
    destino = irmao.with_suffix(extensao)
    temporario = irmao.parent / f".~rpa-{uuid.uuid4().hex}.part"
    try:
        shutil.copyfile(origem, temporario)
        renomear_sem_sobrescrever(temporario, destino)
        return destino
    finally:
        _apagar_temporario(temporario)


def _apagar_temporario(temporario: Path) -> None:
    # Nunca deixa o temporário na pasta de notas, nem deixa a limpeza esconder o erro original.
    for _ in range(3):
        try:
            temporario.unlink(missing_ok=True)
            break
        except OSError:
            time.sleep(1)
