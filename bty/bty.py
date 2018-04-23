#!/usr/bin/env python
from __future__ import print_function
from subprocess import Popen, PIPE
import pprint
import json
import copy
import re
import os

REGEX_HWA = r".*(([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})).*"

DEFAULT_HOST = {
    "hwa": None,
    "img": None,
    "hostname": None,
    "managed": False,
    "pxe_default": None
}

DEFAULT = {
    "bty": {
        "root": "/srv/bty",
    },
    "pxe": {
        "root": "/srv/tftpboot/pxelinux.cfg",
    },
    "img": {
        "root": "/srv/images"
    },
    "sys": {
        "usr": "nvm",
        "grp": 'CNEXLABS\domain^users'
    },
    "web": {
        "usr": "www-data",
        "grp": "www-data"
    },
    "machines": {}
}

CFG_FNAME = "bty.json"

PXE = {
    "cfg_path": "/srv/tftpboot/pxelinux.cfg",
    "labels": [
        "boot_hda",
        "boot_hda_bzi",
        "install"
    ],
}

def ipa_to_hwa(ipa=None):
    """
    Resolve the given IP address to HW address

    @returns hwaddr on the form AA:BB:CC:11:22:33 on success, None otherwise
    """

    if ipa is None:
        print("FAILED: ip: %r" % ipa)
        return None

    cmd = ["arp", "-a", ipa]

    proc = Popen(cmd, stdout=PIPE, stderr=PIPE)

    out, _ = proc.communicate()
    out = out.lower() if out else ""

    match = re.match(REGEX_HWA, out)
    if match:
            return match.group(1).upper()

    print("FAILED: out: %r" % out)

    return None

def hdrs(content=None, content_type=None):
    """Return headers for the given content"""

    if content is None:
        content = b""

    if content_type is None:
        content_type = "text/plain"

    return  [
        ('Content-type', content_type),
        ('Content-Length', str(len(content)))
    ]

def cfg_load(environ):
    """
    Load config from os.path.dirname("SCRIPT_FILENAME")/bty.json

    @returns config as dict on success, None otherwise
    """

    cfg = None

    script_filename = environ.get("SCRIPT_FILENAME")
    if script_filename is None:
        return cfg

    cfg_path = os.sep.join([os.path.dirname(script_filename), CFG_FNAME])
    if not os.path.exists(cfg_path):
        return cfg

    with open(cfg_path, "r") as cfg_fd:
        cfg = json.load(cfg_fd)

    return cfg

def cfg_save(environ, cfg):
    """
    Store the given cfg

    @returns True on success, False otherwise
    """

    script_filename = environ.get("SCRIPT_FILENAME")
    if script_filename is None:
        return False

    cfg_path = os.sep.join([os.path.dirname(script_filename), CFG_FNAME])
    if not os.path.exists(cfg_path):
        return False

    with open(cfg_path, "w") as cfg_fd:
        cfg = json.dump(cfg, cfg_fd, sort_keys=True, indent=2)

    return True

def bootstrap(environ, cfg, host):
    """@returns bootstrap script for the host to run"""

    print("bootstrap")

    script_filename = environ.get("SCRIPT_FILENAME")
    tmpl_path = os.sep.join([
        os.path.dirname(script_filename),
        "install.tmpl" if host["managed"] else "reboot.tmpl"
    ])

    tmpl = ""
    with open(tmpl_path, "r") as tmpl_fd:
        tmpl = tmpl_fd.read()

    for key, val in host.items():
        if val is None:
            continue

        placeholder = "___%s___" % key.upper()
        tmpl = tmpl.replace(placeholder, str(val))

    return tmpl

def pxe_config(environ, cfg, host):
    """@returns PXE config for the given host on success, None otherwise"""

    print("pxe_config")

    if not host["managed"]:
        host["hostname"] = "unmanaged"
        return None

    if None in [host["hostname"], host["hwa"]]:
        print("ERR: invalid host: %r" % host)
        return None

    # TODO: make this configurable
    host["PXE_DEFAULT"] = "boot_hd0"

    script_filename = environ.get("SCRIPT_FILENAME")
    tmpl_path = os.sep.join([
        os.path.dirname(script_filename),
        "pxeconfig.tmpl"
    ])

    tmpl = ""
    with open(tmpl_path, "r") as tmpl_fd:
        tmpl = tmpl_fd.read()

    for key, val in host.items():
        if val is None:
            continue

        placeholder = "___%s___" % key.upper()
        tmpl = tmpl.replace(placeholder, str(val))

    return tmpl

def pxe_config_install(environ, cfg, host, pxe):
    """Install the given pxe config"""

    print("pxe_config_install")

    pxe_fname = "01-%s" % host["hwa"].replace(":", "-")
    pxe_fname = pxe_fname.lower()
    pxe_fpath = os.sep.join([PXE["cfg_path"], pxe_fname])

    print("pxe_fpath: %r" % pxe_fpath)

    with open(pxe_fpath, "w") as pxe_fd:
        pxe_fd.write(pxe)

def wildcard(environ, cfg):
    """Wildcard..."""

    print("wildcard")

    content = ""
    content += "ENVIRON: %s" % pprint.pformat(environ)
    content += "\n\n"
    content += "CONFIG: %s" % pprint.pformat(cfg)

    return content

def manage(environ, cfg):
    """Do some management"""

    return ""

def application(environ, start_response):
    """Application generating and modifying PXE configurations"""

    print("ENTER!")

    path_info = environ.get("PATH_INFO")
    if path_info is None:
        print("FAILED: invalid path_info: %r" % path_info)
        content = pprint.pformat(environ)
        start_response("404 NOT FOUND", hdrs(content))
        return [content]

    hwa = ipa_to_hwa(environ.get("REMOTE_ADDR"))    # Get HWA and set in env
    if hwa is None:
        print("FAILED: invalid hwa: %r" % hwa)
        start_response("404 NOT FOUND", hdrs())
        return []

    environ["REMOTE_HWA"] = hwa

    cfg = cfg_load(environ)             # Load config

    if hwa not in cfg:              # Add entry in config
        cfg[hwa] = copy.deepcopy(DEFAULT_HOST)
        cfg[hwa]["hwa"] = hwa

        cfg_save(environ, cfg)

    host = cfg[hwa]

    print("host: %r" % host)

    if "bootstrap" in path_info:          # Dispatch
        content = bootstrap(environ, cfg, host)

        if host["managed"]:         # Create PXE config for host
            pxe = pxe_config(environ, cfg, host)
            pxe_config_install(environ, cfg, host, pxe)

    elif "manage" in path_info:
        content = manage(environ, cfg)
    else:
        content = wildcard(environ, cfg)

    encoded = content.encode()

    start_response("200 OK", hdrs(encoded))

    return [encoded]
