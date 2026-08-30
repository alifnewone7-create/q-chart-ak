"""Strategy switch — the only job of this module.

Every engine lives in its own file inside this package and publishes a `META`
dict (key, name, tagline, min_confidence, min_candles, fn, about):

    classic.py      -> Classic Momentum
    otc_sniper.py   -> OTC Sniper Pro
    zone_sniper.py  -> Zone Reversal Sniper
    common.py       -> indicator helpers shared by more than one engine

To add a strategy: drop a new <name>.py in here with a META dict, import it
below and append its key to ORDER. Nothing else in the bot changes.
"""
from .classic import META as _CLASSIC_META
from .classic import classic_momentum  # noqa: F401  (flat re-export)
from .common import _clamp, _efficiency  # noqa: F401  (flat re-export)
from .otc_sniper import META as _OTC_META
from .otc_sniper import (  # noqa: F401  (flat re-exports)
    MODULES,
    OTC_MIN_CANDLES,
    _adaptive_factors,
    _m_persistence,
    otc_sniper,
)
from .zone_sniper import META as _ZONE_META
from .zone_sniper import (  # noqa: F401  (flat re-exports)
    ZONE_FILTERS,
    ZONE_MIN_CANDLES,
    _f_multi_rejection,
    _z_best_rejection_level,
    _z_context,
    _z_rev_confirm,
    zone_levels,
    zone_sniper,
)

_ENGINES = (_CLASSIC_META, _OTC_META, _ZONE_META)

STRATEGIES = {m["key"]: m for m in _ENGINES}
ORDER = ["classic", "otc_sniper", "zone_sniper"]
DEFAULT_KEY = "classic"


def get(key):
    return STRATEGIES.get(key) or STRATEGIES[DEFAULT_KEY]
