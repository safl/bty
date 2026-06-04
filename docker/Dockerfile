# bty-web container.
#
# What this is: a full bty-server deployment as a single OCI
# image -- bty-web (HTTP + browser UI) AND dnsmasq (TFTP serving
# the iPXE binaries) running side by side. Functionally
# equivalent to the bty-server appliance: operators can configure
# their LAN DHCP server to point PXE clients at this container
# for both TFTP (option 66) and HTTP-Boot (HTTPClient option 67)
# fetches.
#
# bty does NOT run any DHCP role -- the operator's existing LAN
# DHCP server is the authoritative DHCP source, configured with
# option 60 "PXEClient" / 66 next-server / 67 bootfile pointing
# at this container. Same architecture as the appliance.
#
# Built off the local wheel produced by ``uv build``; the GHA
# workflow stages ``dist/bty_lab-*.whl`` into the build context
# before invoking buildx, so the container's bty-web is exactly
# the version published to PyPI for the same tag.

FROM python:3.13-slim-bookworm AS runtime

# OCI image metadata. The version label is filled by the GHA
# release workflow's build-push-action via ``--label`` or by the
# Dockerfile-internal ARG below if a future build invokes ``docker
# build --build-arg BTY_VERSION=...`` directly. Other labels are
# static identity for the bty project.
ARG BTY_VERSION=dev
LABEL org.opencontainers.image.title="bty-web" \
      org.opencontainers.image.description="bty-web - HTTP server with browser UI for bty image catalog + machine registry" \
      org.opencontainers.image.source="https://github.com/safl/bty" \
      org.opencontainers.image.url="https://github.com/safl/bty" \
      org.opencontainers.image.documentation="https://safl.dk/bty" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      org.opencontainers.image.version="${BTY_VERSION}"

# System deps:
#   libpam0g + libpam-modules: pamela uses pam_unix.so to verify
#     /etc/shadow on /ui/login.
#   qemu-utils: ``bty.flash.probe_image`` shells out to qemu-img
#     when bty-web inspects an uploaded image.
#   tini: PID-1 init that forwards SIGTERM cleanly so
#     ``docker stop`` doesn't fall back to SIGKILL after 10s.
#   ca-certificates: HTTPS uploads / downloads.
#   curl: HEALTHCHECK probes ``/healthz`` so docker/orchestrators
#     detect a wedged uvicorn (Python deadlock, OOM, etc.) without
#     needing to add curl per-deployment.
#   dnsmasq + ipxe: TFTP serving + the iPXE bootfiles. ``setcap``
#     grants dnsmasq the CAP_NET_BIND_SERVICE capability so it can
#     bind UDP 69 as the unprivileged ``bty`` user (no need to run
#     the daemon as root).
#   libcap2-bin: provides setcap.
#   procps: pgrep, used by ``_sysconfig.tftp_status`` when
#     systemctl isn't around (i.e. inside this container) so the
#     UI's TFTP badge still reflects whether dnsmasq is alive.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        qemu-utils \
        tini \
        ca-certificates \
        curl \
        dnsmasq \
        ipxe \
        libcap2-bin \
        procps \
 && setcap 'cap_net_bind_service=+ep' /usr/sbin/dnsmasq \
 && rm -rf /var/lib/apt/lists/*

# Stage the iPXE binaries dnsmasq will hand out via TFTP. Symlinks
# rather than copies so apt upgrades of the iPXE package carry
# through. The same shape the appliance uses (see
# bty-media/auxiliary/cloudinit-base-server.user).
RUN install -d -o root -g root -m 0755 /var/lib/tftpboot \
 && ln -sf /usr/lib/ipxe/undionly.kpxe /var/lib/tftpboot/undionly.kpxe \
 && ln -sf /usr/lib/ipxe/ipxe.efi /var/lib/tftpboot/ipxe.efi

# dnsmasq config: TFTP-only (no DNS, no DHCP), drops to bty user.
COPY docker/dnsmasq.conf /etc/dnsmasq.conf

# Install bty-lab[web] from the wheel staged by the workflow into
# ./dist/. The wheel is platform-independent (pure Python), so
# the same dist/ works for amd64 and arm64 buildx targets.
#
# The two-step shell-glob expansion is deliberate: pip parses
# ``bty_lab-*-py3-none-any.whl[web]`` as a literal filename + extra
# spec, but in /bin/sh the trailing ``[web]`` is a glob bracket
# pattern (any one of w/e/b), which doesn't match anything and
# leaves pip with the literal ``*`` in the filename. Resolve the
# wheel name first, then concatenate the ``[web]`` extra.
#
# ``sort -V | tail -1`` picks the highest version when dist/ has
# multiple wheels (which it commonly does -- ``uv build`` appends
# rather than wipes). A previous bug: ``head -1`` chose the
# alphabetically-first wheel, so dist/ holding 0.22.11 + 0.22.12
# baked 0.22.11 into the container and operators saw stale UI.
COPY dist/bty_lab-*-py3-none-any.whl /tmp/wheels/
RUN set -eu; \
    WHL=$(ls /tmp/wheels/bty_lab-*-py3-none-any.whl | sort -V | tail -1); \
    pip install --no-cache-dir "${WHL}[web]"; \
    rm -rf /tmp/wheels

# The bty user runs the bty-web process at runtime (see USER below). The
# operator UI is gated by ``$BTY_ADMIN_PASSWORD`` (set it at run time; unset
# leaves the UI open, with a startup warning).
#
# ``--uid 1000`` pins the UID so the operator's host-side ``chown
# -R 1000:1000 ./bty-data`` doesn't break when apt's package order
# changes the auto-assigned UID. Empirically dnsmasq's postinst
# grabs UID 999 first on Debian 13, so ``useradd --system`` would
# otherwise hand bty UID 998 -- and the Makefile / docker-compose
# / docs would all silently fall out of sync.
RUN groupadd --gid 1000 bty \
 && useradd --uid 1000 --gid 1000 --no-create-home \
        --shell /usr/sbin/nologin bty

# State + image directories. Operator volume-mounts ``/var/lib/bty``;
# bty-web writes ``state.db``, ``session-secret``, and the image
# catalog under that root. Owned by bty so the unprivileged service
# user can write without needing chown at every container start.
RUN install -d -o bty -g bty -m 0750 /var/lib/bty /var/lib/bty/images
VOLUME /var/lib/bty

# bty-web reads these env vars; defaults match the on-image
# directory layout above. ``BTY_BOOT_DIR`` is set explicitly (the
# code would default it to ``state_dir/boot`` anyway, but spelling
# it out keeps the container's env in sync with the appliance's
# ``/etc/default/bty-web`` so drift is harder to introduce.)
ENV BTY_STATE_DIR=/var/lib/bty \
    BTY_IMAGE_ROOT=/var/lib/bty/images \
    BTY_BOOT_DIR=/var/lib/bty/boot \
    BTY_WEB_HOST=0.0.0.0 \
    BTY_WEB_PORT=8080

EXPOSE 8080
EXPOSE 69/udp

# Purpose-built health endpoint. Lighter than probing ``/`` (no
# template rendering, no redirect) and explicit about its intent.
# 30s start-period covers cold-start (Python import + sqlite
# migrations); after that, probe every 30s, 5s timeout, three
# failures = unhealthy. Catches a wedged uvicorn or a process
# that crashed with the port still held.
# Probe the configured port (shell-form CMD expands the env var at
# runtime): an operator who overrides ``BTY_WEB_PORT`` would
# otherwise get a perpetually-unhealthy container because the
# probe hit the stale default :8080.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${BTY_WEB_PORT:-8080}/healthz" -o /dev/null || exit 1

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

# Run as the unprivileged bty user. tini reaps zombies and forwards signals
# so SIGTERM lands on bty-web within ~100ms instead of the 10s docker-stop
# timeout.
USER bty
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
