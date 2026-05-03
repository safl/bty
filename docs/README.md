# bty documentation

Documentation source for the bty project. Built with Sphinx + MyST,
following the layout used by `safl/aisio`.

## Required tools

- Python 3
- pipx (with `pipx ensurepath` run once)
- make
- A LaTeX distribution for the PDF build (`texlive` variants on Linux,
  `latexmk` via MacTeX on macOS)

## Build setup

From the `bty/docs` directory, install the tooling package:

```bash
pipx install ./tooling
```

This installs three commands: `bty-docs-serve`, `bty-docs-build-html`,
`bty-docs-build-pdf`.

## Development server

Run the live-rebuild dev server:

```bash
bty-docs-serve
```

This watches `docs/src/` and rebuilds HTML on every change. The server
listens on `http://localhost:8000`.

## One-shot builds

```bash
bty-docs-build-html   # HTML only
bty-docs-build-pdf    # PDF only
```

Output lands under `docs/_build/`.
