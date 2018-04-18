#!/usr/bin/env python
from wsgiref.simple_server import make_server
from subprocess import Popen, PIPE
import pprint
import json
import re

REGEX_HW_ADDR=r".*(([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})).*"

def sys_hwaddr(nic=None):
        """
        Probe the system for hw-address via ifconfig / eth0

        @returns hwaddr on the form AA:BB:CC:11:22:33 on success, None otherwise
        """

        if nic is None:
                nic = "eth0"

        cmd = ["ifconfig", nic]

        proc = Popen(cmd, stdout=PIPE, stderr=PIPE)

        out, _ = proc.communicate()
        out = out.lower() if out else ""

        match = re.match(REGEX_HW_ADDR, out)
        if match:
                return match.group(1).upper()

        return None

def application(environ, start_response):
        """Application generating and modifying PXE configurations"""

        status = '200 OK'
        output = b'Hello World!'
        output = pprint.pformat(environ)

        response_headers = [
                ('Content-type', 'text/plain'),
                ('Content-Length', str(len(output)))
        ]
        start_response(status, response_headers)

        return [output]

if __name__ == "__main__":

        httpd = make_server('', 8000, application)
        print "Serving on port 8000..."

        # Serve until process is killed
        httpd.serve_forever()

