"""Compatibility shim: NARS is an instance of the generic SFCC adapter.

Everything lives in sfcc.py; brands.yaml carries the NARS-specific config
(controller base, US/archive fallbacks, shade formatting).
"""

from bsb.resolve.adapters.sfcc import (  # noqa: F401
    MasterResult,
    SfccAdapter,
    VariantResult,
    extract_inci,
    shade_from_title,
)

NarsAdapter = SfccAdapter
