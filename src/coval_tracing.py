"""Compatibility import for Coval voice trace export."""

import sys

from coval_voice import tracing

sys.modules[__name__] = tracing
