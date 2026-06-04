"""Test withdraw_all.py priority ordering logic in isolation."""
import unittest

# Define the exact priority logic to be implemented
_BORROW_TYPES  = {'compound_borrow', 'mw_borrow', 'fluid_borrow', 'aave_borrow'}
_SUPPLY_TYPES  = {'comet', 'erc4626', 'ctoken', 'psm_hold', 'beefy_single', 'aave_supply'}
_LP_TYPES      = {'beefy_lp', 'aero_lp', 'uni_lp', 'pancake_lp'}

_PLATFORMS_CFG = {
    'cb_usdc_weth':   {'type': 'compound_borrow'},
    'mw_weth_usdc':   {'type': 'mw_borrow'},
    'fl_eth_usdc':    {'type': 'fluid_borrow'},
    'aave_weth_usdc': {'type': 'aave_borrow'},
    'compound_usdc':  {'type': 'comet'},
    'fluid_usdc':     {'type': 'erc4626'},
    'aero_lp_weth':   {'type': 'aero_lp'},
    'uni_lp_weth':    {'type': 'uni_lp'},
    'aero_vote':      {'type': 'aero_vote'},
}

def _type_priority(pos_row, platforms_cfg):
    ptype = platforms_cfg.get(pos_row[1], {}).get('type', '')
    if ptype in _BORROW_TYPES: return (0, pos_row[0])
    if ptype in _SUPPLY_TYPES: return (1, pos_row[0])
    if ptype in _LP_TYPES:     return (2, pos_row[0])
    return (3, pos_row[0])

def _make_pos(pos_id, platform):
    return (pos_id, platform, 'USDC', '5000000', '2026-05-30', '2026-06-05', '0xabc', 'active')

class TestWithdrawPriority(unittest.TestCase):

    def _sort(self, rows):
        return sorted(rows, key=lambda r: _type_priority(r, _PLATFORMS_CFG))

    def test_borrow_before_supply(self):
        rows = [
            _make_pos(1, 'compound_usdc'),  # supply
            _make_pos(2, 'cb_usdc_weth'),   # borrow
        ]
        sorted_rows = self._sort(rows)
        self.assertEqual(sorted_rows[0][1], 'cb_usdc_weth')   # borrow first
        self.assertEqual(sorted_rows[1][1], 'compound_usdc')  # supply second

    def test_borrow_before_lp(self):
        rows = [
            _make_pos(1, 'aero_lp_weth'),  # lp
            _make_pos(2, 'fl_eth_usdc'),   # borrow
        ]
        sorted_rows = self._sort(rows)
        self.assertEqual(sorted_rows[0][1], 'fl_eth_usdc')

    def test_supply_before_lp(self):
        rows = [
            _make_pos(1, 'uni_lp_weth'),  # lp
            _make_pos(2, 'fluid_usdc'),   # supply
        ]
        sorted_rows = self._sort(rows)
        self.assertEqual(sorted_rows[0][1], 'fluid_usdc')

    def test_lp_before_vote(self):
        rows = [
            _make_pos(1, 'aero_vote'),     # other
            _make_pos(2, 'aero_lp_weth'),  # lp
        ]
        sorted_rows = self._sort(rows)
        self.assertEqual(sorted_rows[0][1], 'aero_lp_weth')

    def test_within_same_priority_id_ascending(self):
        rows = [
            _make_pos(5, 'mw_weth_usdc'),   # borrow id=5
            _make_pos(2, 'cb_usdc_weth'),   # borrow id=2
            _make_pos(8, 'aave_weth_usdc'), # borrow id=8
        ]
        sorted_rows = self._sort(rows)
        ids = [r[0] for r in sorted_rows]
        self.assertEqual(ids, [2, 5, 8])  # sorted by id within priority

    def test_all_four_priorities(self):
        rows = [
            _make_pos(4, 'aero_vote'),      # other=3
            _make_pos(3, 'uni_lp_weth'),    # lp=2
            _make_pos(2, 'compound_usdc'),  # supply=1
            _make_pos(1, 'cb_usdc_weth'),   # borrow=0
        ]
        sorted_rows = self._sort(rows)
        platforms = [r[1] for r in sorted_rows]
        self.assertEqual(platforms, ['cb_usdc_weth', 'compound_usdc', 'uni_lp_weth', 'aero_vote'])

if __name__ == '__main__':
    unittest.main()
