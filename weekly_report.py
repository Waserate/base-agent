"""
weekly_report.py — Weekly summary from daily_stats.

Usage:
    python weekly_report.py          # prints report (Monday only)
    python weekly_report.py --force  # print even if not Monday

Scheduled: agent.py calls run() every day; run() self-gates on Monday.
"""

import logging
import sys
from datetime import date, timedelta

import state

log = logging.getLogger(__name__)


def should_run(today: date | None = None) -> bool:
    """Return True if today is Monday (weekday == 0)."""
    d = today or date.today()
    return d.weekday() == 0


def _action_totals(days: int = 7) -> dict:
    """Sum action counts across last `days` days."""
    rows = state.get_daily_stats(days)
    totals = dict(lend=0, borrow=0, lp=0, vote=0, game=0, deploy=0)
    for r in rows:
        for k in totals:
            totals[k] += r.get(f'{k}_count', 0)
    totals['total'] = sum(totals.values())
    return totals


def build_report(days: int = 7) -> str:
    """Build and return the weekly report string."""
    rows = state.get_daily_stats(days)
    if not rows:
        return 'No data for weekly report.'

    today      = date.today()
    week_end   = today.isoformat()
    week_start = (today - timedelta(days=days - 1)).isoformat()

    totals = _action_totals(days)

    portfolios = [r['portfolio_usd'] for r in rows if r.get('portfolio_usd', 0) > 0]
    eth_prices = [r['eth_price']     for r in rows if r.get('eth_price', 0)     > 0]
    latest_portfolio = portfolios[0] if portfolios else 0.0
    avg_portfolio    = sum(portfolios) / len(portfolios) if portfolios else 0.0
    latest_eth       = eth_prices[0]  if eth_prices  else 0.0

    # Previous week comparison
    prev_rows       = state.get_daily_stats(days * 2)
    prev_rows       = [r for r in prev_rows if r['date'] < week_start]
    prev_portfolios = [r['portfolio_usd'] for r in prev_rows if r.get('portfolio_usd', 0) > 0]
    prev_avg        = sum(prev_portfolios) / len(prev_portfolios) if prev_portfolios else None

    if prev_avg and prev_avg > 0:
        change_pct = (avg_portfolio - prev_avg) / prev_avg * 100
        vs_prev    = f'{change_pct:+.1f}%'
    else:
        vs_prev = 'N/A'

    lines = [
        '====================================',
        'BASE AGENT — WEEKLY REPORT',
        f'{week_start} to {week_end}',
        '====================================',
        f'Actions ({days} days):',
        f'  Lend:   {totals["lend"]:<5}  Borrow: {totals["borrow"]:<5}  LP:     {totals["lp"]}',
        f'  Vote:   {totals["vote"]:<5}  Game:   {totals["game"]:<5}  Deploy: {totals["deploy"]}',
        f'  Total:  {totals["total"]}',
        '',
        'Portfolio:',
        f'  Latest:   ${latest_portfolio:,.2f}    ETH: ${latest_eth:,.0f}',
        f'  Avg (7d): ${avg_portfolio:,.2f}',
        f'  vs prev 7d: {vs_prev}',
        '',
        '====================================',
    ]
    return '\n'.join(lines)


def run(force: bool = False) -> str | None:
    """
    Print weekly report if today is Monday (or force=True).
    Returns report string or None if skipped.
    """
    if not force and not should_run():
        log.info('weekly_report: not Monday — skipping')
        return None
    state.init_db()
    report = build_report()
    log.info('\n' + report)
    return report


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s',
                        handlers=[logging.StreamHandler(sys.stdout)])
    force  = '--force' in sys.argv
    result = run(force=force)
    if result:
        print(result)
    else:
        print(f'Not Monday ({date.today().strftime("%A")}) — use --force to run anyway.')
