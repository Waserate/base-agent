"""Print all positions in state.db."""
import state
from datetime import date

_TOK = {
    'weth':'WETH','usdc':'USDC','usdt':'USDT','wsteth':'wstETH','cbbtc':'cbBTC',
    'eurc':'EURC','aero':'AERO','virtual':'VIRTUAL','susds':'sUSDS','morpho':'MORPHO',
    'cbxrp':'cbXRP','cake':'CAKE','weeth':'weETH','dola':'DOLA','usdz':'USDz',
}
def _tn(s): return _TOK.get(s.lower(), s.upper())

def _task_name(key):
    k = key.lower()
    _fee = {'100':'0.01%','500':'0.05%','2500':'0.25%','3000':'0.3%','10000':'1%'}
    _sp = {
        'deploy_contract':'Deploy Contract','megapot':'Megapot Lottery',
        'aero_vote':'Vote AERO @ Aerodrome','compound_usdc':'Lend USDC @ Compound',
        'spark_susds':'Lend sUSDS @ Spark',
    }
    if k in _sp: return _sp[k]
    if k.startswith('aero_lp_'):
        p=k[8:].split('_'); return f'LP {_tn(p[0])}/{_tn(p[1])} @ Aerodrome'
    if k.startswith('uni_lp_'):
        p=k[7:].split('_'); fee=_fee.get(p[2],'') if len(p)>2 else ''
        return f'LP {_tn(p[0])}/{_tn(p[1])} @ Uniswap ({fee})' if fee else f'LP {_tn(p[0])}/{_tn(p[1])} @ Uniswap'
    if k.startswith('pancake_lp_'):
        p=k[11:].split('_'); fee=_fee.get(p[2],'') if len(p)>2 else ''
        return f'LP {_tn(p[0])}/{_tn(p[1])} @ Pancake ({fee})' if fee else f'LP {_tn(p[0])}/{_tn(p[1])} @ Pancake'
    if k.startswith('beefy_') and k.endswith('_vlp'):
        p=k[6:-4].split('_'); return f'LP {_tn(p[0])}/{_tn(p[1])} @ Beefy'
    if k.startswith('beefy_'):
        p=k[6:].split('_'); v=p[1].title() if len(p)>1 else ''
        return f'Lend {_tn(p[0])} @ Beefy ({v})' if v else f'Lend {_tn(p[0])} @ Beefy'
    if k.startswith('cb_'):
        p=k[3:].split('_'); base=_tn(p[0])
        coll='multi-col' if p[1]=='multi' else '+'.join(_tn(x) for x in p[1:])
        return f'Borrow {base} ({coll} col) @ Compound'
    if k.startswith('fl_'):
        p=k[3:].split('_'); return f'Borrow {_tn(p[1])} ({_tn(p[0])} col) @ Fluid'
    if k.startswith('mw_'):
        p=k[3:].split('_'); return f'Borrow {_tn(p[1])} ({_tn(p[0])} col) @ Moonwell'
    if k.startswith('aav_'):
        p=k[4:].split('_'); return f'Borrow {_tn(p[1])} ({_tn(p[0])} col) @ AAVE'
    if k.startswith('aave_'): return f'Lend {_tn(k[5:])} @ AAVE'
    for pf,name in [('fluid','Fluid'),('moonwell','Moonwell'),('morpho','Morpho')]:
        if k.startswith(pf+'_'): return f'Lend {_tn(k[len(pf)+1:])} @ {name}'
    return key

state.init_db()
rows = state.all_positions()

if not rows:
    print('No positions in database.')
else:
    print(f'\n{"ID":>3}  {"Task":32}  {"Token":6}  {"Amount":>15}  {"Entry":12}  {"Expiry":12}  {"Status":8}  {"USD":>7}  TX')
    print('-' * 130)
    today = date.today().isoformat()
    for pos in rows:
        pos_id   = pos[0]
        platform = pos[1]
        token    = pos[2]
        amount_wei = pos[3]
        entry    = pos[4]
        expiry   = pos[5]
        tx_hash  = pos[6]
        status   = pos[7] if len(pos) > 7 else 'unknown'
        opened_usd = pos[8] if len(pos) > 8 else None
        expired = ' ⚠' if status == 'active' and expiry <= today else ''
        tx_short = (tx_hash or '')[:12] + '...' if tx_hash else '-'
        usd_str = f'${float(opened_usd):.2f}' if opened_usd else '  n/a'
        try:
            amt_display = f'{int(amount_wei):>15}'
        except (ValueError, TypeError):
            amt_display = f'{str(amount_wei)[:15]:>15}'
        task = _task_name(platform)
        print(f'{pos_id:>3}  {task:32}  {token:6}  {amt_display}  {entry:12}  {expiry:12}  {status:8}  {usd_str:>7}  {tx_short}{expired}')
    print()
