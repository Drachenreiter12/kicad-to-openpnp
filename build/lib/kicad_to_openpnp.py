#!/usr/bin/env python3
"""Convert KiCad 6--10 footprints and boards to OpenPnP library XML.

This is a deliberately small S-expression reader.  It avoids KiCad's old
``pykicad`` module and the changing pcbnew Python API, so it can be run with
normal CPython 3.10+ as well as KiCad's bundled Python.
"""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence


Atom = str
SExpr = list["SExpr | Atom"]


def tokenize(source: str) -> Iterator[str]:
    """Tokenize the KiCad S-expression subset, including escaped strings."""
    index = 0
    while index < len(source):
        char = source[index]
        if char.isspace():
            index += 1
        elif char == ";":  # KiCad comments run to the end of the line.
            index = source.find("\n", index)
            if index == -1:
                return
        elif char in "()":
            yield char
            index += 1
        elif char == '"':
            index += 1
            value: list[str] = []
            while index < len(source) and source[index] != '"':
                if source[index] == "\\" and index + 1 < len(source):
                    index += 1
                value.append(source[index])
                index += 1
            if index == len(source):
                raise ValueError("unterminated quoted string")
            yield "".join(value)
            index += 1
        else:
            end = index
            while end < len(source) and not source[end].isspace() and source[end] not in "();":
                end += 1
            yield source[index:end]
            index = end


def parse(source: str) -> SExpr:
    root: SExpr = []
    stack: list[SExpr] = [root]
    for token in tokenize(source):
        if token == "(":
            child: SExpr = []
            stack[-1].append(child)
            stack.append(child)
        elif token == ")":
            if len(stack) == 1:
                raise ValueError("unexpected closing parenthesis")
            stack.pop()
        else:
            stack[-1].append(token)
    if len(stack) != 1 or len(root) != 1 or not isinstance(root[0], list):
        raise ValueError("expected one complete KiCad S-expression")
    return root[0]


def children(node: SExpr, name: str) -> list[SExpr]:
    return [item for item in node if isinstance(item, list) and item and item[0] == name]


def child(node: SExpr, name: str) -> SExpr | None:
    values = children(node, name)
    return values[0] if values else None


def atom(node: SExpr | None, position: int, default: str = "") -> str:
    if node is None or len(node) <= position or isinstance(node[position], list):
        return default
    return node[position]  # type: ignore[return-value]


def number(value: str, context: str) -> float:
    try:
        return float(value)
    except ValueError as error:
        raise ValueError(f"invalid number for {context}: {value!r}") from error


def fmt(value: float) -> str:
    return "0" if abs(value) < 1e-12 else format(value, ".10g")


@dataclass(frozen=True)
class Pad:
    name: str
    x: float
    y: float
    width: float
    height: float
    rotation: float
    roundness: float


@dataclass(frozen=True)
class Footprint:
    name: str
    pads: tuple[Pad, ...]
    reference: str = ""
    value: str = ""


def pad_from_sexpr(node: SExpr) -> Pad | None:
    # (pad NAME TYPE SHAPE (at X Y [ROT]) (size W H) ...)
    if len(node) < 4 or not all(isinstance(item, str) for item in node[:4]):
        return None
    name, kind, shape = node[1:4]  # type: ignore[misc]
    if kind == "np_thru_hole":
        return None
    at, size = child(node, "at"), child(node, "size")
    if at is None or size is None or len(at) < 3 or len(size) < 3:
        return None
    x, y = number(atom(at, 1), "pad x"), number(atom(at, 2), "pad y")
    width, height = number(atom(size, 1), "pad width"), number(atom(size, 2), "pad height")
    rotation = number(atom(at, 3, "0"), "pad rotation")
    if shape == "roundrect":
        # KiCad ratio is corner radius / min(width, height); OpenPnP uses
        # diameter / min(width, height), expressed as a percentage.
        roundness = max(0.0, min(100.0, 200.0 * number(atom(child(node, "roundrect_rratio"), 1, "0"), "roundrect ratio")))
    elif shape in {"circle", "oval"}:
        roundness = 100.0
    else:  # rect, trapezoid and custom pads are represented safely as rects.
        roundness = 0.0
    return Pad(name, x, y, width, height, rotation, roundness)


def footprint_from_sexpr(node: SExpr) -> Footprint:
    if len(node) < 2 or not isinstance(node[1], str):
        raise ValueError("footprint has no name")
    pads = tuple(pad for item in children(node, "pad") if (pad := pad_from_sexpr(item)) is not None)
    properties = {atom(item, 1): atom(item, 2) for item in children(node, "property")}
    # Older boards use fp_text reference/value instead of properties.
    for text in children(node, "fp_text"):
        if atom(text, 1) in {"reference", "value"}:
            properties.setdefault(atom(text, 1).title(), atom(text, 2))
    return Footprint(node[1], pads, properties.get("Reference", ""), properties.get("Value", ""))


def package_id(name: str) -> str:
    """Use the library footprint name, without the KiCad library prefix."""
    return name.rsplit(":", 1)[-1]


def package_element(footprint: Footprint) -> ET.Element:
    package = ET.Element("package", version="1.1", id=package_id(footprint.name))
    xml_footprint = ET.SubElement(package, "footprint", units="Millimeters")
    for pad in footprint.pads:
        # KiCad coordinates have positive Y down; OpenPnP package coordinates
        # have positive Y up. Rotation is retained: OpenPnP stores the same
        # clockwise visual orientation used in its package editor.
        ET.SubElement(xml_footprint, "pad", name=pad.name, x=fmt(pad.x), y=fmt(-pad.y),
                      width=fmt(pad.width), height=fmt(pad.height), rotation=fmt(pad.rotation),
                      mark="true" if pad.name == "1" else "false", roundness=fmt(pad.roundness))
    ET.SubElement(package, "compatible-nozzle-tip-ids", {"class": "java.util.ArrayList"})
    return package


def part_element(footprint: Footprint, package: str) -> ET.Element:
    part_id = footprint.value or footprint.reference or package
    return ET.Element("part", id=part_id, **{
        "height-units": "Millimeters", "height": "0.0",
        "through-board-depth-units": "Millimeters", "through-board-depth": "0.0",
        "package-id": package, "speed": "1.0", "pick-retry-count": "0",
    })


def indent(element: ET.Element, level: int = 0) -> None:
    whitespace = "\n" + "   " * level
    if len(element):
        if not element.text or not element.text.strip():
            element.text = whitespace + "   "
        for item in element:
            indent(item, level + 1)
        if not element[-1].tail or not element[-1].tail.strip():
            element[-1].tail = whitespace
    if level and (not element.tail or not element.tail.strip()):
        element.tail = whitespace


def write_xml(root: ET.Element, output: Path) -> None:
    indent(root)
    output.write_text(ET.tostring(root, encoding="unicode") + "\n", encoding="utf-8")


def read_input(path: Path) -> list[Footprint]:
    files = sorted(path.glob("*.kicad_mod")) if path.is_dir() else [path]
    if not files:
        raise ValueError(f"no .kicad_mod files found in {path}")
    result: list[Footprint] = []
    for file in files:
        document = parse(file.read_text(encoding="utf-8"))
        if not document or document[0] not in {"footprint", "module", "kicad_pcb"}:
            raise ValueError(f"{file}: not a KiCad footprint or board")
        nodes = children(document, "footprint") if document[0] == "kicad_pcb" else [document]
        result.extend(footprint_from_sexpr(node) for node in nodes)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=Path, help=".kicad_mod, .kicad_pcb, or directory of .kicad_mod files")
    parser.add_argument("--input", "-i", dest="input_option", type=Path, help="input path (legacy spelling)")
    parser.add_argument("--packages", "--output", "-o", type=Path, help="packages output (default: packages.new.xml)")
    parser.add_argument("--parts", type=Path, help="parts output (default for boards: parts.new.xml)")
    parser.add_argument("--no-parts", action="store_true", help="do not create parts.new.xml for board input")
    args = parser.parse_args(argv)
    if args.input and args.input_option:
        parser.error("use either the positional input or --input, not both")
    input_path = args.input or args.input_option
    if input_path is None:
        parser.error("an input path is required")
    packages = args.packages or Path("packages.new.xml")
    parts = args.parts
    if input_path.suffix.lower() == ".kicad_pcb" and not args.no_parts and parts is None:
        parts = Path("parts.new.xml")
    if args.no_parts and args.parts:
        parser.error("--no-parts cannot be used with --parts")
    try:
        footprints = read_input(input_path)
        usable = [footprint for footprint in footprints if footprint.pads]
        package_root = ET.Element("openpnp-packages")
        package_by_id: dict[str, Footprint] = {}
        for footprint in usable:
            package_by_id.setdefault(package_id(footprint.name), footprint)
        for footprint in package_by_id.values():
            package_root.append(package_element(footprint))
        write_xml(package_root, packages)
        if parts:
            if input_path.suffix.lower() != ".kicad_pcb":
                raise ValueError("--parts requires a .kicad_pcb input")
            parts_by_id: dict[str, ET.Element] = {}
            for footprint in usable:
                part = part_element(footprint, package_id(footprint.name))
                existing = parts_by_id.setdefault(part.get("id", ""), part)
                if existing.get("package-id") != part.get("package-id"):
                    raise ValueError(
                        f"KiCad value {part.get('id')!r} is used with different footprints "
                        f"({existing.get('package-id')} and {part.get('package-id')}); "
                        "give the components distinct part-number values"
                    )
            part_root = ET.Element("openpnp-parts")
            part_root.extend(parts_by_id.values())
            write_xml(part_root, parts)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
