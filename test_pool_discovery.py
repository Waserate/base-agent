"""Test pool discovery with sleep fix — no TX sent."""
import logging, sys, time, os
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)

# Delete cache so discovery runs fresh
cache_path = os.path.join(os.path.dirname(__file__), 'cache', 'vote_pools.json')
if os.path.exists(cache_path):
    os.remove(cache_path)
    print('Deleted old cache')

from aero_vote import _fetch_top_pools, _save_pool_cache

print('Starting pool discovery...')
t0 = time.time()
top = _fetch_top_pools()
_save_pool_cache(top)
elapsed = time.time() - t0

print(f'\nDone in {elapsed:.0f}s  ({len(top)} pools cached)')
print('\nTop 10 pools by emission score:')
print(f'  {"#":>2}  {"Pool":42}  {"Score":>12}  {"rewardRate":>12}')
for i, p in enumerate(top[:10]):
    print(f'  {i+1:>2}  {p["pool"]}  {p["score"]:>12.8f}  {p["reward_rate"]:>12}')
