#!/usr/bin/env bash

ERR_WAIT=10

function exit_msg {
  MSG=$1
  if [[ -z "$MSG" ]]; then
    MSG="Something went wrong"
  fi

  echo -n $MSG
  for i in $(seq 1 $ERR_WAIT); do echo -n "."; sleep 1; done
  echo "EXITING!"
  sleep 1

  exit 1
}

function wait_msg {
  MSG=$1
  if [[ -z "$MSG" ]]; then
    MSG="Waiting $ERR_WAIT seconds"
  fi

  echo -n $MSG
  for i in $(seq 1 $ERR_WAIT); do echo -n "."; sleep 1; done
  echo ""
}

IMAGE_ROOT=/home/partimag
IMAGE_FNAME={{ machine.image }}
IMAGE_PATH=$IMAGE_ROOT/$IMAGE_FNAME

DEV_PATH=/dev/sda

echo "DEV_PATH: '$DEV_PATH'"
if [[ ! -e "$DEV_PATH" ]]; then
	DEV_PATH=$(lsblk --output name -r -p -x name | grep -v loop | sed -n 2p)
fi

echo "DEV_PATH: '$DEV_PATH'"
if [[ ! -e "$DEV_PATH" ]]; then
	exit_msg "FAILED: cannot find a block device to install to"
fi

wait_msg "Waiting for devices to settle"

if [[ ! -d $IMAGE_ROOT ]]; then
  exit_msg "FAILED: invalid IMAGE_ROOT: '$IMAGE_ROOT'"
fi

if [[ ! -f $IMAGE_PATH ]]; then
  exit_msg "FAILED: invalid IMAGE_PATH: '$IMAGE_PATH'"
fi

if [[ ! -b $DEV_PATH ]]; then
  exit_msg "FAILED: invalid DEV_PATH: '$DEV_PATH'"
fi

# Write the image to disk
echo "# cloning..."
qemu-img convert -f qcow2 -O raw -p $IMAGE_PATH $DEV_PATH
if [[ $? -ne 0 ]]; then
  exit_msg "FAILED: writing '$IMAGE_PATH' to '$DEV_PATH'"
fi

# Mount and change hostname
echo "# Mount and change hostname"

HOSTNAME="{{ machine.hostname }}"
MP=jazz

mkdir -p $MP
mount /dev/sda1 $MP

echo "$HOSTNAME" > $MP/etc/hostname
sed -i "s/127.0.1.1.*/127.0.1.1\t$HOSTNAME/" $MP/etc/hosts

umount $MP

echo "DONE"

sleep 2

exit 0

