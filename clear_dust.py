"""
clear_dust.py — Sequential withdrawal of dust positions (< $1 USD).

Processes one position at a time, waits for each to complete before moving
to the next. Sleeps 5s between withdrawals to avoid 429.

Usage:
  python clear_dust.py              # dry-run preview
  python clear_dust.py --live       # actual execution
  python clear_dust.py --threshold 2.0  # custom USD threshold
"""
import sys, os, time, json, subprocess, logging, argparse
from datetime import date

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    datefmt='%H:%M:%S',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

from dotenv import load_dotenv
load_dotenv()

import state

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SLEEP_BETWEEN = 5  # seconds between withdrawals

with open(os.path.join(SCRIPT_DIR, 'config/contracts.json')) as f:
    CFG = json.load(f)


def _usd_est(pos) -> float:
    """Mirror serve_dashboard.py usd_est logic."""
    amount_wei = pos[3]
    opened_usd = pos[8] if len(pos) > 8 else None

    if opened_usd is not None:
        return float(opened_usd)

    platform = pos[1]
    p_cfg    = CFG.get('platforms', {}).get(platform, {})
    ptype    = p_cfg.get('type', '')

    if ptype == 'erc4626' and '||' not in str(amount_wei):
        try:
            shares = int(amount_wei) / 1e18
            return 0.0 if shares < 0.01 else 5.0
        except Exception:
            return 5.0

    return 5.0  # unknown — treat as non-dust to be safe


def get_dust_positions(threshold: float = 1.0) -> list:
    """Return active positions with usd_est < threshold."""
    state.init_db()
    active = state.get_active()
    dust = []
    for pos in active:
        usd = _usd_est(pos)
        if usd < threshold:
            dust.append({
                'id':       pos[0],
                'platform': pos[1],
                'token':    pos[2],
                'usd_est':  usd,
                'ptype':    CFG.get('platforms', {}).get(pos[1], {}).get('type', '?'),
            })
    return dust


def run(threshold: float = 1.0, live: bool = False) -> dict:
    """
    Sequential dust withdrawal.
    Returns {processed, skipped, errors, positions}.
    """
    state.init_db()
    dust = get_dust_positions(threshold)

    if not dust:
        log.info(f'No dust positions found (threshold=${threshold:.2f})')
        return {'processed': 0, 'skipped': 0, 'errors': 0, 'positions': []}

    log.info('=' * 60)
    log.info(f'CLEAR DUST  threshold=${threshold:.2f}  {"[LIVE]" if live else "[DRY RUN]"}')
    log.info(f'Found {len(dust)} dust position(s):')
    for d in dust:
        log.info(f'  #{d["id"]:3d}  {d["platform"]:<35}  ${d["usd_est"]:.4f}')
    log.info('=' * 60)

    if not live:
        log.info('DRY RUN — pass --live to execute')
        return {'processed': 0, 'skipped': len(dust), 'errors': 0, 'positions': dust}

    processed = 0
    errors    = 0

    for i, d in enumerate(dust, 1):
        pos_id   = d['id']
        platform = d['platform']
        log.info(f'[{i}/{len(dust)}] Withdrawing #{pos_id} {platform} (${d["usd_est"]:.4f}) ...')

        try:
            result = subprocess.run(
                [sys.executable, 'withdraw_all.py', '--id', str(pos_id)],
                cwd=SCRIPT_DIR,
                timeout=180,
            )
            if result.returncode == 0:
                # Verify position is now closed
                still_active = any(p[0] == pos_id for p in state.get_active())
                if still_active:
                    log.warning(f'  [WARN] #{pos_id} still active after withdrawal — may need manual check')
                    errors += 1
                else:
                    log.info(f'  [OK] #{pos_id} {platform} closed successfully')
                    processed += 1
            else:
                log.warning(f'  [FAIL] #{pos_id} withdraw_all returned code {result.returncode}')
                errors += 1
        except subprocess.TimeoutExpired:
            log.warning(f'  [TIMEOUT] #{pos_id} took >180s — skipping')
            errors += 1
        except Exception as e:
            log.warning(f'  [ERROR] #{pos_id}: {e}')
            errors += 1

        if i < len(dust):
            log.info(f'  Sleeping {SLEEP_BETWEEN}s before next ...')
            time.sleep(SLEEP_BETWEEN)

    log.info('=' * 60)
    log.info(f'Done: {processed} closed, {errors} errors')
    log.info('=' * 60)

    return {'processed': processed, 'errors': errors, 'skipped': 0, 'positions': dust}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Clear dust positions (< threshold USD)')
    parser.add_argument('--live',      action='store_true', help='Actually execute (default: dry run)')
    parser.add_argument('--threshold', type=float, default=1.0, help='USD threshold (default: 1.0)')
    args = parser.parse_args()
    run(threshold=args.threshold, live=args.live)
