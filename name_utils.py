"""Shared human-readable platform name generator."""

_TOK = {
    'weth':'WETH','usdc':'USDC','usdt':'USDT','wsteth':'wstETH','cbbtc':'cbBTC',
    'eurc':'EURC','aero':'AERO','virtual':'VIRTUAL','usds':'USDS','susds':'sUSDS',
    'morpho':'MORPHO','cbxrp':'cbXRP','cake':'CAKE','weeth':'weETH','dola':'DOLA',
    'usdz':'USDz',
}
def _tn(s): return _TOK.get(s.lower(), s.upper())

def _auto_name(key):
    k = key.lower()
    _fee = {'100':'0.01%','500':'0.05%','2500':'0.25%','3000':'0.3%','10000':'1%'}
    _special = {
        'deploy_contract': 'Deploy Contract',
        'megapot':         'ซื้อลอตเตอรี Megapot',
        'aero_vote':       'โหวต AERO ที่ Aerodrome',
        'compound_usdc':   'ฝาก USDC ที่ Compound',
        'spark_susds':     'ฝาก sUSDS ที่ Spark',
    }
    if k in _special: return _special[k]

    if k.startswith('aero_lp_'):
        p = k[8:].split('_'); return f'LP {_tn(p[0])}/{_tn(p[1])} ที่ Aerodrome'

    if k.startswith('uni_lp_'):
        p = k[7:].split('_')
        fee = _fee.get(p[2], p[2]) if len(p) > 2 else ''
        return f'LP {_tn(p[0])}/{_tn(p[1])} ที่ Uniswap ({fee})' if fee else f'LP {_tn(p[0])}/{_tn(p[1])} ที่ Uniswap'

    if k.startswith('pancake_lp_'):
        p = k[11:].split('_')
        fee = _fee.get(p[2], p[2]) if len(p) > 2 else ''
        return f'LP {_tn(p[0])}/{_tn(p[1])} ที่ PancakeSwap ({fee})' if fee else f'LP {_tn(p[0])}/{_tn(p[1])} ที่ PancakeSwap'

    if k.startswith('beefy_') and k.endswith('_vlp'):
        p = k[6:-4].split('_'); return f'LP {_tn(p[0])}/{_tn(p[1])} ที่ Beefy'

    if k.startswith('beefy_'):
        p = k[6:].split('_')
        vault = p[1].title() if len(p) > 1 else ''
        return f'ฝาก {_tn(p[0])} ที่ Beefy ({vault})' if vault else f'ฝาก {_tn(p[0])} ที่ Beefy'

    if k.startswith('cb_'):
        p = k[3:].split('_'); base = _tn(p[0])
        coll = 'หลายค้ำ' if p[1] == 'multi' else '+'.join(_tn(x) for x in p[1:])
        return f'ยืม {base} ใช้ {coll} ค้ำที่ Compound'

    if k.startswith('fl_'):
        p = k[3:].split('_')
        return f'ยืม {_tn(p[1])} ใช้ {_tn(p[0])} ค้ำที่ Fluid'

    if k.startswith('mw_'):
        p = k[3:].split('_')
        return f'ยืม {_tn(p[1])} ใช้ {_tn(p[0])} ค้ำที่ Moonwell'

    if k.startswith('aav_'):
        p = k[4:].split('_')
        return f'ยืม {_tn(p[1])} ใช้ {_tn(p[0])} ค้ำที่ AAVE'

    if k.startswith('aave_'):
        return f'ฝาก {_tn(k[5:])} ที่ AAVE'

    for prefix, proto in [('fluid','Fluid'),('moonwell','Moonwell'),('morpho','Morpho')]:
        if k.startswith(prefix + '_'):
            return f'ฝาก {_tn(k[len(prefix)+1:])} ที่ {proto}'

    return key
