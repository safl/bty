# bty — Boot & Target Utility

[![CI](https://github.com/safl/bty/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/safl/bty/actions/workflows/ci.yml)
[![Docs](https://github.com/safl/bty/actions/workflows/docs.yml/badge.svg?branch=main)](https://github.com/safl/bty/actions/workflows/docs.yml)
[![PyPI](https://img.shields.io/pypi/v/bty-lab.svg)](https://pypi.org/project/bty-lab/)
[![Python](https://img.shields.io/pypi/pyversions/bty-lab.svg)](https://pypi.org/project/bty-lab/)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://github.com/safl/bty/blob/main/LICENSE)

Bare-metal provisioning toolkit. Flashes pre-built ("cooked") system
images onto target disks — locally from a USB stick or remotely over
PXE — and configures them via cloud-init or CIJOE workflows. Designed
for both ad-hoc one-off provisioning (USB live image) and DevOps fleet
operation (server image with browser UI and iPXE).

```{toctree}
:maxdepth: 2
:caption: Contents

overview
concepts
flows
components
related
reference
```
