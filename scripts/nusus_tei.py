"""Nusus TEI to the canonical `parts` array."""

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List

NS = "{http://www.tei-c.org/ns/1.0}"


class TeiError(ValueError):
    pass


def _ws(text: str) -> str:
    return " ".join(text.split())


def tei_pages(path: Path) -> List[Dict[str, str]]:
    """The pages of one nusus TEI file: page_id, page_number, body."""
    body = ET.parse(path).getroot().find(f".//{NS}body")
    if body is None:
        raise TeiError(f"{path.name}: no <body>")
    pages: List[Dict[str, Any]] = []

    def loose(text) -> bool:
        return bool(text and text.strip())

    def walk(parent: ET.Element, div_n: str) -> None:
        if loose(parent.text):
            raise TeiError(f"{path.name}: text directly inside <{parent.tag[len(NS):]}>")
        for el in parent:
            tag = el.tag[len(NS):]
            if loose(el.tail):
                raise TeiError(f"{path.name}: text after <{tag}>")
            if tag == "pb":
                pages.append({"page_id": str(len(pages) + 1),
                              "page_number": el.get("n", ""), "lines": []})
            elif tag == "div":
                if div_n is not None:
                    raise TeiError(f"{path.name}: nested <div>")
                walk(el, el.get("n"))
            elif tag in ("p", "head"):
                text = _ws("".join(el.itertext()))
                if not text:
                    continue
                if not pages:
                    raise TeiError(f"{path.name}: <{tag}> before the first <pb>")
                if tag == "head":
                    if div_n is None:
                        raise TeiError(f"{path.name}: <head> outside a chapter <div>")
                    text = f"<title id={div_n} parent=0>{text}</title>"
                pages[-1]["lines"].append(text)
            else:
                raise TeiError(f"{path.name}: unexpected <{tag}>")

    walk(body, None)
    return [{"page_id": p["page_id"], "page_number": p["page_number"],
             "body": "\n".join(p["lines"])} for p in pages]


def tei_parts(path: Path) -> List[Dict[str, Any]]:
    return [{"part": "1", "pages": tei_pages(path)}]
