VARIANT ?= usb

# usb / server: the cloud-init-in-QEMU disk-image bake (build.yaml).
# live: live-build-driven kernel + initrd + squashfs (live.yaml).
ifeq ($(VARIANT),live)
TASK := tasks/live.yaml
else
TASK := tasks/build.yaml
endif

.DEFAULT_GOAL := help

.PHONY: help deps build clean

help:
	@echo "bty-media - bty appliance image builder"
	@echo ""
	@echo "Targets:"
	@echo "  deps     Install build dependencies (cijoe via pipx)"
	@echo "  build    Build the appliance image"
	@echo "  clean    Remove build artifacts"
	@echo ""
	@echo "Variant: $(VARIANT)  (override with VARIANT=server or VARIANT=live)"
	@echo ""
	@echo "Variants:"
	@echo "  usb     - bootable USB live image carrying bty + image set (.img.zst)"
	@echo "  server  - installable disk image hosting the bty server (.img.zst)"
	@echo "  live    - kernel + initrd + squashfs for PXE-flash clients"
	@echo ""
	@echo "Pipeline ($(TASK)) runs against configs/$(VARIANT).toml."
	@echo "Live variant additionally needs live-build on the build host:"
	@echo "  sudo apt install live-build"

deps:
	pipx install cijoe
	pipx ensurepath

build:
	cijoe $(TASK) --monitor -c configs/$(VARIANT).toml

clean:
	rm -rf cijoe-output cijoe-archive _build
