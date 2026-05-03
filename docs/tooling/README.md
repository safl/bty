# bty-docs

Build and dev-server tooling for the bty project documentation.

Provides three console scripts:

- `bty-docs-serve` — live-rebuild dev server on `http://localhost:8000`.
- `bty-docs-build-html` — one-shot HTML build to `docs/_build/html/`.
- `bty-docs-build-pdf` — one-shot PDF build via LaTeX to
  `docs/_build/latex/bty.pdf`.

## Install

```bash
pipx install ./docs/tooling
```

The PDF build additionally requires a LaTeX distribution (`texlive`
variants on Linux, `latexmk` via MacTeX on macOS).
