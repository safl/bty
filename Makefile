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
        docker-build docker-run docker-clean \
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
	@echo "                  -> ~/system_imaging/disk/bty-<variant>.*"
	@echo "  test-pxe      end-to-end PXE chain test against pre-built artefacts"
	@echo "                  (needs QEMU + KVM; ~5-10 min wall clock)"
	@echo ""
	@echo "Variant: $(VARIANT)  (override with VARIANT=server-x86, server-rpi, netboot-x86, ...)"
	@echo "  usb-x86      - bootable USB live ISO via live-build (.iso.gz, x86_64)"
	@echo "  server-x86   - server appliance image (.img.gz, x86_64; needs qemu-system-x86_64 + KVM)"
	@echo "  server-rpi   - server appliance for Raspberry Pi 4/5 (.img.gz, arm64; needs qemu-user-static + binfmt_misc)"
	@echo "  netboot-x86  - kernel + initrd + squashfs trio for PXE-flash clients (x86_64)"
	@echo ""
	@echo "Docker (trial / image-library bty-web container):"
	@echo "  docker-build  uv build + docker build -> bty-web:dev (single-arch, local)"
	@echo "  docker-run    run bty-web:dev with ./bty-data/ bind-mount on :8080"
	@echo "  docker-clean  stop container, remove bty-web:dev image, wipe ./bty-data/"
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
#   make build VARIANT=usb-x86      - bootable USB live ISO (.iso.gz, x86_64)
#   make build VARIANT=server-x86   - server appliance (.img.gz, x86_64)
#   make build VARIANT=server-rpi   - server appliance for RPi 4/5 (.img.gz, arm64)
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

# ---------- Docker ------------------------------------------------------

# Local single-arch build of the bty-web container. Stages the
# wheel into ./dist/ first so the Dockerfile's COPY finds it.
# Multi-arch + push lives in .github/workflows/release.yml --
# this target is the "smoke test it on my laptop" path.
.PHONY: docker-build docker-run

docker-build:
	$(UV) build
	docker build \
	    --build-arg BTY_VERSION=$$(awk -F'"' '/^version =/ {print $$2; exit}' pyproject.toml) \
	    -f docker/Dockerfile -t bty-web:dev .

# Trial run with a host-side data dir so dropped images show up in
# the container catalog. The bty-web container runs as uid 999
# (the bty user, mirroring the appliance's ``User=bty`` for PAM
# auth via setgid unix_chkpwd); a bare bind-mount the operator
# owns would fail the entrypoint's writability preflight, so we
# chown the dir to 999:999 first. Requires sudo; alternative is
# ``docker run -v bty-data:/var/lib/bty ...`` with a managed
# volume (no chown needed, harder to inspect from the host).
docker-run:
	mkdir -p bty-data/images
	sudo chown -R 999:999 bty-data
	docker run -d --name bty-web --rm -p 8080:8080 \
	    -v "$(CURDIR)/bty-data":/var/lib/bty bty-web:dev
	@echo "bty-web running on http://localhost:8080/ui (login: bty / bty)"
	@echo "logs: docker logs -f bty-web ; stop: docker stop bty-web"

# Stop a running ``bty-web`` container, remove the local
# ``bty-web:dev`` image, and (with sudo) wipe the host-side
# data dir. Keeps the docker-build / docker-run / docker-clean
# triplet symmetric. Operators occasionally want to rebuild
# fresh after a Dockerfile change without leftover state from
# a prior run.
docker-clean:
	-docker stop bty-web 2>/dev/null
	-docker image rm bty-web:dev 2>/dev/null
	-sudo rm -rf bty-data

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
