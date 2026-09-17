import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import kicad_to_openpnp as converter


SCRIPT = Path(__file__).parents[1] / "kicad_to_openpnp.py"


def run(tmp_path, text, extra=()):
    source = tmp_path / "demo.kicad_mod"
    output = tmp_path / "packages.xml"
    source.write_text(text)
    completed = subprocess.run([sys.executable, str(SCRIPT), "-i", str(source), "-o", str(output), *extra], capture_output=True, text=True)
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return ET.parse(output).getroot()


class ConverterTests(unittest.TestCase):
    def test_current_kicad_pads_are_openpnp_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = run(Path(directory), '''(footprint "Demo" (version 20240108) (generator pcbnew)
      (pad "1" smd roundrect (at -1 2 90) (size 0.8 1.2) (layers "F.Cu") (roundrect_rratio 0.25))
      (pad "2" smd circle (at 1 -2) (size 0.6 0.6) (layers "F.Cu"))
      (pad "3" smd oval (at 0 0) (size 2 1) (layers "F.Cu")))''')
            pads = root.findall("./package/footprint/pad")
            self.assertEqual([pad.attrib["name"] for pad in pads], ["1", "2", "3"])
            self.assertEqual(pads[0].attrib, {"name": "1", "x": "-1", "y": "-2", "width": "0.8", "height": "1.2", "rotation": "90", "mark": "true", "roundness": "50"})
            self.assertEqual(pads[1].attrib["roundness"], "100")
            self.assertEqual(pads[2].attrib["height"], "1")  # old tool lost this for circles


    def test_directory_input_ignores_non_footprints(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            (tmp_path / "a.kicad_mod").write_text('(footprint "A" (pad "1" smd rect (at 0 0) (size 1 1)))')
            (tmp_path / "notes.txt").write_text("not a footprint")
            output = tmp_path / "packages.xml"
            completed = subprocess.run([sys.executable, str(SCRIPT), "-i", str(tmp_path), "-o", str(output)], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(ET.parse(output).find("./package").attrib["id"], "A")

    def test_explicit_footprint_and_library_commands_create_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            footprint = tmp_path / "Demo.kicad_mod"
            library = tmp_path / "Demo.pretty"
            library.mkdir()
            footprint.write_text('(footprint "Single" (pad "1" smd rect (at 0 0) (size 1 1)))')
            (library / "Library.kicad_mod").write_text(
                '(footprint "Library" (pad "A" smd oval (at 1 2) (size 2 1)))')

            single_output = tmp_path / "single.xml"
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--footprint", str(footprint), "--packages", str(single_output)],
                capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(ET.parse(single_output).find("package").attrib["id"], "Single")

            library_output = tmp_path / "library.xml"
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--library", str(library), "--packages", str(library_output)],
                capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            pad = ET.parse(library_output).find("./package/footprint/pad")
            self.assertEqual(pad.attrib, {"name": "A", "x": "1", "y": "-2", "width": "2", "height": "1", "rotation": "0", "mark": "false", "roundness": "100"})

    def test_explicit_footprint_and_library_commands_validate_input_type(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            source = tmp_path / "not-a-footprint.txt"
            output = tmp_path / "packages.xml"
            source.write_text("ignored")
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--footprint", str(source), "--packages", str(output)],
                capture_output=True, text=True)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("--footprint requires a .kicad_mod file", completed.stderr)

    def test_board_creates_deduplicated_parts(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            board = tmp_path / "demo.kicad_pcb"
            packages, parts = tmp_path / "packages.xml", tmp_path / "parts.xml"
            board.write_text('''(kicad_pcb (version 20240108) (generator pcbnew)
              (footprint "Resistor_SMD:R_0603" (property "Reference" "R1") (property "Value" "10k") (pad "1" smd rect (at 0 0) (size 1 1)))
              (footprint "Resistor_SMD:R_0603" (property "Reference" "R2") (property "Value" "10k") (pad "1" smd rect (at 0 0) (size 1 1))))''')
            completed = subprocess.run([sys.executable, str(SCRIPT), "-i", str(board), "-o", str(packages), "--parts", str(parts)], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            part = ET.parse(parts).findall("part")[0]
            self.assertEqual(part.attrib["package-id"], "Resistor_SMD:R_0603")
            self.assertEqual(part.attrib["id"], "R_0603:10k")

    def test_standard_footprint_replaces_hand_solder_variant_and_normalizes_part_name(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            library = tmp_path / "Capacitor_SMD.pretty"
            library.mkdir()
            (library / "C_0603_1608Metric.kicad_mod").write_text(
                '''(footprint "C_0603_1608Metric"
                  (fp_rect (start -0.8 -0.4) (end 0.8 0.4) (layer "F.Fab"))
                  (pad "1" smd rect (at -1 0) (size 1 1)))''')
            source = converter.Footprint(
                "Capacitor_SMD:C_0603_1608Metric_Pad1.08x0.95mm_HandSolder", (), "C1", "100n")
            canonical = converter.canonical_footprint(source, [tmp_path])
            self.assertEqual(canonical.name, "Capacitor_SMD:C_0603_1608Metric")
            self.assertEqual(canonical.pads[0].x, -1)
            package = converter.package_element(canonical)
            part = converter.part_element(canonical, package.attrib["id"])
            self.assertEqual(package.attrib["id"], "Capacitor_SMD:C_0603_1608Metric")
            self.assertEqual(part.attrib["id"], "0603:100n")
            self.assertEqual(package.find("footprint").attrib["body-width"], "1.6")
            self.assertEqual(package.find("footprint").attrib["body-height"], "0.8")

    def test_canonical_names_remove_long_pad_variant(self):
        self.assertEqual(
            converter.canonical_footprint_name("Package_DIP:DIP-16_W7.62mm_LongPads"),
            "Package_DIP:DIP-16_W7.62mm")

    def test_fab_lines_define_body_dimensions(self):
        footprint = converter.footprint_from_sexpr(converter.parse('''(footprint "LED"
          (fp_line (start -1.6 -0.8) (end -1.6 0.8) (layer "F.Fab"))
          (fp_line (start -1.6 0.8) (end 1.6 0.8) (layer "F.Fab"))
          (fp_line (start 1.6 0.8) (end 1.6 -0.8) (layer "F.Fab"))
          (fp_line (start 1.6 -0.8) (end -1.6 -0.8) (layer "F.Fab"))
          (pad "1" smd rect (at 0 0) (size 1 1)))'''))
        self.assertEqual((footprint.body_width, footprint.body_height), (3.2, 1.6))

    def test_board_defaults_match_pipx_command(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            board = tmp_path / "demo.kicad_pcb"
            board.write_text('''(kicad_pcb (version 20240108) (generator pcbnew)
              (footprint "Resistor_SMD:R_0603" (property "Reference" "R1") (property "Value" "10k") (pad "1" smd rect (at 0 0) (size 1 1))))''')
            completed = subprocess.run([sys.executable, str(SCRIPT), str(board)], cwd=tmp_path, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue((tmp_path / "packages.new.xml").is_file())
            self.assertTrue((tmp_path / "parts.new.xml").is_file())

    def test_join_preserves_existing_definitions_and_adds_new_ones(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            source = tmp_path / "demo.kicad_mod"
            existing = tmp_path / "packages.xml"
            output = tmp_path / "packages.new.xml"
            source.write_text('(footprint "New" (pad "1" smd rect (at 0 0) (size 1 1)))')
            existing.write_text('''<openpnp-packages>
              <package version="1.1" id="Existing" description="keep me"><footprint units="Millimeters"/></package>
              <package version="1.1" id="New" description="do not replace"><footprint units="Millimeters"/></package>
            </openpnp-packages>''')
            completed = subprocess.run([sys.executable, str(SCRIPT), str(source), "--packages", str(output), "--join", str(existing)], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            packages = {element.attrib["id"]: element for element in ET.parse(output).findall("package")}
            self.assertEqual(set(packages), {"Existing", "New"})
            self.assertEqual(packages["New"].attrib["description"], "do not replace")

    def test_safe_join_fills_empty_footprints_and_reports_nonempty_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            source, existing = tmp_path / "demo.kicad_mod", tmp_path / "packages.xml"
            output, conflicts = tmp_path / "packages.new.xml", tmp_path / "conflicts.txt"
            source.write_text('''(footprint "Demo"
              (pad "1" smd rect (at -1 0) (size 1 1))
              (pad "2" smd rect (at 1 0) (size 1 1)))''')
            existing.write_text('''<openpnp-packages>
              <package id="Demo" bottom-vision-id="configured"><footprint units="Millimeters"/></package>
            </openpnp-packages>''')
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), str(source), "--packages", str(output), "--join", str(existing),
                 "--update-empty-packages", "--conflicts", str(conflicts)], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            package = ET.parse(output).find("package")
            self.assertEqual(package.attrib["bottom-vision-id"], "configured")
            self.assertEqual([pad.attrib["name"] for pad in package.findall("./footprint/pad")], ["1", "2"])
            self.assertFalse(conflicts.exists())

            existing.write_text('''<openpnp-packages>
              <package id="Demo"><footprint units="Millimeters"><pad name="old"/></footprint></package>
            </openpnp-packages>''')
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), str(source), "--packages", str(output), "--join", str(existing),
                 "--update-empty-packages", "--conflicts", str(conflicts)], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("existing footprint is not empty", conflicts.read_text())
            self.assertIn("1 join conflict", completed.stderr)
            self.assertEqual(ET.parse(output).find("./package/footprint/pad").attrib["name"], "old")

    def test_yolo_join_replaces_existing_package(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            source, existing, output = tmp_path / "demo.kicad_mod", tmp_path / "packages.xml", tmp_path / "packages.new.xml"
            source.write_text('(footprint "Demo" (pad "1" smd rect (at 0 0) (size 1 1)))')
            existing.write_text('''<openpnp-packages>
              <package id="Demo" bottom-vision-id="configured"><footprint units="Millimeters"><pad name="old"/></footprint></package>
            </openpnp-packages>''')
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), str(source), "--packages", str(output), "--join", str(existing),
                 "--replace-packages"], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            package = ET.parse(output).find("package")
            self.assertNotIn("bottom-vision-id", package.attrib)
            self.assertEqual([pad.attrib["name"] for pad in package.findall("./footprint/pad")], ["1"])

    def test_join_rejects_duplicate_existing_package_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            source, existing, output = tmp_path / "demo.kicad_mod", tmp_path / "packages.xml", tmp_path / "packages.new.xml"
            source.write_text('(footprint "Demo" (pad "1" smd rect (at 0 0) (size 1 1)))')
            existing.write_text('''<openpnp-packages>
              <package id="Duplicate"><footprint units="Millimeters"/></package>
              <package id="Duplicate"><footprint units="Millimeters"/></package>
            </openpnp-packages>''')
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), str(source), "--packages", str(output), "--join", str(existing)],
                capture_output=True, text=True)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("duplicate package ID 'Duplicate'", completed.stderr)

    def test_join_packages_and_parts_for_board(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            board = tmp_path / "demo.kicad_pcb"
            package_join, part_join = tmp_path / "packages.xml", tmp_path / "parts.xml"
            board.write_text('''(kicad_pcb (version 20240108) (generator pcbnew)
              (footprint "Resistor_SMD:R_0603" (property "Reference" "R1") (property "Value" "10k") (pad "1" smd rect (at 0 0) (size 1 1))))''')
            package_join.write_text('<openpnp-packages><package id="Old"><footprint units="Millimeters"/></package></openpnp-packages>')
            part_join.write_text('<openpnp-parts><part id="Old" package-id="Old"/></openpnp-parts>')
            completed = subprocess.run([sys.executable, str(SCRIPT), str(board), "--join", str(package_join), "--join", str(part_join)], cwd=tmp_path, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(len(ET.parse(tmp_path / "packages.new.xml").findall("package")), 2)
            self.assertEqual(len(ET.parse(tmp_path / "parts.new.xml").findall("part")), 2)
