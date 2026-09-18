#!/usr/bin/env python3
"""Convert KiCad 6--10 footprints and boards to OpenPnP library XML.

This is a deliberately small S-expression reader.  It avoids KiCad's old
``pykicad`` module and the changing pcbnew Python API, so it can be run with
normal CPython 3.10+ as well as KiCad's bundled Python.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import os
import re
import sys
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
    body_width: float | None = None
    body_height: float | None = None
    standard: bool = False


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


def point(node: SExpr | None, context: str) -> tuple[float, float] | None:
    if node is None or len(node) < 3:
        return None
    return number(atom(node, 1), f"{context} x"), number(atom(node, 2), f"{context} y")


def fab_body_dimensions(node: SExpr) -> tuple[float, float] | None:
    """Return the F.Fab outline's X/Y bounding-box dimensions in millimeters."""
    points: list[tuple[float, float]] = []
    for graphic in node:
        if not isinstance(graphic, list) or not graphic or not isinstance(graphic[0], str):
            continue
        kind = graphic[0]
        if kind not in {"fp_rect", "fp_line", "fp_poly", "fp_circle", "fp_arc"} or atom(child(graphic, "layer"), 1) != "F.Fab":
            continue
        if kind == "fp_circle":
            center = point(child(graphic, "center"), "F.Fab circle center")
            edge = point(child(graphic, "end"), "F.Fab circle end")
            if center and edge:
                radius = ((edge[0] - center[0]) ** 2 + (edge[1] - center[1]) ** 2) ** 0.5
                points.extend(((center[0] - radius, center[1] - radius), (center[0] + radius, center[1] + radius)))
            continue
        names = ("start", "end") if kind in {"fp_rect", "fp_line"} else ("start", "mid", "end")
        for name in names:
            if coordinate := point(child(graphic, name), f"F.Fab {kind}"):
                points.append(coordinate)
        if kind == "fp_poly":
            for polygon_point in children(child(graphic, "pts") or [], "xy"):
                if coordinate := point(polygon_point, "F.Fab polygon"):
                    points.append(coordinate)
    if len(points) < 2:
        return None
    xs, ys = zip(*points)
    return max(xs) - min(xs), max(ys) - min(ys)


def footprint_from_sexpr(node: SExpr) -> Footprint:
    if len(node) < 2 or not isinstance(node[1], str):
        raise ValueError("footprint has no name")
    pads = tuple(pad for item in children(node, "pad") if (pad := pad_from_sexpr(item)) is not None)
    properties = {atom(item, 1): atom(item, 2) for item in children(node, "property")}
    # Older boards use fp_text reference/value instead of properties.
    for text in children(node, "fp_text"):
        if atom(text, 1) in {"reference", "value"}:
            properties.setdefault(atom(text, 1).title(), atom(text, 2))
    dimensions = fab_body_dimensions(node)
    return Footprint(node[1], pads, properties.get("Reference", ""), properties.get("Value", ""),
                     *(dimensions or (None, None)))


def package_id(footprint: Footprint) -> str:
    """Use a compact standard package code, or the full custom KiCad name."""
    if not footprint.standard:
        return footprint.name
    name = footprint.name.rsplit(":", 1)[-1]
    if match := re.search(r"(?:^|_)(\d{4})_\d{4}Metric(?:_|$)", name):
        return match.group(1)
    if match := re.search(r"\b(SOT-\d+)\b", name):
        return match.group(1)
    if match := re.match(r"(DIP-\d+)_", name):
        return match.group(1)
    return name


def canonical_footprint_name(name: str) -> str:
    """Remove KiCad's assembly-oriented pad variants from a footprint name."""
    library, separator, footprint = name.rpartition(":")
    if not separator:
        return name
    footprint = re.sub(r"_Pad[^_]*_HandSolder$", "", footprint)
    footprint = re.sub(r"_HandSolder$", "", footprint)
    footprint = re.sub(r"_LongPads$", "", footprint)
    return f"{library}:{footprint}"


def standard_footprint_roots() -> tuple[Path, ...]:
    """Return the usual KiCad footprint-library locations, in priority order."""
    roots = [Path(value) for variable in ("KICAD10_FOOTPRINT_DIR", "KICAD_FOOTPRINT_DIR")
             if (value := os.environ.get(variable))]
    roots.extend((Path("/usr/share/kicad/footprints"), Path("/usr/local/share/kicad/footprints")))
    return tuple(roots)


def standard_footprint_path(name: str, roots: Sequence[Path] | None = None) -> Path | None:
    """Locate a fully-qualified KiCad footprint in its standard library."""
    library, separator, footprint = name.partition(":")
    if not separator:
        return None
    for root in roots or standard_footprint_roots():
        path = root / f"{library}.pretty" / f"{footprint}.kicad_mod"
        if path.is_file():
            return path
    return None


def canonical_footprint(footprint: Footprint, roots: Sequence[Path] | None = None) -> Footprint:
    """Use the standard, non-assembly KiCad footprint when one is available."""
    name = canonical_footprint_name(footprint.name)
    path = standard_footprint_path(name, roots)
    if path is None:
        return Footprint(name, footprint.pads, footprint.reference, footprint.value,
                         footprint.body_width, footprint.body_height, False)
    document = parse(path.read_text(encoding="utf-8"))
    standard = footprint_from_sexpr(document)
    return Footprint(name, standard.pads, footprint.reference, footprint.value,
                     standard.body_width, standard.body_height, True)


def package_element(footprint: Footprint) -> ET.Element:
    package = ET.Element("package", version="1.1", id=package_id(footprint))
    attributes = {"units": "Millimeters"}
    if footprint.body_width is not None and footprint.body_height is not None:
        attributes["body-width"] = fmt(footprint.body_width)
        attributes["body-height"] = fmt(footprint.body_height)
    xml_footprint = ET.SubElement(package, "footprint", attributes)
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
    value = footprint.value or footprint.reference or package
    reference_prefix = re.match(r"[A-Za-z]+", footprint.reference)
    identifier = reference_prefix.group(0) if reference_prefix else ""
    part_id = ":".join(item for item in (identifier, package, value) if item)
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


def read_join_files(paths: Sequence[Path]) -> dict[str, ET.Element]:
    """Read existing OpenPnP libraries keyed by their root element name."""
    roots: dict[str, ET.Element] = {}
    valid_roots = {"openpnp-packages", "openpnp-parts"}
    for path in paths:
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as error:
            raise ValueError(f"{path}: invalid XML: {error}") from error
        if root.tag not in valid_roots:
            raise ValueError(f"{path}: expected an OpenPnP packages.xml or parts.xml file")
        if root.tag in roots:
            raise ValueError(f"more than one --join file contains <{root.tag}>")
        entity_tag = "package" if root.tag == "openpnp-packages" else "part"
        ids: set[str] = set()
        for element in root.findall(entity_tag):
            entity_id = element.get("id")
            if entity_id and entity_id in ids:
                raise ValueError(f"{path}: duplicate {entity_tag} ID {entity_id!r}")
            if entity_id:
                ids.add(entity_id)
        roots[root.tag] = root
    return roots


def joined_root(tag: str, entity_tag: str, generated: Sequence[ET.Element], existing: ET.Element | None) -> ET.Element:
    """Keep existing entities unchanged and add generated entities by unused ID."""
    result = ET.Element(tag, existing.attrib if existing is not None else {})
    existing_ids: set[str] = set()
    if existing is not None:
        for element in existing:
            result.append(deepcopy(element))
            if element.tag == entity_tag and element.get("id"):
                existing_ids.add(element.get("id", ""))
    for element in generated:
        if element.get("id") not in existing_ids:
            result.append(element)
    return result


def merge_empty_package_footprint(existing: ET.Element, generated: ET.Element) -> str | None:
    """Add generated footprint data only when the existing footprint is empty.

    Returns a conflict description when the existing package must not be
    modified. Existing package attributes and OpenPnP configuration remain
    untouched on a successful merge.
    """
    generated_footprint = generated.find("footprint")
    if generated_footprint is None or not list(generated_footprint):
        return "generated package has no pads"
    existing_footprint = existing.find("footprint")
    if existing_footprint is None:
        existing.append(deepcopy(generated_footprint))
        return None
    if list(existing_footprint):
        return "existing footprint is not empty"
    for pad in generated_footprint:
        existing_footprint.append(deepcopy(pad))
    return None


def joined_packages(generated: Sequence[ET.Element], existing: ET.Element | None,
                    update_mode: str | None) -> tuple[ET.Element, list[str]]:
    """Join packages and report IDs which could not safely be updated."""
    if existing is None:
        return joined_root("openpnp-packages", "package", generated, None), []
    generated_by_id = {package.get("id", ""): package for package in generated}
    result = ET.Element("openpnp-packages", existing.attrib)
    conflicts: list[str] = []
    seen_ids: set[str] = set()
    for element in existing:
        copied = deepcopy(element)
        package_id_value = element.get("id", "") if element.tag == "package" else ""
        replacement = generated_by_id.get(package_id_value)
        if replacement is not None:
            seen_ids.add(package_id_value)
            if update_mode == "replace":
                copied = deepcopy(replacement)
            elif update_mode == "safe":
                reason = merge_empty_package_footprint(copied, replacement)
                if reason:
                    conflicts.append(f"package {package_id_value!r}: {reason}")
            else:
                conflicts.append(f"package {package_id_value!r}: existing definition preserved")
        result.append(copied)
    for package in generated:
        if package.get("id", "") not in seen_ids:
            result.append(package)
    return result, conflicts


def report_conflicts(conflicts: Sequence[str], output: Path | None) -> None:
    if not conflicts:
        return
    message = "\n".join(conflicts) + "\n"
    if output is not None:
        output.write_text(message, encoding="utf-8")
    print(f"{len(conflicts)} join conflict(s):\n{message}".rstrip(), file=sys.stderr)


def read_input(path: Path) -> list[Footprint]:
    """Read a board, one footprint, or every footprint in a .pretty library."""
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
    parser.add_argument("--footprint", "--single-footprint", "-f", type=Path, metavar="PATH",
                        help="convert one .kicad_mod footprint to an OpenPnP packages XML file")
    parser.add_argument("--library", "--footprint-library", "-l", type=Path, metavar="PATH",
                        help="convert every .kicad_mod in a KiCad .pretty library to OpenPnP packages")
    parser.add_argument("--packages", "--output", "-o", type=Path, help="packages output (default: packages.new.xml)")
    parser.add_argument("--parts", type=Path, help="parts output (default for boards: parts.new.xml)")
    parser.add_argument("--no-parts", action="store_true", help="do not create parts.new.xml for board input")
    parser.add_argument("--join", type=Path, action="append", default=[], metavar="XML",
                        help="preserve definitions from an existing packages.xml or parts.xml (repeat for both)")
    update_group = parser.add_mutually_exclusive_group()
    update_group.add_argument("--update-empty-packages", action="store_true",
                              help="with --join, add pads only to packages whose footprint is completely empty")
    update_group.add_argument("--replace-packages", action="store_true",
                              help="with --join, replace colliding package definitions entirely (unsafe)")
    parser.add_argument("--conflicts", type=Path, metavar="FILE",
                        help="write unresolved --join package conflicts to FILE")
    args = parser.parse_args(argv)
    input_sources = [source for source in (args.input, args.input_option, args.footprint, args.library) if source is not None]
    if len(input_sources) > 1:
        parser.error("use exactly one input: positional input, --input, --footprint, or --library")
    input_path = input_sources[0] if input_sources else None
    if input_path is None:
        parser.error("an input path is required")
    packages = args.packages or Path("packages.new.xml")
    parts = args.parts
    if input_path.suffix.lower() == ".kicad_pcb" and not args.no_parts and parts is None:
        parts = Path("parts.new.xml")
    if args.no_parts and args.parts:
        parser.error("--no-parts cannot be used with --parts")
    try:
        if (args.update_empty_packages or args.replace_packages) and not args.join:
            raise ValueError("--update-empty-packages and --replace-packages require --join")
        if args.conflicts is not None and not args.join:
            raise ValueError("--conflicts requires --join")
        if args.footprint is not None:
            if input_path.suffix.lower() != ".kicad_mod" or input_path.is_dir():
                raise ValueError("--footprint requires a .kicad_mod file")
        if args.library is not None:
            if not input_path.is_dir() or input_path.suffix.lower() != ".pretty":
                raise ValueError("--library requires a KiCad .pretty directory")
        joins = read_join_files(args.join)
        for join in args.join:
            if join.resolve() in {packages.resolve(), *( [parts.resolve()] if parts else [])}:
                raise ValueError(f"--join input {join} must not be the same as an output file")
        if args.conflicts and args.conflicts.resolve() in {packages.resolve(), *( [parts.resolve()] if parts else [])}:
            raise ValueError("--conflicts file must not be the same as an XML output file")
        if "openpnp-parts" in joins and parts is None:
            raise ValueError("a joined parts.xml file requires board input with parts output enabled")
        footprints = [canonical_footprint(footprint) for footprint in read_input(input_path)]
        usable = [footprint for footprint in footprints if footprint.pads]
        package_by_id: dict[str, Footprint] = {}
        for footprint in usable:
            identifier = package_id(footprint)
            existing = package_by_id.setdefault(identifier, footprint)
            if existing != footprint and (existing.pads != footprint.pads or
                                          existing.body_width != footprint.body_width or
                                          existing.body_height != footprint.body_height):
                raise ValueError(
                    f"KiCad footprints {existing.name!r} and {footprint.name!r} both map to package "
                    f"{identifier!r} but have different geometry; use distinct package names")
        update_mode = "safe" if args.update_empty_packages else "replace" if args.replace_packages else None
        package_root, conflicts = joined_packages(
            [package_element(footprint) for footprint in package_by_id.values()],
            joins.get("openpnp-packages"), update_mode)
        write_xml(package_root, packages)
        if parts:
            if input_path.suffix.lower() != ".kicad_pcb":
                raise ValueError("--parts requires a .kicad_pcb input")
            parts_by_id: dict[str, ET.Element] = {}
            for footprint in usable:
                part = part_element(footprint, package_id(footprint))
                existing = parts_by_id.setdefault(part.get("id", ""), part)
                if existing.get("package-id") != part.get("package-id"):
                    raise ValueError(
                        f"KiCad value {part.get('id')!r} is used with different footprints "
                        f"({existing.get('package-id')} and {part.get('package-id')}); "
                        "give the components distinct part-number values"
                    )
            part_root = joined_root("openpnp-parts", "part", list(parts_by_id.values()), joins.get("openpnp-parts"))
            write_xml(part_root, parts)
        report_conflicts(conflicts, args.conflicts)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
