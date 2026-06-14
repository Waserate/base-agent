"""
remediation_agent.py — Sonnet-powered incident diagnoser + live remediator.

Phase 2a (DIAGNOSE-ONLY, REMEDIATION_MODE=diagnose):
  - READ-ONLY. Allowed tools: Read, Grep, Glob only.
  - Produces: root_cause, category, proposed_action, auto_fixable in incident store.

Phase 2b (LIVE REMEDIATION, REMEDIATION_MODE=live):
  - After diagnosis: if category==state_drift + auto_fixable → run recovery commands.
  - Commands are a deterministic whitelist (no second AI call): withdraw_all, reconcile,
    sweep_tokens. Bounded to MAX_REMEDIATION_ACTIONS per incident.
  - Bot records every action in incident['actions'] for dashboard display.

Phase 3 (CODE-FIX, REMEDIATION_MODE=live):
  - category==code_bug → trigger code_fix_agent.run_fix() in a git worktree.
  - Sets incident status='needs_approval' + stores diff in proposal.

Auth: uses the local Claude Code subscription login (no API key needed).
"""

import os, re, json, glob, asyncio, logging, sqlite3, subprocess, sys
from datetime import date, datetime

import incident_store as store

log = logging.getLogger(__name__)

MODEL                    = os.getenv('REMEDIATION_MODEL', 'claude-sonnet-4-6')
MODE                     = os.getenv('REMEDIATION_MODE', 'diagnose')   # diagnose | live
TIMEOUT_S                = int(os.getenv('REMEDIATION_TIMEOUT_S', '300'))
MAX_TURNS                = int(os.getenv('REMEDIATION_MAX_TURNS', '20'))
MAX_REMEDIATION_ACTIONS  = 3   # hard cap per incident
_DIR                     = os.path.dirname(os.path.abspath(__file__))

_ALLOWED_TOOLS = ['Read', 'Grep', 'Glob']


def _resolve_db(wallet: str) -> str:
    """Resolve a wallet id to its state DB path. Falls back to last_active wallet
    when wallet is empty/'default' (legacy state.db no longer exists)."""
    import wallet_manager as _wm
    if not wallet or wallet in ('default', ''):
        wallet = _wm.get_last_active()
    w = _wm.get_wallet(wallet) if wallet else None
    db = (w or {}).get('state_db') or f'state_{wallet}.db'
    return os.path.join(_DIR, db)

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

CRITICAL — ON-CHAIN BALANCE FIELD: The prompt includes an "ON-CHAIN BALANCE" line \
that was fetched live just before you were called. If it says "balance = 0 *** GHOST \
POSITION ***", the position does NOT exist on-chain regardless of what the DB says. \
Classify as state_drift (proposed_action: "reconcile to close ghost DB row"), \
auto_fixable=true, confidence=high. Do NOT classify as external in this case.

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
        db = _resolve_db(wallet)
        c = sqlite3.connect(db)
        rows = c.execute(
            "SELECT id,platform,token,amount_wei,entry_date,expiry_date,status "
            "FROM positions WHERE platform=? ORDER BY id DESC LIMIT 10", (platform,)
        ).fetchall()
        c.close()
        return '\n'.join(str(r) for r in rows) or '(no rows)'
    except Exception as e:
        return f'(db read error: {e})'


def _onchain_balance(platform: str, wallet: str) -> str:
    """Live read-only on-chain check for the position's receipt token.
    Returns a one-line string injected into the diagnosis prompt.
    If balance = 0 → strong signal that the position is a ghost row (state_drift).

    Supported types:
      aave_supply   → atoken_address.balanceOf(wallet)
      ctoken        → address (mToken).balanceOf(wallet)          [Moonwell lend]
      erc4626       → address (vault).balanceOf(wallet)           [Fluid, Morpho]
      aave_borrow   → borrow_vdebt.balanceOf(wallet)              [vDebt = 0 → repaid]
      mw_borrow     → borrow_mtoken.borrowBalanceStored(wallet)   [debt = 0 → repaid]
    """
    try:
        with open(os.path.join(_DIR, 'config', 'contracts.json'), encoding='utf-8') as f:
            cfg = json.load(f)
        p = cfg.get('platforms', {}).get(platform)
        if not p or not isinstance(p, dict):
            return '(on-chain check: platform not in config)'

        ptype = p.get('type', '')

        from executor import w3 as _w3, WALLET as _DEFAULT_WALLET
        from web3 import Web3 as _Web3

        if wallet and wallet not in ('default', ''):
            try:
                import wallet_manager as _wm
                _w = _wm.get_wallet(wallet)
                wallet_addr = _w.get('address', _DEFAULT_WALLET)
            except Exception:
                wallet_addr = _DEFAULT_WALLET
        else:
            wallet_addr = _DEFAULT_WALLET

        _BAL_ABI = [{'name': 'balanceOf', 'type': 'function', 'stateMutability': 'view',
                     'inputs': [{'name': 'owner', 'type': 'address'}],
                     'outputs': [{'name': '', 'type': 'uint256'}]}]

        if ptype == 'aave_supply':
            token_addr = p.get('atoken_address')
            label = 'aToken'
            if not token_addr:
                return '(on-chain check: atoken_address missing)'
            balance = _w3.eth.contract(
                address=_Web3.to_checksum_address(token_addr), abi=_BAL_ABI
            ).functions.balanceOf(wallet_addr).call()

        elif ptype == 'ctoken':
            token_addr = p.get('address')
            label = 'mToken shares'
            if not token_addr:
                return '(on-chain check: address missing)'
            balance = _w3.eth.contract(
                address=_Web3.to_checksum_address(token_addr), abi=_BAL_ABI
            ).functions.balanceOf(wallet_addr).call()

        elif ptype == 'erc4626':
            token_addr = p.get('address')
            label = 'vault shares'
            if not token_addr:
                return '(on-chain check: address missing)'
            balance = _w3.eth.contract(
                address=_Web3.to_checksum_address(token_addr), abi=_BAL_ABI
            ).functions.balanceOf(wallet_addr).call()

        elif ptype == 'aave_borrow':
            # vDebt token: balanceOf = outstanding variable debt
            # if 0 → borrow fully repaid on-chain (position is ghost if DB still active)
            vdebt_addr = p.get('borrow_vdebt')
            label = 'vDebt (borrow outstanding)'
            if not vdebt_addr:
                return '(on-chain check: borrow_vdebt missing)'
            balance = _w3.eth.contract(
                address=_Web3.to_checksum_address(vdebt_addr), abi=_BAL_ABI
            ).functions.balanceOf(wallet_addr).call()

        elif ptype == 'mw_borrow':
            # borrowBalanceStored on borrow_mtoken = outstanding debt (includes accrued interest)
            # if 0 → debt fully repaid (position is ghost if DB still active)
            borrow_mtoken = p.get('borrow_mtoken')
            label = 'borrowBalanceStored (debt outstanding)'
            if not borrow_mtoken:
                return '(on-chain check: borrow_mtoken missing)'
            _BORROW_ABI = [{'name': 'borrowBalanceStored', 'type': 'function',
                            'stateMutability': 'view',
                            'inputs': [{'name': 'account', 'type': 'address'}],
                            'outputs': [{'name': '', 'type': 'uint256'}]}]
            balance = _w3.eth.contract(
                address=_Web3.to_checksum_address(borrow_mtoken), abi=_BORROW_ABI
            ).functions.borrowBalanceStored(wallet_addr).call()

        elif ptype in ('aero_lp', 'uni_lp', 'pancake_lp', 'beefy_lp'):
            # Gauge staked LP: balanceOf(wallet) on gauge contract
            gauge_addr = p.get('gauge_address')
            pool_addr  = p.get('pool_address') or p.get('address')
            label = 'gauge LP'
            if gauge_addr:
                balance = _w3.eth.contract(
                    address=_Web3.to_checksum_address(gauge_addr), abi=_BAL_ABI
                ).functions.balanceOf(wallet_addr).call()
                if balance == 0 and pool_addr:
                    # Also check unstaked LP in pool
                    pool_bal = _w3.eth.contract(
                        address=_Web3.to_checksum_address(pool_addr), abi=_BAL_ABI
                    ).functions.balanceOf(wallet_addr).call()
                    if pool_bal > 0:
                        return f'ON-CHAIN gauge=0 but pool LP={pool_bal} (unstaked — position exists)'
            elif pool_addr:
                label = 'pool LP (unstaked)'
                balance = _w3.eth.contract(
                    address=_Web3.to_checksum_address(pool_addr), abi=_BAL_ABI
                ).functions.balanceOf(wallet_addr).call()
            else:
                return '(on-chain check: no gauge_address or pool_address in config)'

        elif ptype == 'aero_vote':
            # VotingEscrow.locked(tokenId) → (int128 amount, uint256 end)
            # amount=0 → lock withdrawn (ghost); end<now → expired (unlock available)
            ve_addr   = p.get('address')
            if not ve_addr:
                return '(on-chain check: aero_vote address missing in config)'
            # tokenId is stored as first segment of amount_wei "tokenId|aeroWei"
            import sqlite3 as _sq, glob as _glob
            token_id = None
            for _db in _glob.glob(os.path.join(_DIR, 'state*.db')):
                try:
                    _conn = _sq.connect(_db)
                    _rows = _conn.execute(
                        "SELECT amount_wei FROM positions WHERE platform='aero_vote' AND status='active'"
                    ).fetchall()
                    _conn.close()
                    for (_aw,) in _rows:
                        if _aw and '|' in str(_aw):
                            token_id = int(str(_aw).split('|')[0])
                            break
                except Exception:
                    pass
                if token_id is not None:
                    break
            if token_id is None:
                return '(on-chain check: could not find veAERO tokenId in DB)'
            _VE_ABI = [{'name': 'locked', 'type': 'function', 'stateMutability': 'view',
                        'inputs': [{'name': '_tokenId', 'type': 'uint256'}],
                        'outputs': [{'name': 'amount', 'type': 'int128'},
                                    {'name': 'end',    'type': 'uint256'}]}]
            result = _w3.eth.contract(
                address=_Web3.to_checksum_address(ve_addr), abi=_VE_ABI
            ).functions.locked(token_id).call()
            locked_amount, locked_end = result[0], result[1]
            import time as _time
            now_ts = int(_time.time())
            if locked_amount == 0:
                return (f'ON-CHAIN veAERO tokenId={token_id}: amount=0 '
                        f'*** GHOST POSITION — lock withdrawn on-chain ***')
            elif locked_end < now_ts:
                return (f'ON-CHAIN veAERO tokenId={token_id}: EXPIRED (end={locked_end}, now={now_ts}) '
                        f'— lock can be withdrawn, position still exists on-chain')
            else:
                import datetime as _dt
                exp_str = _dt.datetime.utcfromtimestamp(locked_end).strftime('%Y-%m-%d')
                return (f'ON-CHAIN veAERO tokenId={token_id}: locked amount={locked_amount} '
                        f'expires={exp_str} (position EXISTS and active on-chain)')
            # veAERO: don't use the generic balance==0 path below
            return ''

        else:
            return f'(on-chain check: type={ptype} not supported — check manually)'

        if balance == 0:
            return (f'ON-CHAIN {label} = 0 '
                    f'*** GHOST POSITION — position does NOT exist on-chain ***')
        else:
            return f'ON-CHAIN {label} = {balance} (position EXISTS on-chain)'
    except Exception as e:
        return f'(on-chain check failed: {e})'


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
    wallet   = inc.get('wallet', 'default')
    return f"""An incident was detected by the watcher. Diagnose it.

INCIDENT
  signal:   {inc.get('signal')}
  platform: {platform}
  wallet:   {wallet}
  pos_id:   {inc.get('pos_id')}
  severity: {inc.get('severity')}
  seen:     {inc.get('count')}x over {len(inc.get('days_seen', []))} day(s)
  title:    {inc.get('title')}
  detail:   {inc.get('detail')}

ON-CHAIN BALANCE CHECK (live, fetched now)
  {_onchain_balance(platform, wallet)}

PLATFORM CONFIG (config/contracts.json)
{_platform_cfg(platform)}

STATE.DB ROWS for {platform}
{_db_rows(platform, wallet)}

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


def _ghost_check(inc: dict) -> dict | None:
    """Hard on-chain gate — runs BEFORE Sonnet.
    If balance = 0 → position gone on-chain → return deterministic state_drift result.
    Returns dict on ghost, None if position still exists or check inconclusive."""
    platform = inc.get('platform', '')
    wallet   = inc.get('wallet', '')
    pos_id   = inc.get('pos_id')
    if not platform:
        return None
    result = _onchain_balance(platform, wallet)
    if 'GHOST POSITION' in result:
        log.info(f'  [ghost-gate] {platform}#{pos_id}: {result[:80]}')
        return {
            'category':        'state_drift',
            'root_cause':      f'on-chain balance = 0 — position no longer exists on-chain. {result}',
            'proposed_action': f'close_ghost_row: state.close_position({pos_id})',
            'auto_fixable':    True,
            'confidence':      'high',
            'severity':        'warn',
        }
    return None


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
        # Hard gate: check on-chain before spending Sonnet tokens.
        # If balance = 0 → ghost row, deterministic result, skip LLM.
        ghost = _ghost_check(inc)
        if ghost:
            diag = ghost
        else:
            diag = asyncio.run(asyncio.wait_for(_run(inc), timeout=TIMEOUT_S))
        sev = diag.get('severity') or inc.get('severity', 'warn')
        cat        = diag.get('category', 'unknown')
        auto_fix   = bool(diag.get('auto_fixable', False))
        confidence = diag.get('confidence', 'low')
        store.update(
            incident_id,
            status='diagnosed',
            severity=sev,
            # top-level for _run_remediations() sweep
            category=cat,
            auto_fixable=auto_fix,
            confidence=confidence,
            proposal={
                'root_cause':      diag.get('root_cause', ''),
                'category':        cat,
                'proposed_action': diag.get('proposed_action', ''),
                'auto_fixable':    auto_fix,
                'confidence':      confidence,
                'mode':            MODE,
            },
        )
        log.info(f'diagnosed {incident_id}: {cat} — {diag.get("root_cause","")[:80]}')
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


# ── Phase 2b: deterministic command mapper ───────────────────────────────────

def _wallet_env(wallet: str) -> dict:
    """Extra env vars to run a script in the correct wallet context.
    Sets WALLET_ID and STATE_DB_PATH so state.py uses the right DB."""
    env = os.environ.copy()
    if wallet and wallet not in ('default', ''):
        env['WALLET_ID'] = wallet
        try:
            import wallet_manager as _wm
            w = _wm.get_wallet(wallet)
            db = w.get('state_db')
            if db:
                env['STATE_DB_PATH'] = os.path.join(_DIR, db)
        except Exception:
            pass
    return env


def _run_cmd(cmd: list[str], wallet: str, timeout: int = 120) -> tuple[bool, str]:
    """Run a subprocess command; return (success, output_tail)."""
    try:
        r = subprocess.run(
            cmd, cwd=_DIR, env=_wallet_env(wallet),
            capture_output=True, text=True, timeout=timeout,
        )
        out = (r.stdout + r.stderr)[-800:]
        return r.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, f'timeout after {timeout}s'
    except Exception as e:
        return False, str(e)


def _map_to_commands(proposal: dict, inc: dict) -> list[dict]:
    """
    Map a diagnosis proposal to a whitelist of safe recovery commands.

    Returns list of {cmd: [args…], desc: str}.
    Only state_drift + auto_fixable incidents are mapped; everything else → [].
    """
    if proposal.get('category') != 'state_drift' or not proposal.get('auto_fixable'):
        return []

    action  = (proposal.get('proposed_action') or '').lower()
    pos_id  = inc.get('pos_id')
    wallet  = inc.get('wallet', 'default')
    cmds    = []

    # withdraw_all --id N  (stuck / ghost position with known id)
    if pos_id and ('withdraw' in action or 'withdraw_all' in action):
        cmds.append({
            'cmd':  [sys.executable, 'withdraw_all.py', '--id', str(pos_id)],
            'desc': f'withdraw_all --id {pos_id}',
        })

    # ghost / orphan DB row — close the specific pos_id directly
    # (reconcile() has auto-close disabled due to RPC-flake risk)
    if 'reconcile' in action or 'ghost' in action or 'orphan' in action or 'close' in action:
        if pos_id:
            cmds.append({
                'cmd':  [sys.executable, '-c',
                         f'import state; state.close_position({pos_id}); '
                         f'print("closed pos#{pos_id}")'],
                'desc': f'state.close_position({pos_id}) — close ghost DB row',
            })
        else:
            cmds.append({
                'cmd':  [sys.executable, '-c', 'import onchain_recovery as r; r.reconcile()'],
                'desc': 'onchain_recovery.reconcile() (no pos_id — fallback scan)',
            })

    # sweep residual tokens → ETH
    if 'sweep' in action or 'token' in action:
        cmds.append({
            'cmd':  [sys.executable, 'sweep_tokens.py'],
            'desc': 'sweep_tokens',
        })

    # fallback: proposed_action mentions reconcile but no pos_id and no cmd matched
    if not cmds and 'reconcile' in action:
        cmds.append({
            'cmd':  [sys.executable, '-c', 'import onchain_recovery as r; r.reconcile()'],
            'desc': 'onchain_recovery.reconcile() (fallback scan — no pos_id)',
        })

    return cmds[:MAX_REMEDIATION_ACTIONS]


def _verify_remediation(inc: dict) -> bool:
    """After running recovery commands, verify the problem is actually gone.
    Returns True if fixed (or unverifiable), False if still broken → re-open."""
    proposal = inc.get('proposal') or {}
    category = proposal.get('category', 'unknown')
    pos_id   = inc.get('pos_id')
    wallet   = inc.get('wallet', 'default')

    if category == 'state_drift' and pos_id:
        # Check the DB row in the CORRECT wallet DB
        try:
            db_path = _resolve_db(wallet)
            import sqlite3
            c = sqlite3.connect(db_path)
            rows = c.execute(
                "SELECT status FROM positions WHERE id=?", (pos_id,)
            ).fetchall()
            c.close()
            if not rows:
                return True   # row gone = fixed
            still_active = rows[0][0] == 'active'
            if still_active:
                log.warning(f'  verify: pos#{pos_id} still active in {db_path}')
            return not still_active
        except Exception as e:
            log.warning(f'  verify: DB check failed ({e}) — assuming ok')
            return True

    # For on-chain balance check: re-run _onchain_balance
    platform = inc.get('platform', '')
    if platform and category == 'state_drift':
        result = _onchain_balance(platform, wallet)
        if 'GHOST' in result:
            log.warning(f'  verify: on-chain still shows ghost for {platform}')
            return False

    return True   # can't verify — assume ok


def remediate(incident_id: str) -> dict:
    """
    Phase 2b: run deterministic recovery commands for a diagnosed state_drift incident.

    Only executes when REMEDIATION_MODE=live AND category=state_drift AND auto_fixable.
    Records every action taken in the incident store.
    Returns {'status': 'done'|'skip'|'error', 'actions': [...]}.
    """
    if MODE != 'live':
        return {'status': 'skip', 'reason': 'REMEDIATION_MODE != live'}

    data = store.get_all()
    inc  = next((i for i in data['incidents'] if i['id'] == incident_id), None)
    if inc is None:
        return {'status': 'skip', 'reason': 'incident not found'}

    proposal = inc.get('proposal') or {}
    category = proposal.get('category', 'unknown')

    if category == 'code_bug':
        # Phase 3: hand off to code_fix_agent
        try:
            import code_fix_agent
            code_fix_agent.run_fix(incident_id)
        except Exception as e:
            log.error(f'code_fix_agent failed for {incident_id}: {e}')
        return {'status': 'code_fix_queued'}

    cmds = _map_to_commands(proposal, inc)
    if not cmds:
        return {'status': 'skip', 'reason': f'no recoverable commands for category={category}'}

    log.info(f'[Phase 2b] remediating {incident_id}: {len(cmds)} command(s)')
    store.set_agent_state('working')
    store.update(incident_id, status='remediating')

    actions_taken = []
    all_ok = True
    try:
        for entry in cmds:
            log.info(f'  run: {" ".join(entry["cmd"])}')
            ok, out = _run_cmd(entry['cmd'], wallet=inc.get('wallet', 'default'))
            actions_taken.append({
                'ts':      datetime.now().isoformat(timespec='seconds'),
                'desc':    entry['desc'],
                'ok':      ok,
                'output':  out[-400:],
            })
            log.info(f'  -> {"OK" if ok else "FAIL"}: {out[-120:]}')
            if not ok:
                all_ok = False
                break   # stop on first failure — don't cascade

        # Verify fix actually took — exit code 0 is not enough
        if all_ok:
            all_ok = _verify_remediation(inc)
            if not all_ok:
                log.warning(f'[Phase 2b] {incident_id}: commands exited 0 but verify FAILED — re-opening')

        final_status = 'resolved' if all_ok else 'detected'  # re-open if failed
        store.update(incident_id,
                     status=final_status,
                     actions=actions_taken)
        log.info(f'[Phase 2b] {incident_id} -> {final_status}')
        return {'status': 'done' if all_ok else 'partial', 'actions': actions_taken}
    except Exception as e:
        log.error(f'remediate {incident_id} error: {e}')
        store.update(incident_id, status='detected', actions=actions_taken)
        return {'status': 'error', 'error': str(e)}
    finally:
        store.set_agent_state('watching')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == 'remediate' and len(sys.argv) > 2:
            print(json.dumps(remediate(sys.argv[2]), ensure_ascii=False, indent=2))
        else:
            print(json.dumps(diagnose(cmd), ensure_ascii=False, indent=2))
    else:
        # diagnose the first non-resolved, non-diagnosed incident
        d = store.get_all()
        pend = [i for i in d['incidents'] if i['status'] == 'detected']
        if not pend:
            print('no pending incidents')
        else:
            print(json.dumps(diagnose(pend[0]['id']), ensure_ascii=False, indent=2))
