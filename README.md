# KiCad 10 to OpenPnP

Convert KiCad footprints and boards into OpenPnP package and part definitions.
The converter is dependency-free and runs on Python 3.10+; it does not require
KiCad Python bindings.

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

Convert one footprint or a `.pretty` footprint library:

```bash
kicad10_to_openpnp MyFootprints.pretty
```

This creates `packages.new.xml`.

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

## Notes

Package IDs use the KiCad footprint name. Part IDs use the KiCad Value, falling
back to the Reference. Check component heights and nozzle assignments in
OpenPnP before assembling a board.
