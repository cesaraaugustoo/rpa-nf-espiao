"""Log em TXT: um arquivo por dia em logs/rpa_AAAA-MM-DD.txt, mais a saída no console."""

import logging
import sys
import time
from datetime import date
from pathlib import Path

NOME_LOGGER = "rpa_nf_esp"
FORMATO = "%(asctime)s | %(levelname)-5s | %(message)s"
FORMATO_DATA = "%Y-%m-%d %H:%M:%S"

logging.addLevelName(logging.WARNING, "AVISO")
logging.addLevelName(logging.ERROR, "ERRO")
logging.addLevelName(logging.CRITICAL, "FATAL")


def configurar(dir_logs: Path, dias_manter: int, detalhado: bool = False) -> logging.Logger:
    dir_logs.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(NOME_LOGGER)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    for h in list(log.handlers):
        log.removeHandler(h)
        h.close()

    formato = logging.Formatter(FORMATO, FORMATO_DATA)

    arquivo = logging.FileHandler(dir_logs / f"rpa_{date.today():%Y-%m-%d}.txt", encoding="utf-8")
    arquivo.setLevel(logging.DEBUG if detalhado else logging.INFO)
    arquivo.setFormatter(formato)
    log.addHandler(arquivo)

    # Sob pythonw (Agendador de Tarefas) não existe console: sys.stdout é None.
    if sys.stdout is not None:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.DEBUG if detalhado else logging.INFO)
        console.setFormatter(formato)
        log.addHandler(console)

    # Mensagens internas do pypdf sobre PDFs malformados não interessam ao operador.
    logging.getLogger("pypdf").setLevel(logging.ERROR)

    _limpar_antigos(dir_logs, dias_manter, log)
    return log


def _limpar_antigos(dir_logs: Path, dias_manter: int, log: logging.Logger) -> None:
    limite = time.time() - dias_manter * 86400
    for padrao in ("rpa_*.txt", "capturas/*.png"):
        for arq in dir_logs.glob(padrao):
            try:
                if arq.stat().st_mtime < limite:
                    arq.unlink()
            except OSError as e:
                log.debug("não foi possível remover log antigo %s: %s", arq, e)
