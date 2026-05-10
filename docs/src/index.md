```{image} _static/bty-mascot.png
:alt: bty mascot - a blue bat holding a PXE handshake card and a disk labelled .qcow2 / .img / .raw
:width: 240px
:align: center
```

# bty - flash images onto target disks, locally or over PXE

```{only} html
[![CI](https://github.com/safl/bty/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/safl/bty/actions/workflows/ci.yml)
[![Docs](https://github.com/safl/bty/actions/workflows/docs.yml/badge.svg?branch=main)](https://github.com/safl/bty/actions/workflows/docs.yml)
[![PyPI](https://img.shields.io/pypi/v/bty-lab.svg)](https://pypi.org/project/bty-lab/)
[![Python](https://img.shields.io/pypi/pyversions/bty-lab.svg)](https://pypi.org/project/bty-lab/)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://github.com/safl/bty/blob/main/LICENSE)
```

Image-flash provisioning toolkit for bare-metal and virtual targets.
Writes pre-built ("cooked") system images onto target disks - locally
from a USB live stick or remotely over PXE - and configures the
image. First-boot bring-up (users, network, packages) is baked into
the image upstream by the cooker; post-boot config (when the MAC is
managed by bty-web) runs as a CIJOE task over SSH.

```{toctree}
:maxdepth: 2
:caption: Get started

overview
quickstart
walkthrough-usb
walkthrough-server
walkthrough-server-docker
walkthrough-catalog
```

```{toctree}
:maxdepth: 2
:caption: Reference

concepts
flows
components
dependencies
related
reference
```
