"""
remediation_agent.py — Sonnet-powered incident diagnoser (Phase 2a: DIAGNOSE-ONLY).

Given an incident from incident_store, run Sonnet (via the Claude Agent SDK,
subscription auth) to investigate and produce a structured diagnosis:
root cause, category, proposed recovery action, and whether it is auto-fixable.

SAFETY (Phase 2a):
  - READ-ONLY. Allowed tools: Read, Grep, Glob only. No Bash / Edit / Write.
  - A can_use_tool gate denies anything outside that allowlist (belt + braces).
  - The agent CANNOT touch funds, send TXs, edit code, or run scripts here.
  - Live remediation (running withdraw_all/reconcile/etc.) is Phase 2b, behind
    REMEDIATION_MODE=live — not implemented yet.

Output is written back to the incident (status -> diagnosed, proposal = {...}).
Auth: uses the local Claude Code subscription login (no API key needed).
"""

import os, re, json, glob, asyncio, logging, sqlite3
from datetime import date

import incident_store as store

log = logging.getLogger(__name__)

MODEL       = os.getenv('REMEDIATION_MODEL', 'claude-sonnet-4-6')
MODE        = os.getenv('REMEDIATION_MODE', 'diagnose')   # diagnose | live (2b)
TIMEOUT_S   = int(os.getenv('REMEDIATION_TIMEOUT_S', '300'))
MAX_TURNS   = int(os.getenv('REMEDIATION_MAX_TURNS', '20'))
_DIR        = os.path.dirname(os.path.abspath(__file__))

_ALLOWED_TOOLS = ['Read', 'Grep', 'Glob']

SYSTEM_PROMPT = """You are the remediation diagnoser for an automated Base-chain \
DeFi airdrop bot. An action failed. Investigate the ROOT CAUSE using ONLY \
read-only tools (Read, Grep, Glob) on this repository and the pre-gathered \
context in the prompt.

You are DIAGNOSE-ONLY. You cannot run scripts, send transactions, edit code, or \
move funds. Do not attempt to — those tools are blocked.

Classify the root cause into exactly one category:
- state_drift : DB disagrees with on-chain truth (orphan/ghost position, already \
withdrawn, stale record). Fixable by reconcile / closing the DB row.
- external    : an on-chain/market condition (reserve frozen/paused, no liquidity, \
price-guard depeg, gas too low). The bot code is fine; retrying later or funding \
the wallet is the answer. NOT auto-fixable by us.
- code_bug    : a defect in this repo's code (wrong amount, bad encoding, logic \
error). Needs a code change (human-approved).
- unknown     : insufficient evidence.

When you see a TX revert with a custom error selector (e.g. 0x2c5211c6) that \
persists after fallback gas, it is a deterministic on-chain revert — NOT an RPC/gas \
issue. Decode it from context if you can (Aave/Compound/Moonwell error tables, the \
repo's ABIs).

BE EFFICIENT — strict turn budget. The prompt ALREADY contains the logs, the \
state.db rows, and the platform config. For most state_drift and external cases \
you can conclude from that context ALONE with zero tool calls — do so. Only read \
source code if you genuinely suspect a code_bug and must confirm the mechanism, \
and then read NARROWLY: use Grep -n with context to find the function; never Read \
a large file whole (executor.py, withdraw_all.py, serve_dashboard.py). Per-incident \
the relevant module is small (e.g. aave_supply.py). Conclude in as few tool calls \
as possible — emit the json block as soon as you are confident.

Finish your reply with ONE fenced json block, nothing after it:
```json
{"root_cause":"<one or two sentences>","category":"state_drift|external|code_bug|unknown","proposed_action":"<the single recovery step you'd take, e.g. 'reconcile to close ghost DB row' or 'withdraw_all --id 30' or 'wait — AAVE cbBTC reserve frozen' or 'fix aave_supply.withdraw_all amount'>","auto_fixable":true,"severity":"info|warn|critical","confidence":"low|medium|high"}
```"""


# ── Context gathering (pre-fetched into the prompt — no live tools needed) ──────

def _tail(path: str, n: int = 40, cap: int = 1800) -> str:
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return ''.join(f.readlines()[-n:])[-cap:]
    except Exception:
        return ''


def _latest_log(pattern: str, cap: int = 1800) -> str:
    files = sorted(glob.glob(os.path.join(_DIR, 'logs', pattern)))
    return _tail(files[-1], cap=cap) if files else ''


def _db_rows(platform: str, wallet: str) -> str:
    """Best-effort read of positions for the platform from the wallet's state.db."""
    try:
        import wallet_manager as _wm
        w = _wm.get_wallet(wallet) if wallet and wallet != 'default' else None
        db = os.path.join(_DIR, w['state_db']) if w and 'state_db' in w else \
             os.path.join(_DIR, 'state.db')
        c = sqlite3.connect(db)
        rows = c.execute(
            "SELECT id,platform,token,amount_wei,entry_date,expiry_date,status "
            "FROM positions WHERE platform=? ORDER BY id DESC LIMIT 10", (platform,)
        ).fetchall()
        c.close()
        return '\n'.join(str(r) for r in rows) or '(no rows)'
    except Exception as e:
        return f'(db read error: {e})'


def _platform_cfg(platform: str) -> str:
    try:
        with open(os.path.join(_DIR, 'config', 'contracts.json'), encoding='utf-8') as f:
            cfg = json.load(f)
        p = cfg.get('platforms', {}).get(platform)
        return json.dumps(p, ensure_ascii=False, indent=1) if p else '(platform not in config)'
    except Exception as e:
        return f'(cfg read error: {e})'


def _build_prompt(inc: dict) -> str:
    platform = inc.get('platform', '')
    return f"""An incident was detected by the watcher. Diagnose it.

INCIDENT
  signal:   {inc.get('signal')}
  platform: {platform}
  wallet:   {inc.get('wallet')}
  pos_id:   {inc.get('pos_id')}
  severity: {inc.get('severity')}
  seen:     {inc.get('count')}x over {len(inc.get('days_seen', []))} day(s)
  title:    {inc.get('title')}
  detail:   {inc.get('detail')}

PLATFORM CONFIG (config/contracts.json)
{_platform_cfg(platform)}

STATE.DB ROWS for {platform}
{_db_rows(platform, inc.get('wallet', 'default'))}

RECENT withdraw log tail
{_latest_log('withdraw_*.log') or '(none)'}

RECENT agent log tail
{_tail(os.path.join(_DIR, 'logs', 'agent.log')) or '(none)'}

Use Read/Grep/Glob to inspect the relevant source (e.g. the platform's module:
aave_supply.py, withdraw_all.py, executor.py) to confirm the mechanism, then give
your structured diagnosis."""


def _parse_diagnosis(text: str) -> dict:
    """Extract the last ```json {...}``` block from the agent's final message."""
    blocks = re.findall(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
    if not blocks:
        blocks = re.findall(r'(\{[^{}]*"root_cause".*?\})', text, re.DOTALL)
    if not blocks:
        return {}
    try:
        return json.loads(blocks[-1])
    except Exception:
        return {}


async def _run(inc: dict) -> dict:
    from claude_agent_sdk import (
        query, ClaudeAgentOptions, AssistantMessage, ResultMessage, TextBlock,
    )
    opts = ClaudeAgentOptions(
        model=MODEL,
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=_ALLOWED_TOOLS,
        disallowed_tools=['Bash', 'Edit', 'Write', 'NotebookEdit', 'WebFetch', 'WebSearch',
                          'Agent', 'Task'],
        permission_mode='default',
        cwd=_DIR,
        setting_sources=[],          # don't load the user's global CLAUDE.md — keep focused/cheap
        max_turns=MAX_TURNS,
        thinking={'type': 'disabled'},   # extended thinking adds ~40s/turn — not worth it here
    )
    final_text = []
    try:
        async for msg in query(prompt=_build_prompt(inc), options=opts):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        final_text.append(block.text)
    except Exception as e:
        # max-turns / process errors: keep whatever the agent already produced
        log.warning(f'query ended early ({e}) — parsing partial output')
    joined = '\n'.join(final_text)
    diag = _parse_diagnosis(joined)
    if diag:
        return diag
    return {'root_cause': (joined[:500] or 'no output'), 'category': 'unknown', 'confidence': 'low'}


def diagnose(incident_id: str) -> dict:
    """Synchronous entry point. Runs Sonnet diagnosis for one incident, writes the
    result back to the store, returns the diagnosis dict. Never raises — failures
    are recorded on the incident so the watcher loop stays alive."""
    data = store.get_all()
    inc  = next((i for i in data['incidents'] if i['id'] == incident_id), None)
    if inc is None:
        return {}

    store.set_agent_state('working')
    store.update(incident_id, status='investigating')
    try:
        diag = asyncio.run(asyncio.wait_for(_run(inc), timeout=TIMEOUT_S))
        sev = diag.get('severity') or inc.get('severity', 'warn')
        store.update(
            incident_id,
            status='diagnosed',
            severity=sev,
            proposal={
                'root_cause':     diag.get('root_cause', ''),
                'category':       diag.get('category', 'unknown'),
                'proposed_action': diag.get('proposed_action', ''),
                'auto_fixable':   bool(diag.get('auto_fixable', False)),
                'confidence':     diag.get('confidence', 'low'),
                'mode':           MODE,
            },
        )
        log.info(f'diagnosed {incident_id}: {diag.get("category")} — {diag.get("root_cause","")[:80]}')
        return diag
    except asyncio.TimeoutError:
        store.update(incident_id, status='detected',
                     proposal={'root_cause': f'diagnosis timed out after {TIMEOUT_S}s', 'category': 'unknown'})
        log.warning(f'diagnose {incident_id} timed out')
        return {}
    except Exception as e:
        store.update(incident_id, status='detected',
                     proposal={'root_cause': f'diagnosis error: {e}', 'category': 'unknown'})
        log.error(f'diagnose {incident_id} failed: {e}')
        return {}
    finally:
        store.set_agent_state('watching')


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if len(sys.argv) > 1:
        print(json.dumps(diagnose(sys.argv[1]), ensure_ascii=False, indent=2))
    else:
        # diagnose the first non-resolved, non-diagnosed incident
        d = store.get_all()
        pend = [i for i in d['incidents'] if i['status'] == 'detected']
        if not pend:
            print('no pending incidents')
        else:
            print(json.dumps(diagnose(pend[0]['id']), ensure_ascii=False, indent=2))
