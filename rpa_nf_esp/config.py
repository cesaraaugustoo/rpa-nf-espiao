"""Carga e validação da configuração (config/config.toml) e das credenciais (.env)."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .documentos import cnpj_valido

RAIZ_PROJETO = Path(__file__).resolve().parent.parent
CONFIG_PADRAO = RAIZ_PROJETO / "config" / "config.toml"


class ErroConfiguracao(Exception):
    """Configuração ausente ou inválida. A execução não deve prosseguir."""


@dataclass(frozen=True)
class Empresa:
    cnpj: str
    loja: str
    razao_social: str
    pasta: str
    aliases_pasta: tuple[str, ...] = ()

    @property
    def rotulo(self) -> str:
        return f"{self.pasta} ({self.cnpj})"


@dataclass(frozen=True)
class Config:
    url_login: str
    usuario: str
    senha: str
    headless: bool
    timeout_ms: int
    timeout_download_ms: int
    dias_retroativos: int
    max_retentativas: int
    dias_aguardar_xml: int
    baixar_canceladas: bool
    raiz_notas: Path
    sufixo_rpa: str
    dir_dados: Path
    dir_logs: Path
    dias_manter_logs: int
    apelidos_fonte: Path
    apelidos_copia: Path
    apelidos_usar_sugeridos: bool
    apelidos_extras: Path
    empresas: tuple[Empresa, ...]


def _caminho(valor: str) -> Path:
    p = Path(os.path.expandvars(valor))
    return p if p.is_absolute() else RAIZ_PROJETO / p


def _exigir(secao: dict, chave: str, nome_secao: str):
    if chave not in secao:
        raise ErroConfiguracao(f"config.toml: falta '{chave}' em [{nome_secao}]")
    return secao[chave]


def carregar(caminho: Path | None = None) -> Config:
    caminho = caminho or CONFIG_PADRAO
    if not caminho.is_file():
        raise ErroConfiguracao(
            f"arquivo de configuração não encontrado: {caminho}. "
            "Copie config/config.example.toml para config/config.toml e ajuste."
        )
    try:
        dados = tomllib.loads(caminho.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ErroConfiguracao(f"config.toml inválido: {e}") from e

    load_dotenv(RAIZ_PROJETO / ".env", override=False)
    usuario = os.environ.get("ESPIAO_USUARIO", "").strip()
    senha = os.environ.get("ESPIAO_SENHA", "")
    if not usuario or not senha:
        raise ErroConfiguracao("credenciais ausentes: defina ESPIAO_USUARIO e ESPIAO_SENHA no arquivo .env")

    esp = dados.get("espiao", {})
    exe = dados.get("execucao", {})
    pas = dados.get("pastas", {})
    ape = dados.get("apelidos", {})

    empresas = []
    for i, e in enumerate(dados.get("empresas", []), start=1):
        cnpj = str(_exigir(e, "cnpj", f"empresas #{i}"))
        if not cnpj_valido(cnpj):
            raise ErroConfiguracao(f"config.toml: CNPJ inválido na empresa #{i}: {cnpj}")
        empresas.append(
            Empresa(
                cnpj=cnpj,
                loja=str(e.get("loja", "")),
                razao_social=str(e.get("razao_social", "")),
                pasta=str(_exigir(e, "pasta", f"empresas #{i}")).strip(),
                aliases_pasta=tuple(str(a).strip() for a in e.get("aliases_pasta", [])),
            )
        )
    if not empresas:
        raise ErroConfiguracao("config.toml: nenhuma empresa em [[empresas]]")
    if len({e.cnpj for e in empresas}) != len(empresas):
        raise ErroConfiguracao("config.toml: CNPJ repetido em [[empresas]]")

    dias_retroativos = int(exe.get("dias_retroativos", 1))
    if not 0 <= dias_retroativos <= 29:
        raise ErroConfiguracao("config.toml: dias_retroativos deve ficar entre 0 e 29")
    dias_aguardar_xml = int(exe.get("dias_aguardar_xml", 6))
    if not 0 <= dias_aguardar_xml <= 29:
        raise ErroConfiguracao("config.toml: dias_aguardar_xml deve ficar entre 0 e 29")

    raiz_notas = _caminho(str(_exigir(pas, "raiz_notas", "pastas")))
    if not raiz_notas.is_dir():
        raise ErroConfiguracao(f"pasta raiz das notas não encontrada: {raiz_notas}")

    return Config(
        url_login=str(esp.get("url_login", "https://app.espiaonfe.com.br/auth/login")),
        usuario=usuario,
        senha=senha,
        headless=bool(esp.get("headless", True)),
        timeout_ms=int(esp.get("timeout_ms", 30000)),
        timeout_download_ms=int(esp.get("timeout_download_ms", 90000)),
        dias_retroativos=dias_retroativos,
        max_retentativas=int(exe.get("max_retentativas", 5)),
        dias_aguardar_xml=dias_aguardar_xml,
        baixar_canceladas=bool(exe.get("baixar_canceladas", False)),
        raiz_notas=raiz_notas,
        sufixo_rpa=str(pas.get("sufixo_rpa", "_RPA")),
        dir_dados=_caminho(str(pas.get("dados", r"%LOCALAPPDATA%\RPA_NF_ESP\dados"))),
        dir_logs=_caminho(str(pas.get("logs", "logs"))),
        dias_manter_logs=int(pas.get("dias_manter_logs", 90)),
        apelidos_fonte=_caminho(str(_exigir(ape, "fonte", "apelidos"))),
        apelidos_copia=_caminho(str(ape.get("copia_local", "config/11-apelidos-fornecedores.md"))),
        apelidos_usar_sugeridos=bool(ape.get("usar_sugeridos", True)),
        apelidos_extras=_caminho(str(ape.get("extras", "config/apelidos_extras.csv"))),
        empresas=tuple(empresas),
    )
