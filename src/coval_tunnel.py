#!/usr/bin/env python3
"""Compatibility entry point for the Coval voice client and CLI."""

import sys

from coval_voice import client

if __name__ == "__main__":
    client.cli()
else:
    sys.modules[__name__] = client
