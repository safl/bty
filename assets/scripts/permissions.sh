#!/usr/bin/env bash
if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root" 1>&2
   exit 1
fi

TFTPBOOT_ROOT='/srv/tftpboot'
IMAGES_ROOT='/srv/images'
BTY_ROOT='/srv/bty'

SYS_USR='nvm'
SYS_GRP='CNEXLABS\domain^users'

WEB_USR='www-data'
WEB_GRP='www-data'

# Set these up so the '$SYS_USR' can administer them
chown -R "$SYS_USR":"$SYS_GRP" $TFTPBOOT_ROOT
chown -R "$SYS_USR":"$SYS_GRP" $BTY_ROOT

# Allow anonymous access via NFS
chown -R nobody:nogroup $IMAGES_ROOT
chown -R nobody:nogroup $TFTPBOOT_ROOT/cilla

# Change group such that administrators can write/overwrite
sudo chgrp -R "$SYS_GRP" $TFTPBOOT_ROOT/pxelinux.bzi
sudo chmod g+rwx -R $TFTPBOOT_ROOT/pxelinux.bzi
sudo setfacl -d -m group:"$SYS_GRP":rwx $TFTPBOOT_ROOT/pxelinux.bzi

# Change such that Apache can modify pxe-configs and bty-config
chown -R "$WEB_USR":"$WEB_GRP" $BTY_ROOT
chown -R "$WEB_USR":"$WEB_GRP" $TFTPBOOT_ROOT/pxelinux.cfg
