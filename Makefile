# bty top-level Makefile
#
# All common operations in one place; ``make help`` lists them.
# Operators run everything from the repo root: ``make build
# VARIANT=server``, ``make test``, ``make ci``, etc.

UV      ?= uv
VARIANT ?= usb

# Per-variant cijoe workflow file under cijoe/tasks/. usb / server share
# the cloud-init-in-QEMU bake; live uses the live-build pipeline.
ifeq ($(VARIANT),live)
MEDIA_TASK := tasks/live.yaml
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
	@echo "Dev (Python package):"
	@echo "  deps          uv sync --all-extras --group dev"
	@echo "  test          pytest (excludes integration / pxe markers)"
	@echo "  lint          ruff check"
	@echo "  format        ruff format (writes)"
	@echo "  format-check  ruff format --check"
	@echo "  typecheck     mypy src"
	@echo "  ci            lint + format-check + typecheck + test"
	@echo "  wheel         uv build (wheel + sdist into ./dist/)"
	@echo ""
	@echo "Media (cijoe pipelines under cijoe/):"
	@echo "  media-deps    pipx install cijoe"
	@echo "  build         build a media image (override VARIANT below)"
	@echo "  test-pxe      end-to-end PXE chain test against pre-built artefacts"
	@echo ""
	@echo "Variant: $(VARIANT)  (override with VARIANT=server or VARIANT=live)"
	@echo "  usb     - bootable USB live image (.img.zst)"
	@echo "  server  - server appliance image (.img.zst)"
	@echo "  live    - kernel + initrd + squashfs for PXE-flash clients"
	@echo ""
	@echo "Docs (bty-docs sibling; ``pipx install ./docs/tooling`` first):"
	@echo "  docs-html     bty-docs-build-html"
	@echo "  docs-pdf      bty-docs-build-pdf"
	@echo "  docs-serve    bty-docs-serve (live-rebuild dev server)"
	@echo ""
	@echo "Cleanup:"
	@echo "  clean         remove build artefacts (Python + media)"

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
#   make build VARIANT=usb     - bootable USB live image (.img.zst)
#   make build VARIANT=server  - server appliance (.img.zst)
#   make build VARIANT=live    - kernel + initrd + squashfs for PXE clients
#
# usb / server use cloud-init in QEMU (cijoe/tasks/build.yaml) and
# need ``qemu-system-x86_64`` + KVM accessible. live uses live-build
# (cijoe/tasks/live.yaml) which needs ``live-build`` on the host
# and passwordless sudo.
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
