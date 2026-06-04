"""
settings.py — Runtime-configurable agent settings.

All values are read at call-time (not import-time) so changes take effect
on the next scheduled job without restarting agent.py.

Usage:
    import settings
    cfg = settings.load()          # read current settings
    settings.save(cfg)             # atomic write
    settings.reset()               # restore defaults
    settings.expiry_for_type(ptype)  # random expiry days by platform type
"""

import os, json, random, copy

SETTINGS_FILE = os.path.join(os.path.dirname(__file__), 'settings.json')

DEFAULTS = {
    "usd_tiers": [
        {"label": "low",  "min": 5.0,  "max": 8.0,  "weight": 0.70},
        {"label": "mid",  "min": 8.0,  "max": 12.0, "weight": 0.25},
        {"label": "high", "min": 12.0, "max": 15.0, "weight": 0.05},
    ],
    "eth_min":         0.005,
    "usdc_retain_usd": 10.0,
    "weth_retain_eth": 0.005,
    "expiry_days": {
        "lend":   [3, 5],
        "lp":     [3, 5],
        "borrow": [3, 7],
        "vote":   [7, 14],
    },
    "max_concurrent": {
        "lp":     5,
        "lend":   6,
        "borrow": 4,
    },
}

_BORROW_TYPES = {'compound_borrow', 'mw_borrow', 'fluid_borrow', 'aave_borrow'}
_LP_TYPES     = {'aero_lp', 'beefy_lp', 'uni_lp', 'pancake_lp', 'beefy_single'}
_VOTE_TYPES   = {'aero_vote'}


def load() -> dict:
    """Load settings, filling missing keys from DEFAULTS."""
    try:
        with open(SETTINGS_FILE) as f:
            s = json.load(f)
        for k, v in DEFAULTS.items():
            if k not in s:
                s[k] = v
        return s
    except (FileNotFoundError, json.JSONDecodeError):
        return copy.deepcopy(DEFAULTS)


def save(data: dict) -> None:
    """Atomic write: tmp file then os.replace to avoid partial-read corruption."""
    tmp = SETTINGS_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SETTINGS_FILE)


def reset() -> dict:
    """Reset to defaults, persist to disk, and return the defaults."""
    d = copy.deepcopy(DEFAULTS)
    save(d)
    return d


def print_config() -> None:
    """Print current settings summary to stdout (CMD window)."""
    cfg = load()
    t   = cfg.get('usd_tiers', DEFAULTS['usd_tiers'])
    exp = cfg.get('expiry_days', DEFAULTS['expiry_days'])
    mc  = cfg.get('max_concurrent', DEFAULTS['max_concurrent'])
    tier_str = '  |  '.join(
        f"{int(round(x['weight']*100))}% ${x['min']}-${x['max']}" for x in t
    )
    import os as _os
    wid = _os.environ.get('WALLET_ID', '')
    try:
        import wallet_manager as _wm_s
        _we_s = _wm_s.get_wallet(wid)
        wlabel = f'  [{_we_s["name"]}]' if _we_s else (f'  [{wid}]' if wid else '')
    except Exception:
        wlabel = f'  [{wid}]' if wid else ''
    sep = '=' * 55
    print(sep)
    print(f'  AGENT CONFIG (settings.json){wlabel}')
    print(sep)
    print(f"  USD TIERS    {tier_str}")
    print(f"  ETH MIN      {cfg.get('eth_min', 0.005)} ETH")
    print(f"  KEEP USDC    ${cfg.get('usdc_retain_usd', 10.0)}    "
          f"KEEP WETH  {cfg.get('weth_retain_eth', 0.005)} ETH")
    print(f"  HOLD LEND    {exp.get('lend',[3,5])[0]}-{exp.get('lend',[3,5])[1]}d    "
          f"HOLD LP    {exp.get('lp',[3,5])[0]}-{exp.get('lp',[3,5])[1]}d    "
          f"HOLD BORROW  {exp.get('borrow',[3,7])[0]}-{exp.get('borrow',[3,7])[1]}d    "
          f"HOLD VOTE  {exp.get('vote',[7,14])[0]}-{exp.get('vote',[7,14])[1]}d")
    print(f"  MAX LP       {mc.get('lp',5)}    "
          f"MAX LEND   {mc.get('lend',6)}    "
          f"MAX BORROW   {mc.get('borrow',4)}")
    print(sep)


def expiry_for_type(ptype: str) -> int:
    """Return random expiry days for a platform type using current settings."""
    cfg = load()
    exp = cfg.get('expiry_days', DEFAULTS['expiry_days'])
    if ptype in _BORROW_TYPES:
        r = exp.get('borrow', [3, 7])
    elif ptype in _LP_TYPES:
        r = exp.get('lp', [3, 5])
    elif ptype in _VOTE_TYPES:
        r = exp.get('vote', [7, 14])
    else:
        r = exp.get('lend', [3, 5])
    return random.randint(int(r[0]), int(r[1]))
