from __future__ import print_function
import pprint
import json
import copy
import time
import re
import os
import bty
from subprocess import Popen, PIPE
from flask import Flask, render_template, request

REGEX_HWA = r".*(([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})).*"

MACHINE_ATTRS = [
    "hostname",
    "hwa",
    "image",
    "managed",
    "plabel"
    "ptemplate",
]
MACHINE_STRUCT = { attr: None for attr in MACHINE_ATTRS }

CFG_FNAME = "bty.json"
CFG_FPATH = "/tmp/%s" % CFG_FNAME
CFG_DEFAULT = {
    "pconfigs": {
        "coll": [],
        "root": "/srv/tftpboot/pxelinux.cfg",
    },

    "ptemplates": {
        "coll": {
            "pxe-c115200.cfg": {
                "fname": "pxe-c115200.cfg",
                "labels": ["boot_hd0", "boot_hd0_bzi", "install"]
            }
        },
        "root": "/srv/bty/bty/templates",
        "default": "pxe-c115200.cfg",
        "default_skip": "pxe-skip.cfg",
    },

    "images": {
        "coll": [],
        "root": "/srv/images",
        "default": None,
    },

    "machines": {
        "coll": {}
    },

    "sys": {
        "usr": "nvm",
        "grp": 'CNEXLABS\domain^users'
    },

    "web": {
        "usr": "www-data",
        "grp": "www-data"
    }
}

def machine(cfg, **attrs):

    struct = copy.deepcopy(MACHINE_STRUCT)
    for attr, val in attrs.items():
        struct[attr] = val

    if set(struct.keys()) != set(MACHINE_ATTRS):
        print("invalid struct: %r" % struct)
        return None

    if struct["hwa"] is None:
        print("invalid struct: %r" % struct)
        return None

    if struct["managed"]:       # Construct a managed machine from given info
        struct["managed"] = True
        if not struct["image"]:
            struct["image"] = cfg["images"]["default"]

    else:
        struct["managed"] = False
        struct["image"] = cfg["images"]["default_skip"]

    return machine

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

def hwa_to_host(hwa=None):
    """Guest the host or die trying"""

    if hwa is None:
        hwa = ipa_to_hwa(request.remote_addr)

    if hwa is None:
        print("FAILED: hwa: %r")
        return None

    host = cfg["machines"]["coll"].get(hwa)
    if host is None:
        print("FAILED: machines: %r, host: %r" % (cfg["machines"], host))
        return None

    return host

def hdrs(content=None, content_type=None):
    """Return headers for the given content"""

    if content is None:
        content = b""

    if content_type is None:
        content_type = "text/html"

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

def cfg_init(cfg_fpath):
    """
    Load config from `cfg_fpath`, use default if it does not exist
    """

    cfg = cfg_load(CFG_FPATH)
    if cfg is None:
        print("FAILED: loading configuration")
        print("WARNING: using default config")
        cfg = copy.deepcopy(CFG_DEFAULT)

        if not cfg_save(CFG_FPATH, cfg):
            print("FAILED: configuration seems severely broken")

    return cfg

def cfg_save(cfg_fpath, cfg):
    """
    Store the given cfg

    @returns True on success, False otherwise
    """

    try:
        with open(cfg_fpath, "w") as cfg_fd:
            cfg = json.dump(cfg, cfg_fd, sort_keys=True, indent=2)
    except IOError as exc:
        print("FAILED: persisting cfg")
        return False

    return True


def pxe_config(cfg, host):
    """Returns the pxe-config for the given host"""

    tmpl = host.get("ptemplate")
    if tmpl is None:
        print("FAILED: host: %r, tmpl: %r" % (host, tmpl))
        return None

    return render_template(tmpl, cfg=cfg, host=host)

def pxe_config_install(cfg, host, pxe):
    """Install the given pxe config"""

    print("pxe_config_install")

    pxe_fname = "01-%s" % host["hwa"].replace(":", "-")
    pxe_fname = pxe_fname.lower()
    pxe_fpath = os.sep.join([cfg["pconfigs"]["root"], pxe_fname])

    print("pxe_fpath: %r" % pxe_fpath)

    with open(pxe_fpath, "w") as pxe_fd:
        pxe_fd.write(pxe)

app = Flask(__name__)
#app.config.from_object('websiteconfig')

cfg = cfg_init(CFG_FPATH)
if cfg is None:
    print("FAILED: cannot obtain a configuration")
    exit(1)

@app.route("/slow")
def app_slow():
    """Render configuration"""

    time.sleep(5)

    return render_template('ui_cfg.html', cfg=cfg)

def bulk_remove(form):
    """Remove entries"""

    print("# TODO: Process bulk remove")
    for hwa in form.get("bulk_ident"):
        print("hwa: %r" % hwa)

    return

def bulk_refresh(form):
    """Remove entries"""

    print("# TODO: Process bulk refresh")
    for hwa in form.get("bulk_ident"):
        print("hwa: %r" % hwa)

    return

@app.route("/cfg")
def app_cfg():
    """Provide config as JSON"""

    return json.dumps(cfg)

@app.route("/", methods=["GET", "POST"])
def app_cfg_ui():
    """sdfsd"""

    if request.method == "POST":

        print(pprint.pformat(request.form))

        action = request.form.get("action")
        if action == "refresh" and "bulk_ident" in request.form:
            bulk_refresh(dict(request.form))
        elif action == "remove" and "bulk_ident" in request.form:
            bulk_remove(dict(request.form))
        else:
            print("Process SINGLE update change")

    return render_template('ui_cfg.html', cfg=cfg)

@app.route("/pxe", methods=["GET"])
@app.route("/pxe/<hwa>", methods=["GET"])
def app_pxe(hwa=None):
    """sdfsd"""

    host = hwa_to_host(hwa)
    if host is None:
        print("FAILED: hwa_to_host, hwa: %r" % hwa)
        return "", 404

    pcfg = pxe_config(cfg, host)
    if pcfg is None:
        print("FAILED: pxe_config" % hwa)
        return "", 404

    return pcfg

@app.route("/bootstrap.sh")
@app.route("/bootstrap.sh/<hwa>")
def app_bootstrap(hwa=None):
    """@returns bootstrap script for the host to run"""

    host = hwa_to_host(hwa)
    if host is None:
        print("FAILED: hwa_to_host, hwa: %r" % hwa)
        return "", ""

    host = cfg["machines"]["coll"].get(hwa)
    if host is None:
        print("FAILED: machines: %r, host: %r" % (cfg["machines"], host))
        return "", 404

    if not host["managed"]:
        return render_template("bootstrap_cancel.sh")

    tmpls = cfg["ptemplates"]["coll"]   # Switch to a non-install PXE LABEL
    ptemplate = host["ptemplate"]
    labels = [lbl for lbl in tmpls[ptemplate]["labels"] if "install" not in lbl]
    if labels:
        labels.sort()
        host["plabel"] = labels[0]
        cfg_save(CFG_FPATH, cfg)

    pxe = pxe_config(cfg, host)         # Create PXE config for host
    try:
        pxe_config_install(cfg, host, pxe)
    except IOError as exc:
        print("FAILED: pxe_config_install, err: %r" % exc)

    return render_template("bootstrap.sh", cfg=cfg, host=host)
