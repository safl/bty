#!/usr/bin/env python
from __future__ import print_function
from subprocess import Popen, PIPE
import pprint
import json
import copy
import re
import os
import bty

CFG_FPATH="/tmp/bty.json"

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

    match = re.match(bty.REGEX_HWA, out)
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

def cfg_load(cfg_fpath):
    """
    Load config from os.path.dirname("SCRIPT_FILENAME")/bty.json

    @returns config as dict on success, None otherwise
    """

    if not os.path.exists(cfg_fpath):
        print("FAILED: !exist(cfg_fpath: %r)" % cfg_fpath)
        return None

    with open(cfg_fpath, "r") as cfg_fd:
        return json.load(cfg_fd)

    return None

def cfg_save(cfg_fpath, cfg):
    """
    Store the given cfg

    @returns True on success, False otherwise
    """

    with open(cfg_fpath, "w") as cfg_fd:
        cfg = json.dump(cfg, cfg_fd, sort_keys=True, indent=2)

    return True

def pxe_config(environ, cfg, host):
    """@returns PXE config for the given host on success, None otherwise"""

    print("pxe_config")

    if not host["managed"]:
        host["hostname"] = "unmanaged"
        return None

    if None in [host["hostname"], host["hwa"]]:
        print("ERR: invalid host: %r" % host)
        return None

    host["PXE_DEFAULT"] = "boot_hd0"            # TODO: make this configurable

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
    pxe_fpath = os.sep.join([PXE["cfg_fpath"], pxe_fname])

    print("pxe_fpath: %r" % pxe_fpath)

    with open(pxe_fpath, "w") as pxe_fd:
        pxe_fd.write(pxe)

def app_wildcard(state):
    """Wildcard..."""

    print("# WILDCARD")

    return "state: %r" % state

def app_manage(state):
    """Do some management"""

    return ""

def app_bootstrap(environ, cfg, host):
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

    if host["managed"]:             # Create PXE config for host
        pxe = pxe_config(environ, cfg, host)
        pxe_config_install(environ, cfg, host, pxe)

    return tmpl

def app(state):
    """Application generating and modifying PXE configurations"""

    path_info = state["env"].get("PATH_INFO")

    if "bootstrap" in path_info:
        return app_bootstrap(state)
    elif "manage" in path_info:
        return app_manage(state)
    else:
        return app_wildcard(state)

def state_init(environ):
    """
    Initialized application state, load config, parse URI etc.

    @Returns state on success, None otherwise
    """

    state = {
        "env": environ,
        "cfg": None,
    }

    state["cfg"] = cfg_load(CFG_FPATH)
    if state["cfg"] is None:
        print("FAILED: init, could not load config")
        return None

    if state["env"].get("PATH_INFO") is None:
        print("FAILED: init, no PATH_INFO in environment")
        return None

    hwa = ipa_to_hwa(state["env"].get("REMOTE_ADDR"))   # Get HWA
    if hwa is None:
        print("FAILED: invalid hwa: %r" % hwa)
        return None

    state["env"]["REMOTE_HWA"] = hwa

    if hwa not in state["cfg"]["machines"]:             # Add host to CFG
        state["cfg"][hwa] = copy.deepcopy(DEFAULT_HOST)
        state["cfg"][hwa]["hwa"] = hwa

        cfg_save(CFG_FPATH, cfg)                        # Persist CFG changes

    return state

def application(environ, start_response):
    """WSGI entry point"""

    msg = "404 Not Found"
    encoded = b""

    state = state_init(environ)
    if state:
        msg = "200 OK"
        encoded = app(state).encode()
    else:
        print("FAILED: environ: %r" % environ)

    start_response(msg, hdrs(encoded))

    return [encoded]
