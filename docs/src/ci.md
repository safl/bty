# CI and release verification

bty ships from a single GitHub Actions pipeline,
[`.github/workflows/ci-cd.yml`](https://github.com/safl/bty/blob/main/.github/workflows/ci-cd.yml)
(workflow name **CI**). The same suite runs on every pull request and every
push to `main`; pushing a `v*` tag runs it again and additionally publishes.
Nothing is published from a red build.

## Triggers

| Event | What runs |
|---|---|
| Pull request to `main` | Full verification suite (no publish) |
| Push to `main` | Full suite, then **auto-tag** if the version is new (`tag-release`) |
| Push of a `v*` tag | Full suite, then **publish** (PyPI + ghcr.io + GitHub release) |
| `workflow_dispatch` | `test` + `build-wheel` only (a quick package smoke test) |

## Verification jobs

Everything below must be green before anything publishes.

- **check-not-published** -- aborts early if PyPI already has the version, so a
  re-tag can't rebuild release assets from a newer commit while PyPI stays
  frozen (the destinations would drift out of sync).
- **lint** -- `pre-commit` (ruff check + format, shellcheck, hygiene hooks).
- **test** -- the typecheck + pytest matrix across Python 3.11-3.14.
- **flash-integration** -- the flash pipeline against a loop device.
- **build-wheel** -- `uv build` of the `bty-lab` wheel + sdist.
- **build-ipxe** -- compiles bty's custom embedded-chain `ipxe.efi` (see below).
- **build-media** -- the netboot live image (kernel + initrd + squashfs).
- **build-usb-x86** -- the bootable USB live ISO.
- **test-pxe** -- the end-to-end PXE chain test (see below).
- **test-usb-grow** -- boots the USB ISO in QEMU and asserts the first-boot
  service grows `BTY_IMAGES` to fill the stick.
- **test-usb-ventoy** -- Ventoy-boots the ISO in QEMU and asserts image +
  catalog discovery surfaces an operator-dropped image and catalog entry.
- **docs** -- HTML + PDF build (a broken PDF blocks the release).

## The PXE chain test

`test-pxe` is the end-to-end proof that a target can PXE-boot and flash
itself. It builds the bty-web image from the checkout, runs it as a
container, and PXE-boots a QEMU client VM against it over a host bridge:

1. A host bridge (`br-pxe`) carries the server-side IP and a user-owned tap
   for the client VM. A test-side `dnsmasq` on the bridge does DHCP + TFTP
   (bty itself never runs DHCP -- this is synthetic test machinery).
2. The bty-web container publishes `:8080`; the test drives the production
   HTTP API to seed the live trio, a dummy flash image, and a per-MAC
   `boot_mode=bty-flash-always` assignment.
3. The QEMU client PXE-boots, chainloads iPXE, fetches the per-MAC bootstrap,
   loads kernel + initrd + squashfs over HTTP, and runs `bty` in auto-flash
   mode.

The test tails the client's serial console and asserts every stage marker
appears -- iPXE loaded, `/pxe-bootstrap.ipxe` fetched, the per-MAC chain, the
kernel/initrd/squashfs fetch, `bty: auto-flash starting`, and `bty: flash
complete; rebooting`. It runs `make test-pxe`
(`cijoe/scripts/pxe_prepare.py` + `pxe_run_chain_test.py`).

## The custom iPXE

`build-ipxe` runs `make ipxe`, which compiles bty's slim, embedded-chain
`ipxe.efi` (~1 MB). Its embedded script chains to
`http://${next-server}:8080/pxe-bootstrap.ipxe`, so the operator's DHCP only
needs a single bootfile -- no userclass logic. The binary is:

- baked into the **bty-web** image (`docker/seed/`), which seeds it into the
  HTTP boot dir for UEFI HTTP-Boot;
- baked into the **bty-tftp** sidecar (`deploy/tftp/seed/`) for TFTP;
- attached to the GitHub release.

It's x86_64-EFI only; the arm64 images fall back to stock iPXE (the documented
BIOS/arm64 chain-loop caveat).

## Auto-release

A version bump on `main` releases itself, but only off a green build:

1. **tag-release** runs on `main` after the entire verification suite passes.
   It reads the version from `pyproject.toml` and, if no matching `v<version>`
   tag exists, creates and pushes it. The push uses a PAT (`RELEASE_PAT`) so
   the new tag triggers a fresh workflow run -- a `GITHUB_TOKEN`-pushed tag
   would not.
2. The tagged run re-runs the suite and then publishes:
   - **attach-to-release** -- gathers the wheel/sdist, custom `ipxe.efi`,
     netboot trio, USB ISO, PDF docs, a generated `catalog.toml`, and a
     `release.toml` manifest, and attaches them to the GitHub release.
   - **publish-pypi** -- trusted-publishes the wheel + sdist (strictly last;
     a published PyPI version can never be re-uploaded).
   - **publish-docker** / **publish-tftp** -- build and push the multi-arch
     `ghcr.io/safl/bty-web` and `ghcr.io/safl/bty-tftp` images, staging the
     custom `ipxe.efi` into each build context first.

## Running it locally

```sh
make ci          # lint + format-check + typecheck + test (the package side)
make test-pxe    # the end-to-end PXE chain test (needs QEMU + KVM + podman)
make ipxe        # build the custom ipxe.efi -> dist/ipxe/ipxe.efi
make build VARIANT=netboot-x86   # the netboot live image
```
