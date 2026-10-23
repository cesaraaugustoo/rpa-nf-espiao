"""Linha de comando do RPA.

    python -m rpa_nf_esp                    execução normal (agendada de hora em hora)
    python -m rpa_nf_esp --ensaio           pesquisa e mostra o que faria, sem baixar nem mover nada
    python -m rpa_nf_esp --visivel          mostra o navegador
    python -m rpa_nf_esp --empresa <CNPJ>
    python -m rpa_nf_esp --somente-pastas   só a virada de mês/ano das pastas
    python -m rpa_nf_esp --pendencias       lista notas em aberto e desistidas
    python -m rpa_nf_esp --reprocessar <chave>

Códigos de saída: 0 tudo certo | 1 terminou com falhas | 2 erro fatal | 3 já havia outra execução rodando.
"""

import argparse
import msvcrt
import sys
import time
from dataclasses import replace
from datetime import date
from pathlib import Path

from . import __version__
from . import apelidos as mod_apelidos
from . import config as mod_config
from . import logs
from .conferencia import Conferencia
from .documentos import formatar_cnpj, so_digitos
from .espiao import CredenciaisRecusadas, resumir_erro
from .executor import Executor, resumo_para_json, resumo_para_log
from .ledger import EM_ABERTO, Ledger, Status

SAIDA_OK, SAIDA_FALHAS, SAIDA_FATAL, SAIDA_OCUPADO = 0, 1, 2, 3


class TravaExecucao:
    """Impede duas execuções simultâneas (ex.: agendamento disparando com a anterior ainda rodando)."""

    def __init__(self, caminho: Path):
        self.caminho = caminho
        self._arquivo = None

    def __enter__(self) -> "TravaExecucao":
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        self._arquivo = open(self.caminho, "a+")
        self._arquivo.seek(0)  # trava e destrava sempre o mesmo byte (posição 0)
        try:
            msvcrt.locking(self._arquivo.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self._arquivo.close()
            raise BlockingIOError(f"outra execução está em andamento (trava: {self.caminho})") from None
        return self

    def __exit__(self, *exc) -> None:
        try:
            self._arquivo.seek(0)
            msvcrt.locking(self._arquivo.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._arquivo.close()


def _argumentos(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="rpa_nf_esp", description="Baixa DANFEs do Espião NF-e para as pastas de notas.")
    p.add_argument("--ensaio", action="store_true", help="não baixa, não grava e não move nada; só registra no log o que faria")
    p.add_argument("--visivel", action="store_true", help="abre o navegador visível")
    p.add_argument("--empresa", action="append", metavar="CNPJ", help="processa só esta empresa (pode repetir)")
    p.add_argument("--somente-pastas", action="store_true", help="só aplica a virada de mês/ano nas pastas")
    p.add_argument("--pendencias", action="store_true", help="lista as notas em aberto e desistidas e sai")
    p.add_argument("--reprocessar", metavar="CHAVE", help="devolve uma nota para a fila de tentativas e sai")
    p.add_argument("--conferir", nargs="?", const=29, type=int, metavar="DIAS",
                   help="confere Espião x pastas nos últimos DIAS (padrão e máximo 29), sem baixar nada")
    p.add_argument("--hoje", metavar="AAAA-MM-DD", help="simula outra data (só junto com --ensaio --somente-pastas)")
    p.add_argument("--detalhado", action="store_true", help="log em nível de depuração")
    p.add_argument("--config", type=Path, help="caminho alternativo do config.toml")
    p.add_argument("--versao", action="version", version=f"%(prog)s {__version__}")
    a = p.parse_args(argv)
    if a.hoje and not (a.ensaio and a.somente_pastas):
        p.error("--hoje só é permitido junto com --ensaio --somente-pastas (para não mover pastas reais por engano)")
    return a


def _listar_pendencias(ledger: Ledger, cfg: mod_config.Config) -> None:
    nomes = {e.cnpj: e.pasta for e in cfg.empresas}
    registros = ledger.listar((*EM_ABERTO, Status.DESISTIDA))
    if not registros:
        print("Nenhuma nota em aberto ou desistida.")
        return
    print(f"{'STATUS':<15} {'EMPRESA':<24} {'EMISSÃO':<10} {'NF':>9}  {'FALHAS':>6}  EMITENTE / CHAVE / ÚLTIMO ERRO")
    for r in registros:
        print(f"{r.status:<15} {nomes.get(r.empresa_cnpj, r.empresa_cnpj):<24} {r.emissao:%d/%m/%Y} "
              f"{(r.numero or ''):>9}  {r.falhas:>6}  {r.emitente_nome}")
        print(f"{'':<70}{r.chave}")
        if r.ultimo_erro:
            print(f"{'':<70}{r.ultimo_erro[:150]}")


def principal(argv: list[str] | None = None) -> int:
    args = _argumentos(argv)
    try:
        cfg = mod_config.carregar(args.config)
    except mod_config.ErroConfiguracao as e:
        print(f"ERRO DE CONFIGURAÇÃO: {e}", file=sys.stderr)
        return SAIDA_FATAL
    if args.visivel:
        cfg = replace(cfg, headless=False)

    log = logs.configurar(cfg.dir_logs, cfg.dias_manter_logs, args.detalhado)
    ledger = Ledger(cfg.dir_dados / "rpa.sqlite3")
    try:
        if args.pendencias:
            _listar_pendencias(ledger, cfg)
            return SAIDA_OK
        if args.reprocessar:
            chave = so_digitos(args.reprocessar)
            ok = ledger.reprocessar(chave)
            log.info("Reprocessamento manual da chave %s: %s", chave, "na fila" if ok else "chave não encontrada")
            return SAIDA_OK if ok else SAIDA_FALHAS

        empresas = cfg.empresas
        if args.empresa:
            pedidas = {so_digitos(c) for c in args.empresa}
            empresas = tuple(e for e in cfg.empresas if e.cnpj in pedidas)
            desconhecidas = pedidas - {e.cnpj for e in empresas}
            if desconhecidas:
                log.critical("CNPJ fora do config.toml: %s", ", ".join(formatar_cnpj(c) for c in desconhecidas))
                return SAIDA_FATAL
        hoje = date.fromisoformat(args.hoje) if args.hoje else date.today()

        with TravaExecucao(cfg.dir_dados / "rpa.lock"):
            if args.conferir is not None:
                return _conferir(cfg, log, ledger, empresas, hoje, args.conferir)
            return _executar(cfg, log, ledger, args, empresas, hoje)
    except BlockingIOError as e:
        log.warning("Execução ignorada: %s", e)
        return SAIDA_OCUPADO
    finally:
        ledger.fechar()


def _conferir(cfg, log, ledger, empresas, hoje, dias) -> int:
    log.info("=" * 70)
    log.info("CONFERÊNCIA iniciada (somente leitura, últimos %d dias)", min(dias, 29))
    try:
        apelidos = mod_apelidos.carregar(cfg, log)
        ok, relatorio = Conferencia(cfg, log, ledger, apelidos, hoje, dias).executar(empresas)
    except Exception as e:
        log.critical("Conferência não concluída: %s", resumir_erro(e))
        log.debug("Detalhes", exc_info=True)
        return SAIDA_FATAL
    log.info("Relatório salvo em %s", relatorio)
    return SAIDA_OK if ok else SAIDA_FALHAS


def _executar(cfg, log, ledger, args, empresas, hoje) -> int:
    inicio = time.monotonic()
    execucao = ledger.iniciar_execucao(args.ensaio)
    log.info("=" * 70)
    log.info("EXECUÇÃO #%d iniciada (versão %s)%s%s", execucao, __version__,
             " - MODO ENSAIO, nada será gravado" if args.ensaio else "",
             f" - data simulada {hoje:%d/%m/%Y}" if args.hoje else "")
    resumos = []
    try:
        executor = Executor(cfg, log, ledger, ensaio=args.ensaio, empresas=empresas, hoje=hoje)
        resumos = executor.executar(somente_pastas=args.somente_pastas)
    except mod_config.ErroConfiguracao as e:
        log.critical("Erro de configuração: %s", e)
        ledger.finalizar_execucao("FATAL", {"erro": str(e)})
        return SAIDA_FATAL
    except CredenciaisRecusadas as e:
        log.critical("%s. Confira ESPIAO_USUARIO/ESPIAO_SENHA no .env", e)
        ledger.finalizar_execucao("FATAL", {"erro": str(e)})
        return SAIDA_FATAL
    except KeyboardInterrupt:
        log.warning("Execução interrompida pelo usuário")
        ledger.finalizar_execucao("INTERROMPIDA", resumo_para_json(resumos))
        return SAIDA_FALHAS
    except Exception as e:
        log.exception("Erro fatal: %s", e)
        ledger.finalizar_execucao("FATAL", {"erro": str(e)})
        return SAIDA_FATAL

    for linha in resumo_para_log(resumos):
        log.info(linha)
    situacao = "OK" if all(r.ok for r in resumos) else "COM FALHAS"
    ledger.finalizar_execucao(situacao, resumo_para_json(resumos))
    log.info("EXECUÇÃO #%d concluída em %.0fs - %s", execucao, time.monotonic() - inicio, situacao)
    return SAIDA_OK if situacao == "OK" else SAIDA_FALHAS


if __name__ == "__main__":
    sys.exit(principal())
