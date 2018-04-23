REGEX_HWA = r".*(([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})).*"

DEFAULT_HOST = {
    "hwa": None,
    "img": None,
    "hostname": None,
    "managed": False,
    "pxe_default": None
}

DEFAULT_CFG = {
    "bty": {
        "root": "/srv/bty",
        "templates": "/srv/bty/templates"
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

