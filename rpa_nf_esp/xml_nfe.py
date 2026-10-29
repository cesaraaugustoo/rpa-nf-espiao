"""Conferência do XML da NF-e baixado do Espião.

O arquivo entregue é o nfeProc (NF-e + protocolo de autorização da SEFAZ). A nota é
identificada pelo atributo Id do infNFe, "NFe" + chave de acesso: se não bater com a chave
da linha clicada, o arquivo é recusado.
"""

from pathlib import Path
from xml.etree import ElementTree


class XmlInvalido(Exception):
    """O arquivo não é o XML da nota esperada."""


def validar_xml_nfe(caminho: Path, chave: str) -> None:
    try:
        raiz = ElementTree.parse(caminho).getroot()
    except (ElementTree.ParseError, OSError) as e:
        raise XmlInvalido(f"XML ilegível: {e}") from e
    ids = {el.get("Id") for el in raiz.iter() if el.tag.rpartition("}")[2] == "infNFe"}
    if f"NFe{chave}" not in ids:
        raise XmlInvalido(f"o XML não é da nota {chave} (encontrado: {', '.join(sorted(filter(None, ids))) or 'nenhuma'})")
