# KiCad 10 to OpenPnP

Convert KiCad footprints and boards into OpenPnP package and part definitions.
The converter is dependency-free and runs on Python 3.10+ (to at least 3.13); it does not require
KiCad Python bindings. The Idea and parts of the Code were copied from https://github.com/mmalecki/kicad-to-openpnp , which was not compatible with KiCad 10.

## Install

From this project directory:

```bash
pipx install .
```

## Usage

Convert a KiCad board:

```bash
kicad10_to_openpnp board.kicad_pcb
```

This creates these files in the current directory:

- `packages.new.xml` — OpenPnP package definitions
- `parts.new.xml` — OpenPnP part definitions

Convert a single KiCad 10 footprint directly to an OpenPnP packages file:

```bash
kicad10_to_openpnp --footprint Connector_USB.pretty/USB_C_Receptacle.kicad_mod
```

Convert every footprint in a KiCad `.pretty` library directly to packages and
pads:

```bash
kicad10_to_openpnp --footprint-library Connector_USB.pretty
```

The positional input continues to accept either form too:

```bash
kicad10_to_openpnp MyFootprints.pretty
```

This creates `packages.new.xml`.

### Generated package data

For each package, the converter writes KiCad pad positions and dimensions,
plus the physical body width and height from the footprint's `F.Fab` outline
when available. It reads fabrication rectangles, lines, polygons, circles, and
arc geometry. This body size helps OpenPnP vision distinguish the component
body from its pads.

Standard KiCad packages use compact IDs: `C_0603_1608Metric` becomes `0603`,
`SOT-23` remains `SOT-23`, and `DIP-16_W7.62mm` becomes `DIP-16`. Custom or
unrecognized footprints retain their full `library:footprint` name. Part IDs
are written as `identifier:package:value` when the KiCad footprint has a
reference designator, or `package:value` otherwise. The identifier is the
reference prefix: for example `C:0603:100n`, `R:0805:10kR`, or `D:1206:LED`.

Choose different output files if needed:

```bash
kicad10_to_openpnp board.kicad_pcb --packages packages.xml --parts parts.xml
```

Use `--no-parts` to create packages only from a board. Run
`kicad10_to_openpnp --help` for all options.

### Add to an existing OpenPnP library

Use `--join` to copy an existing library into the new output and append only
definitions whose IDs do not already exist. Repeat it for both library files:

```bash
kicad10_to_openpnp board.kicad_pcb \
  --join ~/.openpnp2/packages.xml \
  --join ~/.openpnp2/parts.xml
```

This writes `packages.new.xml` and `parts.new.xml`; it never overwrites the
files supplied to `--join`.

### Updating joined packages

By default, matching package IDs are preserved. Use the safe update mode to
add generated pads only when an existing package's `<footprint>` is completely
empty; OpenPnP settings such as bottom vision and nozzle assignments are kept.
Any packages that cannot be safely updated are listed on standard error and
can also be saved to a file:

```bash
kicad10_to_openpnp board.kicad_pcb \
  --join ~/.openpnp2/packages.xml \
  --update-empty-packages --conflicts join-conflicts.txt
```

`--replace-packages` is the unsafe alternative: it completely replaces any
joined package with the generated definition, including its OpenPnP-specific
settings. Use it only when that loss is intended.

## Notes

Package IDs use the shortest conventional identifier for standard KiCad
footprints and the full `library:footprint` name for custom ones. Part IDs use
`identifier:package:value` when a reference is present, falling back to
`package:value` otherwise. This keeps otherwise ambiguous KiCad values such
as `LED` distinct by package.

When KiCad's standard footprint library is installed, the converter uses its
base footprint for variants intended for hand soldering or long pads. For
example, `C_0603_1608Metric_Pad1.08x0.95mm_HandSolder` becomes
`Capacitor_SMD:C_0603_1608Metric`, and `DIP-16_W7.62mm_LongPads` becomes
`Package_DIP:DIP-16_W7.62mm`. The footprint library is found through
`KICAD10_FOOTPRINT_DIR`, `KICAD_FOOTPRINT_DIR`, or KiCad's standard Linux
install locations. When a footprint has an `F.Fab` body outline, its bounding
box is exported as OpenPnP `body-width` and `body-height` for vision use; for
example, the standard 0603 is `1.6 × 0.8 mm`. Check component heights and
nozzle assignments in OpenPnP before assembling a board.

Most changes to the original author's code were made with AI assistance.

## License

This project is licensed under the [MIT License](LICENSE).
