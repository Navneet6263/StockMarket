"""Fresh demand and resistance zone scanner.

Detects fresh, untested weekly/monthly/yearly demand and resistance zones,
scores them, and tracks price as it approaches, touches, reacts to or breaks
them.  Everything here is a technical classification built from explicit,
configurable rules; nothing in this package claims to know institutional
orders or the probability of a successful trade.

See ``docs/FRESH_ZONE_SCANNER.md`` for the specification mapping and the
owner decisions that are still open.
"""

from app.fresh_zones.params import ScannerParams, get_default_params

__all__ = ["ScannerParams", "get_default_params"]
