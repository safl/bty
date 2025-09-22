# bty

## Filesystem

Below is where you find stuff after following the **Host Setup Notes**:

```
/srv/bty                # BTY repository
/srv/images             # Reference environments as qcow2 images
/srv/tftp               # BIOS/syslinux/PXE binaries
/srv/tftp/pxelinux.bzi  # Kernel Images
/srv/tftp/pxelinux.cfg  # PXE configurations
/srv/tftp/cilla         # CloneZilla filesystem, initrd, and Kernel
```

## Host Setup Notes

Using an Ubuntu 16 reference environment do the following:

```bash
# Change hostname

# Install system packages
sudo dnf install \
    dracut \
    httpd \
    mod_wsgi \
    python3-flask \
    python3-jinja2 \
    syslinux \
    syslinux-tftpboot \
    tftp-server \
    -y

# Add odus to www-data group
sudo usermod -a -G apache odus

# Grab bty
sudo git clone https://github.com/safl/bty.git /srv/bty

# Create directory structure
sudo mkdir /srv/tftp
sudo mkdir /srv/tftp/pxelinux.bzi
sudo mkdir /srv/tftp/pxelinux.cfg
sudo mkdir /srv/images
```

### Setup TFTP server and boot environment

```bash
# Copy the boot loader and bios into the tftp directory
sudo cp /tftpboot/pxelinux.0 /srv/tftp/
sudo cp /tftpboot/ldlinux.c32 /srv/tftp/
sudo cp /tftpboot/chain.c32 /srv/tftp/
sudo cp /tftpboot/libcom32.c32 /srv/tftp/
sudo cp /tftpboot/libutil.c32 /srv/tftp/
sudo cp /tftpboot/menu.c32 /srv/tftp/
```

Download a CloneZilla AMD64 zip and use it to create the `cilla` PXE env.:

https://clonezilla.org/downloads/download.php?branch=stable

```bash
unzip clonezilla-live-2.5.6-22-amd64.zip live/filesystem.squashfs
unzip clonezilla-live-2.5.6-22-amd64.zip live/initrd.img
unzip clonezilla-live-2.5.6-22-amd64.zip live/vmlinuz
sudo mv live /srv/tftp/cilla
```

Disable SE Linux for ``/srv``:

```bash
sudo chcon -R -t httpd_sys_content_t /srv
```

Enable the TFTP server by editing conf. file: `sudo vim /etc/default/tftpd-hpa`:

```
# /etc/default/tftpd-hpa

TFTP_USERNAME="tftp"
TFTP_DIRECTORY="/srv/tftp"
TFTP_ADDRESS=":69"
TFTP_OPTIONS="--secure -4"
```

Fix permissions:

```bash
sudo /srv/bty/bin/permissions.sh
```

```
sudo dnf install httpd
```

### Setup HTTP Server and BTY UI

Change the default config `sudo vim /etc/apache2/sites-enabled/000-default.conf`

```
<VirtualHost *:80>
        ServerAdmin webmaster@localhost
        DocumentRoot /srv

        <Directory /srv>
                Options Indexes FollowSymLinks
                AllowOverride None
                Require all granted
        </Directory>

        WSGIDaemonProcess bty threads=1
        WSGIScriptAlias /bty /srv/bty/bin/bty_hardcoded.wsgi
        WSGIScriptReloading On

        <Directory /srv/bty>
                WSGIProcessGroup bty
                WSGIApplicationGroup %{GLOBAL}
                Options Indexes FollowSymLinks
                AllowOverride None
                Require all granted
        </Directory>

        Alias /image /srv/images
        <Directory /srv/images>
            Options Indexes FollowSymLinks
            AllowOverride None
            Require all granted

            Dav On

            # Disable write methods
            <LimitExcept GET OPTIONS PROPFIND>
                Require all denied
            </LimitExcept>
        </Directory>

        ErrorLog /var/log/httpd/error.log
        CustomLog /var/log/httpd/access.log combined
</VirtualHost>
```

```bash
sudo systemctl reload httpd
sudo service httpd restart
```

### Setup NFS exports for deployment / CloneZilla

Edit NFS exports, `sudo vim /etc/exports`:

```
/srv/tftp/cilla     *(ro,sync,no_subtree_check)
/srv/images     *(ro,sync,no_subtree_check)
```

Enable and start `rpc-statd`:

```bash
sudo systemctl enable rpc-statd
sudo systemctl start rpc-statd
```

# TODO

* Fix hardcoded values
 - hostname
 - paths
 - etc.
