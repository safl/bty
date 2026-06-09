"""Layered config for bty-web.

Replaces the v0.41-era ``envvars``-shell-file + ~17 ``$BTY_*`` env-var
reads scattered across the codebase. v0.42+: every operator-tunable
knob is a key in :class:`Config`, sourced from (in increasing
priority):

1. Built-in defaults baked into the dataclass.
2. TOML config file(s) -- a single ``bty.toml``, OR a directory of
   drop-ins (``conf.d/*.toml``), OR several files with the operator's
   explicit ordering.
3. Environment variables -- ``BTY_<SECTION>_<KEY>`` overrides the
   matching TOML key per-key (not per-file).

Each layer overrides the prior on a per-key basis: setting one
key via env doesn't force the operator to also set the rest;
unset keys keep their TOML / default value.

The runtime contract for containers / k8s is **one** env var --
``BTY_CONFIG_FILE`` (or ``BTY_CONFIG_DIR``) -- points the loader at
the operator's TOML. Individual ``BTY_<SECTION>_<KEY>`` env vars are
the override escape hatch for one-shot dev runs, k8s Secrets, etc.

Search order when no explicit ``paths`` argument is passed:

* ``/etc/bty/conf.d/*.toml`` (drop-ins, lexicographic order, system-wide)
* ``/etc/bty/bty.toml`` (single-file system default)
* ``<state_dir>/bty.toml`` (single-file host-local default)

Whichever exist are loaded in that order; later wins per key.
Missing paths are silently skipped -- a fresh deploy with no
``bty.toml`` at all still starts on pure defaults.
"""

from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, get_type_hints

DEFAULT_SYSTEM_CONF_DIR = Path("/etc/bty/conf.d")
DEFAULT_SYSTEM_CONFIG_FILE = Path("/etc/bty/bty.toml")
DEFAULT_STATE_DIR = "/var/lib/bty"

# The well-known default password (matches the v0.41.3 "bty" decision).
# Logged on startup so an operator who's actually exposed bty-web
# doesn't silently ship with the default.
DEFAULT_ADMIN_PASSWORD = "bty"


@dataclass(frozen=True)
class AdminConfig:
    """Operator login + secrets section.

    ``password`` gates the operator UI at ``/ui/login``. Auth is
    always on; an unset value falls back to :data:`DEFAULT_ADMIN_PASSWORD`
    (the literal ``"bty"``) so a fresh deploy comes up clickable.
    Env override: ``BTY_ADMIN_PASSWORD``.
    """

    password: str = DEFAULT_ADMIN_PASSWORD


@dataclass(frozen=True)
class ServerConfig:
    """HTTP server + request-handling section.

    * ``host`` / ``port`` -- where uvicorn binds. Env:
      ``BTY_SERVER_HOST`` / ``BTY_SERVER_PORT``.
    * ``trusted_proxy`` -- read the client IP from ``X-Forwarded-For``
      when set. Only safe behind a reverse proxy that strips inbound
      ``X-Forwarded-For``. Env: ``BTY_SERVER_TRUSTED_PROXY``.
    * ``session_secret`` -- HMAC key for the session cookie. Blank
      means "auto-generate on first start and persist to
      ``<state_dir>/session-secret``" -- the v0.41 default behaviour.
      Env: ``BTY_SERVER_SESSION_SECRET``.
    """

    host: str = "0.0.0.0"
    port: int = 8080
    trusted_proxy: str = ""
    session_secret: str = ""


@dataclass(frozen=True)
class PathsConfig:
    """Filesystem locations bty-web writes to / reads from.

    Each path has a sane default; only ``state_dir`` is grounded in
    an absolute path. The rest default to ``""`` and the loader
    resolves them relative to ``state_dir`` -- so an operator who
    only sets ``state_dir`` gets a coherent layout for free.

    Env: ``BTY_PATHS_STATE_DIR`` / ``BTY_PATHS_BOOT_DIR`` /
    ``BTY_PATHS_BACKUP_DIR`` / ``BTY_PATHS_CATALOG_FILE``.
    """

    state_dir: str = DEFAULT_STATE_DIR
    boot_dir: str = ""  # blank -> <state_dir>/boot
    backup_dir: str = ""  # blank -> <state_dir>/backups
    catalog_file: str = ""  # blank -> <state_dir>/catalog.toml


@dataclass(frozen=True)
class WithcacheConfig:
    """withcache integration section.

    ``url`` is the base URL of the withcache cache-host. Blank means
    "no withcache configured" -- bty-web then streams catalog entries
    from the origin URL directly on each flash. Set to
    ``http://<lan>:3000`` to route through a local withcache.
    Env: ``BTY_WITHCACHE_URL``.
    """

    url: str = ""


@dataclass(frozen=True)
class NetbootConfig:
    """Netboot / TFTP integration section.

    ``tftp_probe_host`` is where the /ui/netboot diagnostic sends its
    TFTP RRQ to test reachability. Default ``127.0.0.1`` works for
    host installs (co-located dnsmasq). Container deploys set this to
    the host's LAN address (the ``bty-tftp`` sidecar uses
    ``network_mode: host``). Env: ``BTY_NETBOOT_TFTP_PROBE_HOST``.
    """

    tftp_probe_host: str = "127.0.0.1"


@dataclass(frozen=True)
class TuningConfig:
    """Concurrency + size limits.

    Defaults match the v0.41-era hardcoded values. Most operators
    never touch these. Env: ``BTY_TUNING_BACKUP_MAX_PARALLEL`` /
    ``BTY_TUNING_MAX_UPLOAD_BYTES``.
    """

    backup_max_parallel: int = 1
    max_upload_bytes: int = 200 * 1024 * 1024 * 1024  # 200 GiB


@dataclass(frozen=True)
class Config:
    """Root config object. One instance per process, built at startup
    and read by every module that used to do ``os.environ.get(...)``.
    """

    admin: AdminConfig = field(default_factory=AdminConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    withcache: WithcacheConfig = field(default_factory=WithcacheConfig)
    netboot: NetbootConfig = field(default_factory=NetbootConfig)
    tuning: TuningConfig = field(default_factory=TuningConfig)

    # ---- Derived path resolvers ------------------------------------------
    #
    # The dataclass stores raw strings; these properties resolve the
    # "" default to ``<state_dir>/<subpath>`` so call sites don't
    # have to repeat the fallback logic.

    @property
    def state_dir(self) -> Path:
        return Path(self.paths.state_dir)

    @property
    def boot_dir(self) -> Path:
        return Path(self.paths.boot_dir) if self.paths.boot_dir else self.state_dir / "boot"

    @property
    def backup_dir(self) -> Path:
        return Path(self.paths.backup_dir) if self.paths.backup_dir else self.state_dir / "backups"

    @property
    def catalog_file(self) -> Path:
        return (
            Path(self.paths.catalog_file)
            if self.paths.catalog_file
            else self.state_dir / "catalog.toml"
        )

    @property
    def state_db(self) -> Path:
        return self.state_dir / "state.db"


# ---- Loader -----------------------------------------------------------------


@dataclass(frozen=True)
class LoadedConfig:
    """The result of :func:`load_config`: the resolved :class:`Config`
    PLUS per-key provenance + the path bty-web can write operator
    edits back to.

    The Settings page reads ``sources["admin.password"]`` to badge
    each row ("default" / "toml(<path>)" / "env(<NAME>)" /
    "override:toml(<path>)") and to decide whether the edit field is
    editable. ``primary_toml`` is the file Settings writes to when
    the operator submits an edit; it's the last writable TOML in the
    search list (or ``None`` if none of the candidates were
    writable -- the UI then surfaces "no writable config file
    available, set ``$BTY_CONFIG_FILE``").

    ``loaded_files`` is the ordered list of TOML files that
    contributed to ``cfg``; used by the Settings page to render the
    full provenance hierarchy when an operator clicks "where did
    this value come from?".
    """

    cfg: Config
    sources: dict[str, str]
    loaded_files: list[Path]
    primary_toml: Path | None


def _merge_into(
    target: dict[str, Any],
    src: dict[str, Any],
    src_label: str,
    sources: dict[str, str],
    prefix: str = "",
) -> None:
    """Recursive per-key merge -- ``src`` overrides ``target`` for
    every key it sets, including inside nested dicts (the sections).
    Lists are replaced, not concatenated.

    Every successful overlay also stamps ``sources[<dotted_key>] =
    src_label`` so :func:`load_config` can report which file (or
    env-var) supplied each final value.
    """
    for k, v in src.items():
        dotted = f"{prefix}{k}"
        if isinstance(v, dict):
            # Initialise the target section if absent so recursion
            # below can stamp the leaf keys (not the section name)
            # in ``sources``. Without this, the first TOML to set
            # ``[section] key`` would stamp ``sources["section"]``
            # instead of ``sources["section.key"]`` and the leaf
            # source would stay "default".
            if not isinstance(target.get(k), dict):
                target[k] = {}
            _merge_into(target[k], v, src_label, sources, prefix=f"{dotted}.")
        else:
            target[k] = v
            sources[dotted] = src_label


def _load_toml_file(path: Path) -> dict[str, Any]:
    """Parse one TOML file. Returns ``{}`` when the file doesn't
    exist; raises on parse error so a malformed config fails loud."""
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _expand_paths(paths: list[Path]) -> list[Path]:
    """Resolve directories to their sorted ``*.toml`` contents.

    A file path is kept as-is; a directory expands to the .toml
    files inside it in lexicographic order (the conventional
    ``conf.d/`` drop-in semantics). Non-existent paths are dropped
    silently -- the loader's default search lists multiple
    candidates and only the present ones contribute."""
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out.extend(sorted(p.glob("*.toml")))
        elif p.is_file():
            out.append(p)
    return out


def _default_search_paths() -> list[Path]:
    """The standard search order when no explicit ``paths`` is given.

    Operators who want a different layout pass ``--config`` (a CLI
    arg) or set ``$BTY_CONFIG_FILE`` / ``$BTY_CONFIG_DIR``. The
    defaults aim at the most common deploys:

    * ``/etc/bty/conf.d/*.toml`` -- system-wide drop-ins
    * ``/etc/bty/bty.toml`` -- system-wide single file
    * ``$BTY_CONFIG_DIR`` -- operator-pointed drop-in directory
    * ``$BTY_CONFIG_FILE`` -- operator-pointed single file
    * ``<state_dir>/bty.toml`` -- host-local single file

    Each later candidate overrides earlier per-key.  The state_dir
    candidate uses the env-var override if set so an operator who
    points ``BTY_PATHS_STATE_DIR`` at ``/srv/bty`` also gets
    ``/srv/bty/bty.toml`` picked up.
    """
    state_dir = Path(
        os.environ.get("BTY_PATHS_STATE_DIR")
        or os.environ.get("BTY_STATE_DIR")  # legacy alias
        or DEFAULT_STATE_DIR
    )
    candidates: list[Path] = [
        DEFAULT_SYSTEM_CONF_DIR,
        DEFAULT_SYSTEM_CONFIG_FILE,
    ]
    dpath = (os.environ.get("BTY_CONFIG_DIR") or "").strip()
    if dpath:
        candidates.append(Path(dpath))
    fpath = (os.environ.get("BTY_CONFIG_FILE") or "").strip()
    if fpath:
        candidates.append(Path(fpath))
    candidates.append(state_dir / "bty.toml")
    return candidates


# Legacy env-var aliases. v0.41 used these flat names; v0.42 follows
# the ``BTY_<SECTION>_<KEY>`` convention. The alias table lets v0.41
# deploys keep working without re-editing envvars / compose; the
# canonical new name still wins if both are set. Remove after v0.43
# (operators have one release to migrate to bty.toml or the new env
# names).
_LEGACY_ENV_ALIASES: dict[str, tuple[str, str]] = {
    # legacy env name           -> (section, key)
    "BTY_ADMIN_PASSWORD": ("admin", "password"),
    "BTY_STATE_DIR": ("paths", "state_dir"),
    "BTY_BOOT_DIR": ("paths", "boot_dir"),
    "BTY_BACKUP_DIR": ("paths", "backup_dir"),
    "BTY_CATALOG_FILE": ("paths", "catalog_file"),
    "BTY_WEB_HOST": ("server", "host"),
    "BTY_WEB_PORT": ("server", "port"),
    "BTY_TRUSTED_PROXY": ("server", "trusted_proxy"),
    "BTY_SESSION_SECRET": ("server", "session_secret"),
    "BTY_WITHCACHE_URL": ("withcache", "url"),
    "BTY_TFTP_PROBE_HOST": ("netboot", "tftp_probe_host"),
    "BTY_MAX_UPLOAD_BYTES": ("tuning", "max_upload_bytes"),
    "BTY_BACKUP_MAX_PARALLEL": ("tuning", "backup_max_parallel"),
}


def _apply_env_overrides(data: dict[str, Any], sources: dict[str, str]) -> None:
    """Walk the Config schema and overlay any matching env-var
    overrides onto ``data``. A field ``[section] key`` is overridden
    by env var ``BTY_<SECTION>_<KEY>``.

    Types are coerced from the env's string to the dataclass's
    declared type (``int`` / ``str``; ``bool`` would need richer
    parsing but no boolean fields exist today). A bad coerce raises
    ``ValueError`` -- a typo'd integer fails loud at startup rather
    than silently dropping the override.

    Every applied override stamps ``sources[<section.key>] =
    "env(<NAME>)"`` so the Settings UI can mark the field read-only
    + surface the env var name responsible.

    Legacy v0.41 env names (see :data:`_LEGACY_ENV_ALIASES`) are
    also honoured -- the canonical new name wins when both are set,
    so a deploy with both BTY_STATE_DIR (legacy) and
    BTY_PATHS_STATE_DIR (new) takes the new value.
    """
    section_types = get_type_hints(Config)
    # Legacy aliases first; the canonical pass below overrides if
    # both are set.
    for legacy_name, (section_name, field_name) in _LEGACY_ENV_ALIASES.items():
        raw = os.environ.get(legacy_name)
        if raw is None or raw == "":
            continue
        section_cls = section_types[section_name]
        fld_type = next(f.type for f in fields(section_cls) if f.name == field_name)
        section_data = data.setdefault(section_name, {})
        section_data[field_name] = _coerce(raw, fld_type)
        sources[f"{section_name}.{field_name}"] = f"env({legacy_name})"
    # Canonical BTY_<SECTION>_<KEY> overrides.
    for section_name, section_cls in section_types.items():
        section_data = data.setdefault(section_name, {})
        for fld in fields(section_cls):
            env_key = f"BTY_{section_name.upper()}_{fld.name.upper()}"
            raw = os.environ.get(env_key)
            if raw is None or raw == "":
                continue
            section_data[fld.name] = _coerce(raw, fld.type)
            sources[f"{section_name}.{fld.name}"] = f"env({env_key})"


def _coerce(raw: str, declared: Any) -> Any:
    """Coerce a string env value to the field's declared type.

    Only ``str`` / ``int`` are supported -- the Config schema doesn't
    use floats / bools / lists today. A broader type system can be
    added when the schema grows."""
    # ``declared`` may be a typing string ("str") or a real type
    # depending on how the dataclass was authored (``from __future__
    # import annotations`` turns them all into strings). Handle both.
    if declared is str or declared == "str":
        return raw
    if declared is int or declared == "int":
        return int(raw)
    return raw


def _instantiate(data: dict[str, Any]) -> Config:
    """Build a Config from a per-section dict, dropping any unknown
    keys. Unknown keys are silently ignored (forwards-compat: an
    older bty-web reading a newer bty.toml shouldn't crash on a key
    it doesn't know yet)."""

    def _build(section_name: str, section_cls: type) -> Any:
        section_data = data.get(section_name, {}) or {}
        valid_keys = {f.name for f in fields(section_cls)}
        return section_cls(**{k: v for k, v in section_data.items() if k in valid_keys})

    section_types = get_type_hints(Config)
    return Config(
        **{name: _build(name, cls) for name, cls in section_types.items()},
    )


def _seed_defaults(sources: dict[str, str]) -> None:
    """Pre-stamp every Config key with provenance ``"default"`` so a
    key that no TOML or env sets still has a source entry. The
    Settings UI uses the dict as the source-of-truth for whether to
    render a row as editable / read-only, so missing entries would
    skip rows that ARE editable (just at their built-in default)."""
    section_types = get_type_hints(Config)
    for section_name, section_cls in section_types.items():
        for fld in fields(section_cls):
            sources[f"{section_name}.{fld.name}"] = "default"


def load_config(paths: list[Path] | None = None) -> LoadedConfig:
    """Build the live :class:`LoadedConfig` (config + provenance).

    ``paths`` -- explicit list of files / directories, in increasing
    priority order. ``None`` (the default) uses
    :func:`_default_search_paths`. Each path can be either a single
    ``.toml`` file or a directory of drop-ins (loaded in
    lexicographic order). Missing paths are silently skipped.

    Env vars override per-key on top of the merged TOML data.

    No I/O side effects beyond reading the TOML files; the caller is
    responsible for creating directories / persisting the
    session-secret / etc.
    """
    candidates = paths if paths is not None else _default_search_paths()
    expanded = _expand_paths(candidates)

    sources: dict[str, str] = {}
    _seed_defaults(sources)

    merged: dict[str, Any] = {}
    for p in expanded:
        _merge_into(merged, _load_toml_file(p), f"toml({p})", sources)

    _apply_env_overrides(merged, sources)

    # "Primary" TOML is the file Settings writes operator edits back
    # to: the LAST single-file path the operator passed (the
    # rightmost / highest-priority TOML), since that's where they
    # already expect their config to live. Drop-in directories aren't
    # eligible -- writing into a glob is unsafe (which file should it
    # land in?). When no single-file path is found, leave primary
    # None; the UI surfaces "no writable config file".
    primary = _pick_primary_toml(candidates)

    return LoadedConfig(
        cfg=_instantiate(merged),
        sources=sources,
        loaded_files=expanded,
        primary_toml=primary,
    )


def _pick_primary_toml(candidates: list[Path]) -> Path | None:
    """Return the file Settings should write operator edits to.

    Walks the candidate list (the operator's --config order, OR the
    default search list) from highest to lowest priority, picks the
    first single-file entry that's a file OR doesn't exist yet but
    sits in a writable directory. Drop-in directories are skipped.
    """
    for p in reversed(candidates):
        if p.is_dir():
            continue
        if p.is_file():
            return p
        # File doesn't exist yet: writable iff its parent does.
        parent = p.parent
        if parent.is_dir() and os.access(parent, os.W_OK):
            return p
    return None


# ---- Process-wide active config (set once at startup) -----------------------
#
# bty-web has ONE Config per process; threading it through every call
# site that used to do ``os.environ.get("BTY_*")`` would touch dozens
# of signatures. The singleton pattern (set once in ``bty.web.__init__.
# main()``, read everywhere else) keeps the cutover surgical. Tests
# inject via ``set_active_config`` in a fixture.
#
# Reading ``active_config()`` BEFORE ``set_active_config()`` raises --
# rather than silently returning a default-only Config that doesn't
# match the operator's bty.toml, the call site must be ordered after
# startup. The exception message names the call site responsible.

_ACTIVE: LoadedConfig | None = None


def set_active_config(loaded: LoadedConfig) -> None:
    """Install ``loaded`` as the process-wide active config. Called
    exactly once from ``bty.web.__init__.main()`` after CLI / env /
    TOML have been resolved; tests call it from fixtures to inject a
    bespoke config."""
    global _ACTIVE
    _ACTIVE = loaded


def active_config() -> LoadedConfig:
    """Return the process-wide active config. Raises ``RuntimeError``
    if called before :func:`set_active_config` -- the explicit failure
    is loud, while a silent default would be a subtle "operator's
    config is being ignored" bug."""
    if _ACTIVE is None:
        raise RuntimeError(
            "bty.web._config.active_config() called before set_active_config(); "
            "ensure bty.web.__init__.main() ran (or, in a test, install a "
            "Config via set_active_config in a fixture)"
        )
    return _ACTIVE


def cfg() -> Config:
    """Shorthand for ``active_config().cfg`` -- the common-case
    accessor when a caller doesn't also need provenance / loaded
    files."""
    return active_config().cfg


def save_value(path: Path, section: str, key: str, value: str | int) -> None:
    """Persist a single ``[section] key = value`` into ``path``.

    The write preserves the operator's existing comments + ordering
    when ``tomlkit`` is available (the Settings page round-trips
    through this on every edit). On a fresh file the call seeds a
    minimal TOML body. Atomic via tempfile + rename so a crash
    mid-write either keeps the OLD file or none, never a truncated
    one.

    Type coercion: ``value`` is taken as-is; the caller is
    responsible for matching the Config schema's declared type (e.g.
    the form handler parses ``port`` as ``int`` before calling here).
    """
    # tomlkit is the conventional choice for round-trip TOML editing;
    # stdlib ``tomllib`` is read-only. Import lazily so a bty-web that
    # never touches the config-writer (read-only deploys, k8s with
    # env-only overrides) doesn't pay the import.
    import tomlkit

    if path.is_file():
        with path.open("r", encoding="utf-8") as f:
            doc = tomlkit.parse(f.read())
    else:
        doc = tomlkit.document()
        # Header so a freshly-created file isn't anonymous bytes.
        doc.add(tomlkit.comment("Written by bty-web Settings. Edit freely;"))
        doc.add(tomlkit.comment("bty-web re-reads on each restart."))

    if section not in doc:
        doc[section] = tomlkit.table()
    doc[section][key] = value

    # Atomic write: same-dir tempfile + rename. A crash mid-write
    # leaves either the OLD file (if any) or the absence of one;
    # never a truncated file the loader would choke on. Mode 0640
    # since these values include the admin password.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
    os.chmod(tmp, 0o640)
    tmp.replace(path)


def detect_host_addr() -> str:
    """Best-effort LAN IP detection -- moved here from ``deploy.py``
    so both the deploy-time defaults and the runtime config helpers
    can share one implementation.

    UDP-connects to a TEST-NET-2 address (no packet sent on
    UDP-connect; the kernel just chooses an outbound interface) and
    reads the local socket address. Returns whichever IP the host
    would actually use for outbound traffic -- almost always the LAN
    address bty-web should advertise. Falls back to ``127.0.0.1``
    when no route is available; callers may want to surface that.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("198.51.100.1", 80))
        addr: str = s.getsockname()[0]
        return addr
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()
