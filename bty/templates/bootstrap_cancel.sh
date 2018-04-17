#!/usr/bin/env bash

COUNTDOWN=10
echo -n "# Machine is not managed, skipping in $COUNTDOWN seconds"

for i in $(seq 1 $COUNTDOWN); do echo -n "."; sleep 1; done

echo "SKIPPING!"
sleep 1

exit 0
