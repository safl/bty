"""Sphinx configuration for bty documentation."""

from __future__ import annotations

project = "bty"
author = "Simon A. F. Lund"
copyright = f"2026, {author}"

extensions = [
    "myst_parser",
    "sphinx_copybutton",
]

myst_enable_extensions = [
    "deflist",
    "fieldlist",
    "tasklist",
    "linkify",
    "colon_fence",
]

source_suffix = {".md": "markdown"}
master_doc = "index"

templates_path = ["_templates"]
exclude_patterns: list[str] = ["_build", "Thumbs.db", ".DS_Store"]

# HTML output
html_theme = "furo"
html_title = "bty - flash images onto target disks, locally or remote with and without PXE"
html_static_path = ["_static"]

# LaTeX / PDF output - pdflatex with sane UTF-8 (inputenc utf8). Avoid
# exotic Unicode (arrows, box-drawing, em-dashes) in docs sources;
# smart quotes are fine.
latex_engine = "pdflatex"
latex_documents = [
    ("index", "bty.tex", "bty - flash images onto target disks", author, "manual"),
]
latex_elements = {
    "papersize": "a4paper",
    "pointsize": "11pt",
}
