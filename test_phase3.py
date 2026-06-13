"""Dry-run test for Phase 3 (spark_usds) — supply + withdraw path."""
import os, logging, sqlite3, random
os.environ['DRY_RUN'] = 'true'

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

import state, agent

state.init_db()

# ── TEST 1: SUPPLY (ETH → USDC → USDS → Spark vault) ─────────────────────────
print('=' * 60)
print('TEST 1: SUPPLY  ETH -> USDC -> USDS -> Spark vault')
print('=' * 60)

p        = agent.CFG['platforms']['spark_usds']
amt      = agent._amount('spark_usds')
tok_addr = p.get('token_address', agent.USDC_ADDR)
failed   = []

print(f'amount: {amt} wei  ({amt/1e18} USDS)')

ok = agent._prepare_token_safe(p, tok_addr, amt, failed)
print(f'_prepare_token_safe -> ok={ok}  failures={failed}')

assert ok, f'FAIL: prepare returned False  failures={failed}'

expiry = random.randint(5, 10)
txh = agent._supply('spark_usds')
state.add_position('spark_usds', p['token'], amt, expiry, txh)
print(f'Position opened  tx={txh[:20]}...  expiry={expiry}d')

# ── TEST 2: WITHDRAW (Spark vault → USDS → USDC → ETH) ───────────────────────
print()
print('=' * 60)
print('TEST 2: WITHDRAW  Spark vault -> USDS -> USDC -> ETH')
print('=' * 60)

# Insert fake expired spark_usds position
conn = sqlite3.connect(state.DB_PATH)
conn.execute(
    "INSERT INTO positions (platform,token,amount_wei,entry_date,expiry_date,tx_hash,status) "
    "VALUES ('spark_usds','USDS','20000000000000000000','2026-05-18','2026-05-28','0x1234','active')"
)
conn.commit()
conn.close()

expired = state.get_expired()
spark_rows = [r for r in expired if r[1] == 'spark_usds']
print(f'Expired spark_usds rows found: {len(spark_rows)}')
assert spark_rows, 'FAIL: no expired spark_usds rows'

pos_id, platform, token, amount_wei, entry, expiry_date, txh2, status = spark_rows[0]
failed2 = []

amt_int = int(float(amount_wei))
withdraw_txh = agent._withdraw(platform, amt_int)
state.close_position(pos_id)
print(f'Withdrew  tx={withdraw_txh[:20]}...')

ok2 = agent._return_to_eth_safe(p, tok_addr, amt_int, failed2)
print(f'_return_to_eth_safe -> ok={ok2}  failures={failed2}')

assert ok2, f'FAIL: return_to_eth returned False  failures={failed2}'

print()
print('ALL ASSERTIONS PASSED ✓')
