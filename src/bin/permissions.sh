#!/usr/bin/env bash
if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root" 1>&2
   exit 1
fi

BTY_ROOT='/srv/bty'
FTP_ROOT='/srv/tftp'
IMG_ROOT='/srv/images'

SYS_USR='odus'
SYS_GRP='odus'

WEB_USR='www-data'
WEB_GRP='www-data'

# Set these up so the '$SYS_USR' can administer them
chown -R "$SYS_USR":"$SYS_GRP" $FTP_ROOT
chown -R "$SYS_USR":"$SYS_GRP" $BTY_ROOT

# Allow anonymous access via NFS
chown -R nobody:nogroup $IMG_ROOT
chown -R nobody:nogroup $FTP_ROOT/cilla

# Change group such that administrators can write/overwrite
sudo chgrp -R "$SYS_GRP" $FTP_ROOT/pxelinux.bzi
sudo chmod g+rwx -R $FTP_ROOT/pxelinux.bzi
sudo setfacl -d -m group:"$SYS_GRP":rwx $FTP_ROOT/pxelinux.bzi

# Change such that Apache can modify pxe-configs and bty-config
chown -R "$SYS_USR":"$SYS_GRP" $BTY_ROOT
chown -R "$WEB_USR":"$WEB_GRP" $BTY_ROOT/bty
chown -R "$WEB_USR":"$WEB_GRP" $BTY_ROOT/cfg
chown -R "$WEB_USR":"$WEB_GRP" $FTP_ROOT/pxelinux.cfg
