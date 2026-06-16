# Flash OS images onto target disks

```{only} html
[![CI](https://github.com/safl/bty/actions/workflows/ci-cd.yml/badge.svg?branch=main)](https://github.com/safl/bty/actions/workflows/ci-cd.yml)
[![Docs](https://github.com/safl/bty/actions/workflows/docs.yml/badge.svg?branch=main)](https://github.com/safl/bty/actions/workflows/docs.yml)
[![PyPI](https://img.shields.io/pypi/v/bty-lab.svg)](https://pypi.org/project/bty-lab/)
[![Python](https://img.shields.io/pypi/pyversions/bty-lab.svg)](https://pypi.org/project/bty-lab/)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://github.com/safl/bty/blob/main/LICENSE)
```

Flash a single bare-metal box ad-hoc with a USB stick, or reflash a
whole fleet remotely from a single controller. bty works with or without
PXE and scales from one machine to a rack without changing how you
operate. The image is the source of truth: rebuild the image, reflash
the target.

bty is a flasher, not an image builder. First-boot bring-up (users,
network, packages, hostnames) gets baked into the image upstream with
cloud-init / kickstart / preseed; the companion project
[nosi](https://github.com/safl/nosi) is one such builder, but any source
of pre-built images works. bty just writes the bytes.

```{toctree}
:maxdepth: 1

quickstart
```

```{toctree}
:maxdepth: 2
:caption: Walkthroughs

overview
walkthrough-catalog
walkthrough-image-store
walkthrough-server-docker
```

```{toctree}
:maxdepth: 2
:caption: Tutorials

tutorials/bty-usbboot-pc
tutorials/bty-usbboot-rpi
tutorials/bty-ventoy
tutorials/bmc
tutorials/bty-lab-deploy
tutorials/bty-netboot-pc
```

```{toctree}
:maxdepth: 2
:caption: Reference

concepts
flows
components
operations
dependencies
ci
related
reference
changelog
```
