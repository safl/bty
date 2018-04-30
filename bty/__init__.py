from __future__ import print_function
import pprint
import json
import copy
import time
import glob
import re
import os
import bty
from subprocess import Popen, PIPE
from flask import Flask, render_template, request
from flask import config as fconfig

REGEX_HWA = r".*(([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})).*"
REGEX_LBL = r"^LABEL\s+(.*)$"

MACHINE_ATTRS = [
    "hostname",
    "hwa",
    "image",
    "managed",
    "plabel"
    "ptemplate",
]
MACHINE_STRUCT = { attr: None for attr in MACHINE_ATTRS }

PCONFIG_ATTRS = [
    ("fname", str),
    ("content", str)
]
PCONFIG_STRUCT = { a: c() for a, c in PCONFIG_ATTRS }

PTEMPLATE_ATTRS = [
    ("fname", str),
    ("content", str),
    ("labels", list)
]
PTEMPLATE_STRUCT = { a: c() for a, c in PTEMPLATE_ATTRS }

CFG_FNAME = "bty.json"
CFG_FPATH = "/tmp/%s" % CFG_FNAME
CFG_DEFAULT = {
    "pconfigs": {
        "coll": {},
        "root": "/srv/tftpboot/pxelinux.cfg",
    },

    "ptemplates": {
        "coll": {},
        "root": "/srv/bty/bty/templates",
        "exts": ["cfg"],
        "default": "pxe-c115200.cfg",
        "default_skip": "pxe-skip.cfg",
    },

    "images": {
        "coll": [],
        "root": "/srv/images",
        "default": None,
        "exts": ["qcow2"]
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

def pxe_config(cfg, host):
    """Returns the pxe-config for the given host"""

    tmpl = host.get("ptemplate")
    if tmpl is None:
        print("FAILED: host: %r, tmpl: %r" % (host, tmpl))
        return None

    return render_template(tmpl, cfg=cfg, host=host)

def pxe_config_install(cfg, host, pxe_content, pxe_fname=None):
    """Install the given pxe config"""

    print("pxe_config_install")

    if pxe_fname is None:
        pxe_fname = "01-%s" % host["hwa"].replace(":", "-")
        pxe_fname = pxe_fname.lower()

    pxe_fpath = os.sep.join([cfg["pconfigs"]["root"], pxe_fname])

    print("pxe_fpath: %r" % pxe_fpath)

    with open(pxe_fpath, "w") as pxe_fd:
        pxe_fd.write(pxe_content)

def cfg_init_pconfigs(cfg):
    """
    Initialize PXE configs by scanning system
    """

    fnames = []

    cfg["pconfigs"]["coll"] = {}
    for fpath in glob.glob(os.sep.join([cfg["pconfigs"]["root"], "*"])):
        fname = os.path.basename(fpath)
        fnames.append(fname)

        pcfg = copy.deepcopy(PCONFIG_STRUCT)
        pcfg["fname"] = fname
        pcfg["content"] = open(fpath).read()

        cfg["pconfigs"]["coll"][fname] = pcfg

    if "default" in fnames:     # There is a default config, we can exit
        return

    print("WARNING: PXE default config is missing, creating one")
    host = copy.deepcopy(MACHINE_STRUCT)
    host["hostname"] = "default"
    host["image"] = cfg["images"]["default"]
    host["ptemplate"] = cfg["ptemplates"]["default"]
    host["plabel"] = "install"

    pxe = pxe_config(cfg, host)
    if pxe is None:
        print("FAILED: pxe_config for host: %r" % host)
        return

    pxe_config_install(cfg, host, pxe, "default")

def cfg_init_ptemplates(cfg, app):
    """
    Initialize PXE configs by scanning system
    """

    def annotate_labels(tmpl):
        """Returns a list of LABELs from the given TMPL source"""

        tmpl["labels"] = []
        for line in tmpl["content"].splitlines():
            match = re.match(bty.REGEX_LBL, line)
            if match:
                tmpl["labels"].append(match.group(1))

        tmpl["labels"].sort()

    jenv = app.jinja_env
    lodr = jenv.loader

    cfg["ptemplates"]["coll"] = {}
    for fname in jenv.list_templates(extensions=cfg["ptemplates"]["exts"]):
        fname = str(fname)
        tmpl = copy.deepcopy(PTEMPLATE_STRUCT)

        tmpl["fname"] = fname
        tmpl["content"], fpath, _ = lodr.get_source(app.jinja_env, fname)
        # TODO: parse LABELS

        annotate_labels(tmpl)

        cfg["ptemplates"]["coll"][fname] = tmpl

        if not cfg["ptemplates"]["default"] and "skip" not in fname:
            cfg["ptemplates"]["default"] = fname

        if not cfg["ptemplates"]["default_skip"] and "skip" in fname:
            cfg["ptemplates"]["default_skip"] = fname

def cfg_init_images(cfg):
    """
    Initialize images in configuration
    """

    cfg["images"]["coll"] = []
    for ext in cfg["images"]["exts"]:
        for fpath in glob.glob(os.sep.join([
            cfg["images"]["root"], "*.%s" % ext]
        )):
            fname = os.path.basename(fpath)
            cfg["images"]["coll"].append(fname)

    if cfg["images"]["coll"]:
        cfg["images"]["coll"].sort()
        cfg["images"]["default"] = cfg["images"]["coll"][0]


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

def cfg_init(cfg_fpath, app):
    """
    Load config from `cfg_fpath`, use default if it does not exist
    """

    if app is None:
        print("FAILED: app: %r" % app)
        return None

    cfg = cfg_load(CFG_FPATH)
    if cfg:
        return cfg

    print("FAILED: cfg_load(%r)" % CFG_FPATH)

    # TODO: set some default paths...
    # TODO: ... like set the ptemplate["root"] to jinjas search dir

    print("WARNING: initializing a best-effort configuration...")
    cfg = copy.deepcopy(CFG_DEFAULT)
    cfg_init_images(cfg)
    cfg_init_ptemplates(cfg, app)
    cfg_init_pconfigs(cfg)

    pprint.pprint(cfg)

    #if not cfg_save(CFG_FPATH, cfg):
    #    print("FAILED: configuration seems severely broken")

    return cfg

APP = Flask(__name__)
#APP.config.from_object('websiteconfig')
if APP is None:
    print("FAILED: cannot instantiate APP")
    exit(1)

CFG = None
with APP.app_context():
    CFG = cfg_init(CFG_FPATH, APP)
    if CFG is None:
        print("FAILED: cannot obtain a configuration")
        exit(1)

@APP.route("/jazz")
def app_slow():
    """This is the jazzy part"""

    #help(app.jinja_env)

    #print()

    return ""

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

@APP.route("/cfg")
def app_cfg():
    """Provide config as JSON"""

    return json.dumps(CFG)

@APP.route("/", methods=["GET", "POST"])
def app_cfg_ui():
    """Render UI"""

    if request.method == "POST":

        print(pprint.pformat(request.form))

        action = request.form.get("action")
        if action == "refresh" and "bulk_ident" in request.form:
            bulk_refresh(dict(request.form))
        elif action == "remove" and "bulk_ident" in request.form:
            bulk_remove(dict(request.form))
        else:
            print("Process SINGLE update change")

    return render_template('ui_cfg.html', cfg=CFG)

@APP.route("/pxe", methods=["GET"])
@APP.route("/pxe/<hwa>", methods=["GET"])
def app_pxe(hwa=None):
    """sdfsd"""

    host = hwa_to_host(hwa)
    if host is None:
        print("FAILED: hwa_to_host, hwa: %r" % hwa)
        return "", 404

    pcfg = pxe_config(CFG, host)
    if pcfg is None:
        print("FAILED: pxe_config" % hwa)
        return "", 404

    return pcfg

@APP.route("/bootstrap.sh")
@APP.route("/bootstrap.sh/<hwa>")
def app_bootstrap(hwa=None):
    """@returns bootstrap script for the host to run"""

    host = hwa_to_host(hwa)
    if host is None:
        print("FAILED: hwa_to_host, hwa: %r" % hwa)
        return "", ""

    host = CFG["machines"]["coll"].get(hwa)
    if host is None:
        print("FAILED: machines: %r, host: %r" % (CFG["machines"], host))
        return "", 404

    if not host["managed"]:
        return render_template("bootstrap_cancel.sh")

    tmpls = CFG["ptemplates"]["coll"]   # Switch to a non-install PXE LABEL
    ptemplate = host["ptemplate"]
    labels = [lbl for lbl in tmpls[ptemplate]["labels"] if "install" not in lbl]
    if labels:
        labels.sort()
        host["plabel"] = labels[0]
        cfg_save(CFG_FPATH, CFG)

    pxe = pxe_config(CFG, host)         # Create PXE config for host
    try:
        pxe_config_install(CFG, host, pxe)
    except IOError as exc:
        print("FAILED: pxe_config_install, err: %r" % exc)

    return render_template("bootstrap.sh", cfg=CFG, host=host)
