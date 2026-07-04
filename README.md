<p align="center">
  <img src="docs/src/_static/bty-mascot.png" alt="bty mascot" width="240">
</p>

# bty

*Flash operating system images onto target disks.*

*(Pronounced "battie", rhyming with "batty"; the blue bat up top is the mascot.)*

[![CI](https://github.com/safl/bty/actions/workflows/ci-cd.yml/badge.svg?branch=main)](https://github.com/safl/bty/actions/workflows/ci-cd.yml)
[![Documentation](https://img.shields.io/badge/docs-safl.dk%2Fbty-blue)](https://safl.dk/bty)
[![PyPI](https://img.shields.io/pypi/v/bty-lab.svg)](https://pypi.org/project/bty-lab/)
[![Container](https://img.shields.io/badge/container-ghcr.io%2Fsafl%2Fbty--web-blue)](https://github.com/safl/bty/pkgs/container/bty-web)
[![Changelog](https://img.shields.io/badge/changelog-CHANGELOG.md-blue)](https://github.com/safl/bty/blob/main/CHANGELOG.md)

bty writes OS images to bare metal, offline from a USB stick or networked over
PXE, scaling from one machine to a rack on one runtime. The image is the source
of truth: rebuild the image, reflash the target, with no imperative
configuration management. bty is a flasher, not an image builder, so pair it
with one such as [safl/nosi](https://github.com/safl/nosi).

## Documentation

Install, the USB / portable-catalog / PXE-server delivery shapes, the bty-web
HTTP API, and ORAS-published images and catalogs all live at:

### → <https://safl.dk/bty>

## Ecosystem sidecars

The container deploy runs bty-web alongside two sibling services
that are built and released independently:

- [safl/withcache](https://github.com/safl/withcache) -- URL-keyed
  artifact cache; bty's preferred image-bytes source.
- [safl/nbdmux](https://github.com/safl/nbdmux) -- HTTP-controlled
  NBD-export multiplexer that serves `boot_mode=ramboot` targets.
