"""Morse ident decode -- placeholder for RB-VOR-IF v1.1.

Reference: rb-vor-architecture.md section 16 (backlog); open item, not in v1.0.

Not implemented. A VOR keys its 2-3 letter identifier at 1020 Hz every ~10 s,
and decoding it is the real safeguard against tuning the wrong station -- but it
is a v1.1 feature and is deliberately out of the frozen v1.0 interface.

The hook is a callable on the demodulator that would receive the 1020 Hz band;
the generator already produces a keyed ident (`tools/vor_gen.py --ident`), and
`rbvor.quality` already ignores it, so adding the decoder later needs no change
to the existing signal path.

Importing this module does nothing. Call `decode_ident()` and it tells you,
plainly, that the feature is not built -- rather than raising on import and
making the whole package look broken.
"""

from __future__ import annotations

__all__ = ["IDENT_HZ", "decode_ident", "AVAILABLE"]

IDENT_HZ = 1020.0
AVAILABLE = False


def decode_ident(*_args, **_kwargs) -> None:
    """Not implemented in v1.0. Returns None; reserved as `vorident` for v1.1."""
    return None
