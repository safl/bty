VARIANT ?= usb

.DEFAULT_GOAL := help

.PHONY: help deps build clean

help:
	@echo "bty-media - bty appliance image builder"
	@echo ""
	@echo "Targets:"
	@echo "  deps     Install build dependencies (cijoe via pipx)"
	@echo "  build    Build the appliance image (.img.zst)"
	@echo "  clean    Remove build artifacts"
	@echo ""
	@echo "Variant: $(VARIANT)  (override with VARIANT=server)"
	@echo ""
	@echo "Variants:"
	@echo "  usb     - bootable USB live image carrying bty + image set"
	@echo "  server  - installable disk image hosting the bty server"
	@echo ""
	@echo "The build downloads the Debian cloud image, drives cloud-init"
	@echo "in QEMU to bake the rootfs, converts the result to raw,"
	@echo "and zstd-compresses to bty-$(VARIANT)-x86_64.img.zst."

deps:
	pipx install cijoe
	pipx ensurepath

build:
	cijoe tasks/build.yaml --monitor -c configs/$(VARIANT).toml

clean:
	rm -rf cijoe-output cijoe-archive
