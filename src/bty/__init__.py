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
    "plabel",
    "ptemplate"
]
MACHINE_STRUCT = { attr: None for attr in MACHINE_ATTRS }
MACHINE_STRUCT["hostname"] = ""

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
CFG_FPATH = "/srv/bty/cfg/%s" % CFG_FNAME
CFG_DEFAULT = {
    "pconfigs": {
        "coll": {},
        "root": "/srv/tftp/pxelinux.cfg",
    },

    "ptemplates": {
        "coll": {},
        "root": "/srv/bty/bty/templates",
        "exts": ["cfg"],
        "default": None,
        "default_skip": None,
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
        "usr": "odus",
        "grp": "odus"
    },

    "web": {
        "usr": "www-data",
        "grp": "www-data"
    }
}

def machine_create(cfg, **attrs):

    struct = copy.deepcopy(MACHINE_STRUCT)
    for attr, val in attrs.items():
        struct[attr] = val

    if set(struct.keys()) != set(MACHINE_ATTRS):
        print("invalid struct: %r" % struct)
        return None

    if struct["hwa"] is None:
        print("invalid struct: %r" % struct)
        return None

    struct["managed"] = bool(int(struct["managed"]))
    if struct["managed"]:       # Construct a managed machine from given info
        if not struct["image"]:
            struct["image"] = cfg["images"]["default"]
    else:
        tmpl = cfg["ptemplates"]["default_skip"]
        lbls = cfg["ptemplates"]["coll"][tmpl]["labels"]

        struct["managed"] = False
        struct["ptemplate"] = tmpl
        struct["plabel"] = lbls[0] if lbls else ""

    if struct["hostname"] is None:
        struct["hostname"] = ""

    return struct

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

def hwa_lookup(cfg, hwa=None):
    """Guest the machine or die trying"""

    ipa = request.remote_addr
    machine = None

    if hwa is None:
        hwa = ipa_to_hwa(ipa)

    if hwa is None:
        print("FAILED: hwa_lookup hwa: %r" % hwa)
        return hwa, ipa, machine

    machine = cfg["machines"]["coll"].get(hwa)
    if machine is None:
        print("FAILED: hwa_lookup / cfg[...].get")
        return hwa, ipa, machine

    return hwa, ipa, machine

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

def pxe_config_create(cfg, machine):
    """Returns the pxe-config for the given machine"""

    tmpl = machine.get("ptemplate")
    if tmpl is None:
        print("FAILED: machine: %r, tmpl: %r" % (machine, tmpl))
        return None

    return render_template(tmpl, cfg=cfg, machine=machine)

def pxe_config_install(cfg, machine, pxe_content, pxe_fname=None):
    """Install the given pxe config"""

    print("pxe_config_install")

    if pxe_fname is None:
        pxe_fname = "01-%s" % machine["hwa"].replace(":", "-")
        pxe_fname = pxe_fname.lower()

    pxe_fpath = os.sep.join([cfg["pconfigs"]["root"], pxe_fname])

    print("pxe_fpath: %r" % pxe_fpath)

    with open(pxe_fpath, "w") as pxe_fd:
        pxe_fd.write(pxe_content)

def pxe_deploy(cfg, machine, pxe_fname=None):
    """
    Deploy a PXE configuration for the given machine

    @returns True on success, False otherwise
    """

    pxe_config = pxe_config_create(CFG, machine)
    if pxe_config is None:
        print("FAILED: pxe_config_create")
        return False

    try:
        pxe_config_install(cfg, machine, pxe_config, pxe_fname)
    except IOError as exc:
        print("FAILED: pxe_config_install, err: %r" % exc)
        return False

    return True

def cfg_init_default_pxe(cfg):
    """Initialize the default PXE config"""

    # Create the PXE default configuration
    print("WARNING: PXE default config is missing, creating one")
    machine = copy.deepcopy(MACHINE_STRUCT)
    machine["hostname"] = "default"
    machine["image"] = cfg["images"]["default"]
    machine["ptemplate"] = cfg["ptemplates"]["default"]
    machine["plabel"] = "olleh"

    if not pxe_deploy(cfg, machine, "default"):
        print("FAILED: pxe_deploy(...) for machine: %r" % machine)

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

    print("INFO: cfg_fpath: %r" % cfg_fpath)

    try:
        with open(cfg_fpath, "w") as cfg_fd:
            cfg = json.dump(cfg, cfg_fd, sort_keys=True, indent=2)
    except IOError as exc:
        print("FAILED: cfg_save")
        return False

    print("SUCCESS: cfg_save")

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
    cfg_init_default_pxe(cfg)
    cfg_init_pconfigs(cfg)

    if not cfg_save(CFG_FPATH, cfg):
        print("FAILED: configuration seems severely broken")

    return cfg

def cfg_apply_machines(cfg, form):
    """Apply config changes to machines"""

    print("## cfg_apply_machines")

    # MAP 'null' to None in formular
    for attr in sorted(list(set(MACHINE_ATTRS) - set(["managed"]))):
        print("JAZZ: %r" % attr)
        form[attr] = [
            None if val == "null" else val for val in form[attr]
        ]

    changes = False                         # Make changes
    for hwa, managed, hostname, image, plabel, ptemplate in zip(
            form["hwa"], form["managed"], form["hostname"], form["image"],
            form["plabel"], form["ptemplate"]):

        machine = machine_create(cfg,
            hwa=hwa,
            managed=managed,
            hostname=hostname,
            image=image,
            plabel=plabel,
            ptemplate=ptemplate
        )
        if not machine:
            continue

        cfg["machines"]["coll"][hwa] = machine
        changes = True

    if not cfg_save(CFG_FPATH, cfg):        # Persist changes
        print("FAILED: cfg_apply_machines, could not cfg_save(...)")
        return

    for hwa in cfg["machines"]["coll"]:     # (re)write PXE configurations
        machine = cfg["machines"]["coll"][hwa]
        if machine:
            pxe_deploy(cfg, machine)

    cfg_init_pconfigs(cfg)                  # Reload PXE configurations

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

    pxe_default_path = os.sep.join([CFG["pconfigs"]["root"], "default"])
    if not os.path.exists(pxe_default_path):
        cfg_init_default_pxe(CFG)
        cfg_init_pconfigs(CFG)

    if not cfg_save(CFG_FPATH, CFG):
        print("FAILED: configuration seems severely broken")

def bulk_remove(cfg, form):
    """Remove entries"""

    print("## bulk_remove")

    changed = False

    for hwa in form.get("bulk_ident"):
        print("hwa: %r" % hwa)

        if hwa in cfg["machines"]["coll"]:
            del cfg["machines"]["coll"][hwa]
            changed = True

    if changed:
        if cfg_save(CFG_FPATH, cfg):
            print("SUCCESS: bulk_remove")
        else:
            print("FAILED: bulk_remove")

    return

def bulk_refresh(cfg, form):
    """Remove entries"""

    print("## bulk_refresh")

    for hwa in form.get("bulk_ident"):
        print("hwa: %r" % hwa)

        machine = cfg["machines"]["coll"].get(hwa)
        if machine:
            pxe_deploy(cfg, machine)

    return

@APP.route("/bootstrap.sh")
@APP.route("/bootstrap.sh/<hwa>")
def web_bootstrap(hwa=None):
    """
    Creates a bootstrap script and changes machine config

    @returns bootstrap script for the machine to execute
    """

    lookup = hwa, ipa, machine = hwa_lookup(CFG, hwa)
    if hwa is None:
        print("FAILED: web_bootstrap, lookup: %r" % lookup)
        return "", 404

    if machine is None: # Add unknown machine as unmanaged
        ptmpl = CFG["ptemplates"]["default_skip"]

        machine = copy.deepcopy(MACHINE_STRUCT)
        machine["hwa"] = hwa
        machine["managed"] = False
        machine["ptemplate"] = ptmpl
        if ptmpl:
            lbls = CFG["ptemplates"]["coll"][ptmpl]["labels"]
            machine["plabel"] = lbls[0] if lbls else ""

        CFG["machines"]["coll"][hwa] = machine
        cfg_save(CFG_FPATH, CFG)

    if not machine["managed"]:
        return render_template("bootstrap_cancel.sh")

    ptemplate = machine["ptemplate"]

    tmpls = CFG["ptemplates"]["coll"]   # Switch to a non-install PXE LABEL
    labels = [lbl for lbl in tmpls[ptemplate]["labels"] if "install" not in lbl]
    if labels:
        labels.sort()
        machine["plabel"] = labels[0]
        cfg_save(CFG_FPATH, CFG)

    if not pxe_deploy(CFG, machine):
        print("FAILED: pxe_deploy")

    return render_template("bootstrap.sh", cfg=CFG, machine=machine)

@APP.route("/cfg")
def web_raw():
    """Provide config as JSON"""

    return json.dumps(CFG)

@APP.route("/", methods=["GET", "POST"])
def web_ui():
    """Render UI"""

    if request.method == "POST":

        do_cfg_save = True
        action = request.form.get("action")
        if action == "refresh" and "bulk_ident" in request.form:
            bulk_refresh(CFG, dict(request.form))
            do_cfg_save = False
        elif action == "remove" and "bulk_ident" in request.form:
            bulk_remove(CFG, dict(request.form))
        elif action == "pconfigs_refresh":
            cfg_init_pconfigs(CFG)
        elif action == "ptemplates_refresh":
            cfg_init_ptemplates(CFG, APP)
        elif action == "images_refresh":
            cfg_init_images(CFG)
        elif action == "apply":
            cfg_apply_machines(CFG, dict(request.form))
        else:
            print("FAILED: form: %r" % request.form)
            do_cfg_save = False

        if do_cfg_save:
            if not cfg_save(CFG_FPATH, CFG):
                print("FAILED: configuration seems severely broken")

    response = render_template('ui_cfg.html', cfg=CFG)

    return response
