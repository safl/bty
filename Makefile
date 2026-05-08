# bty top-level Makefile
#
# All common operations in one place; ``make help`` lists them.
# Operators run everything from the repo root: ``make build
# VARIANT=server-x86``, ``make test``, ``make ci``, etc.

UV      ?= uv
VARIANT ?= usb-x86

# Per-variant cijoe workflow file under cijoe/tasks/.
#  - usb-x86 uses the live-build iso-hybrid pipeline (usb.yaml).
#  - netboot-x86 uses the live-build netboot pipeline (netboot.yaml).
#    Renamed from live-x86 in M19 phase 5.
#  - server-x86 uses the cloud-init-in-QEMU bake (build.yaml)
#  - server-rpi mounts a Raspberry Pi OS image and chroots via
#    qemu-aarch64-static (build-rpi.yaml)
ifeq ($(VARIANT),netboot-x86)
MEDIA_TASK := tasks/netboot.yaml
else ifeq ($(VARIANT),usb-x86)
MEDIA_TASK := tasks/usb.yaml
else ifeq ($(VARIANT),server-rpi)
MEDIA_TASK := tasks/build-rpi.yaml
else
MEDIA_TASK := tasks/build.yaml
endif

.DEFAULT_GOAL := help

.PHONY: help \
        deps test lint format format-check typecheck ci wheel \
        media-deps build test-pxe \
        docs-html docs-pdf docs-serve \
        clean

help:
	@echo "bty top-level Makefile"
	@echo ""
	@echo "Dev (Python package, no sudo, no network beyond uv):"
	@echo "  deps          uv sync --all-extras --group dev"
	@echo "  test          pytest (excludes integration / pxe markers)"
	@echo "  lint          ruff check"
	@echo "  format        ruff format (writes)"
	@echo "  format-check  ruff format --check"
	@echo "  typecheck     mypy src"
	@echo "  ci            lint + format-check + typecheck + test"
	@echo "  wheel         uv build  -> dist/bty_lab-X.Y.Z-py3-none-any.whl + sdist"
	@echo ""
	@echo "Media (cijoe pipelines under cijoe/; require passwordless sudo):"
	@echo "  media-deps    pipx install cijoe"
	@echo "  build         build a media image (override VARIANT below)"
	@echo "                  -> bty-media/output/<variant>-X.Y.Z.{iso.gz,img.zst,tar.zst}"
	@echo "  test-pxe      end-to-end PXE chain test against pre-built artefacts"
	@echo "                  (needs QEMU + KVM; ~5-10 min wall clock)"
	@echo ""
	@echo "Variant: $(VARIANT)  (override with VARIANT=server-x86, server-rpi, netboot-x86, ...)"
	@echo "  usb-x86      - bootable USB live ISO via live-build (.iso.gz, x86_64)"
	@echo "  server-x86   - server appliance image (.img.zst, x86_64; needs qemu-system-x86_64 + KVM)"
	@echo "  server-rpi   - server appliance for Raspberry Pi 4/5 (.img.zst, arm64; needs qemu-user-static + binfmt_misc)"
	@echo "  netboot-x86  - kernel + initrd + squashfs for PXE-flash clients (.tar.zst, x86_64)"
	@echo ""
	@echo "Docs (bty-docs sibling; ``pipx install ./docs/tooling`` first):"
	@echo "  docs-html     bty-docs-build-html  -> docs/_build/html/"
	@echo "  docs-pdf      bty-docs-build-pdf   -> docs/_build/pdf/bty.pdf"
	@echo "  docs-serve    bty-docs-serve (live-rebuild dev server on :8000)"
	@echo ""
	@echo "Cleanup:"
	@echo "  clean         remove build artefacts (Python dist/, cijoe-output, _build, caches)"

# ---------- Python package ----------------------------------------------

deps:
	$(UV) sync --all-extras --group dev

test:
	$(UV) run pytest

lint:
	$(UV) run ruff check

format:
	$(UV) run ruff format

format-check:
	$(UV) run ruff format --check

typecheck:
	$(UV) run mypy src

ci: lint format-check typecheck test

wheel:
	$(UV) build

# ---------- Media (bty-media/ via cijoe) ---------------------------------

media-deps:
	pipx install cijoe
	pipx ensurepath

# Build a media image. Pick the variant via ``VARIANT=...``:
#   make build VARIANT=usb-x86      - bootable USB live ISO (.iso.xz, x86_64)
#   make build VARIANT=server-x86   - server appliance (.img.zst, x86_64)
#   make build VARIANT=server-rpi   - server appliance for RPi 4/5 (.img.zst, arm64)
#   make build VARIANT=netboot-x86  - kernel + initrd + squashfs for PXE clients
#
# server-x86 uses cloud-init in QEMU (cijoe/tasks/build.yaml) and
# needs ``qemu-system-x86_64`` + KVM accessible. netboot-x86 +
# usb-x86 both use live-build (cijoe/tasks/netboot.yaml,
# cijoe/tasks/usb.yaml) and need ``live-build`` on the host plus
# passwordless sudo. server-rpi (cijoe/tasks/build-rpi.yaml)
# customises Raspberry Pi OS Lite arm64 via losetup +
# qemu-aarch64-static chroot; needs ``qemu-user-static`` + binfmt_misc
# + passwordless sudo.
build:
	cd cijoe && cijoe $(MEDIA_TASK) --monitor -c configs/$(VARIANT).toml

# End-to-end PXE chain test: server + client QEMU VMs sharing an L2
# segment. Requires pre-built server + live artefacts under
# ~/system_imaging/disk/. Wall clock 5-10 min per run.
test-pxe:
	cd cijoe && cijoe tasks/test-pxe.yaml --monitor -c configs/test-pxe.toml

# ---------- Docs --------------------------------------------------------

docs-html:
	bty-docs-build-html

docs-pdf:
	bty-docs-build-pdf

docs-serve:
	bty-docs-serve

# ---------- Cleanup -----------------------------------------------------

clean:
	rm -rf dist .pytest_cache .ruff_cache .mypy_cache
	rm -rf cijoe/cijoe-output cijoe/cijoe-archive cijoe/_build
