#!/usr/bin/env python3
"""EfficientIP SOLIDserver: create / update / verify DHCP + IPAM sites from a CSV.

Run without arguments for the menu, or with --help for the command-line options.
"""
import sys

from eipdhcp.cli import main

if __name__ == "__main__":
    sys.exit(main())
