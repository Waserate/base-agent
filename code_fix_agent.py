"""
code_fix_agent.py — Phase 3: Sonnet-powered code-fix for code_bug incidents.

When the watcher diagnoses category=code_bug, this module:
  1. Creates a git worktree at cache/fix-{inc_id}/
  2. Runs Sonnet (claude-agent-sdk, subscription auth) inside the worktree.
     Allowed tools: Read, Grep, Glob, Edit, Write, Bash (py_compile only).
  3. Agent reads the incident + source, patches the specific file(s).
  4. Runs py_compile verification on changed files.
  5. Computes git diff, stores it in incident proposal.patch_diff.
  6. Sets incident status = needs_approval.

Dashboard then shows the diff with Approve / Reject buttons.
On Approve: files are copied from worktree → repo root → worktree removed.
On Reject:  worktree removed, incident marked dismissed.

SAFETY:
  - Sonnet runs in the worktree, not the live repo — no risk to running agent.
  - Bash is technically allowed but system prompt restricts to py_compile.
  - No TX-sending tools available; no executor/state imports in the agent context.
  - Human must approve before any file in the live repo changes.
"""

import os, re, json, asyncio, logging, subprocess, sys, shutil
from datetime import datetime

import incident_store as store

log = logging.getLogger(__name__)

_DIR       = os.path.dirname(os.path.abspath(__file__))
MODEL      = os.getenv('REMEDIATION_MODEL', 'claude-sonnet-4-6')
TIMEOUT_S  = int(os.getenv('FIX_TIMEOUT_S', '600'))
MAX_TURNS  = int(os.getenv('FIX_MAX_TURNS', '30'))

SYSTEM_PROMPT = """You are a surgical code-fix agent for the Base-chain airdrop bot.
A specific bug was diagnosed. Your job: fix it with the MINIMAL change.

RULES:
1. Read the incident description carefully — it tells you the file, the mechanism, and the fix.
2. Use Read/Grep/Glob to confirm the exact line(s) before touching anything.
3. Use Edit to apply ONE targeted fix. Never rewrite whole functions.
4. After editing: verify syntax with Bash:
   python -c "import py_compile; py_compile.compile('<file>', doraise=True)"
   If that fails, fix it before finishing.
5. DO NOT run agent.py, withdraw_all.py, or any script that touches funds/blockchain.
6. DO NOT change logic beyond the stated bug. No refactors, no extra cleanup.
7. If you cannot identify the exact bug from the provided context, emit the json block
   with fixed=false and a clear reason — do not guess.

When done, emit ONE fenced json block:
```json
{"fixed": true, "files_changed": ["path/to/file.py"], "fix_summary": "one sentence"}
```
or if you cannot fix it:
```json
{"fixed": false, "files_changed": [], "fix_summary": "reason you cannot fix it"}
```
Nothing after the json block."""


def _worktree_path(incident_id: str) -> str:
    return os.path.join(_DIR, 'cache', f'fix-{incident_id}')


def _create_worktree(incident_id: str) -> str:
    """Create a git worktree for the fix. Returns the worktree path."""
    wt = _worktree_path(incident_id)
    if os.path.exists(wt):
        shutil.rmtree(wt, ignore_errors=True)
    r = subprocess.run(
        ['git', 'worktree', 'add', '--detach', wt, 'HEAD'],
        cwd=_DIR, capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f'git worktree add failed: {r.stderr.strip()}')
    log.info(f'worktree created at {wt}')
    return wt


def _remove_worktree(incident_id: str):
    wt = _worktree_path(incident_id)
    try:
        subprocess.run(['git', 'worktree', 'remove', '--force', wt],
                       cwd=_DIR, capture_output=True)
    except Exception:
        pass
    shutil.rmtree(wt, ignore_errors=True)


def _get_diff(worktree: str) -> str:
    """Get unified diff of changes made in the worktree vs HEAD."""
    r = subprocess.run(
        ['git', 'diff', 'HEAD'],
        cwd=worktree, capture_output=True, text=True,
    )
    return r.stdout.strip()


def _changed_files(worktree: str) -> list[str]:
    """Files actually changed in the worktree per git — the source of truth for
    apply_fix (do NOT trust the agent's self-reported files_changed, which can
    omit or misname a file it edited). Includes tracked modifications + new
    untracked files."""
    files = []
    r = subprocess.run(['git', 'diff', '--name-only', 'HEAD'],
                       cwd=worktree, capture_output=True, text=True)
    files += [l.strip() for l in r.stdout.splitlines() if l.strip()]
    # untracked (newly created) files
    r2 = subprocess.run(['git', 'ls-files', '--others', '--exclude-standard'],
                        cwd=worktree, capture_output=True, text=True)
    files += [l.strip() for l in r2.stdout.splitlines() if l.strip()]
    return sorted(set(files))


def _build_fix_prompt(inc: dict) -> str:
    proposal = inc.get('proposal') or {}
    return f"""A code_bug incident was diagnosed. Fix it.

INCIDENT
  id:       {inc['id']}
  platform: {inc.get('platform', '—')}
  wallet:   {inc.get('wallet', 'default')}
  title:    {inc.get('title', '—')}
  detail:   {inc.get('detail', '—')}

DIAGNOSIS
  root_cause:      {proposal.get('root_cause', '—')}
  proposed_action: {proposal.get('proposed_action', '—')}
  confidence:      {proposal.get('confidence', '—')}

You are working in a git worktree copy of the repo (safe sandbox — NOT the live directory).
The running agent is unaffected by changes here until a human approves.

Start by using Grep to find the exact line(s) mentioned in root_cause/proposed_action.
Make the minimal fix. Verify with py_compile. Emit the json result block."""


def _parse_result(text: str) -> dict:
    blocks = re.findall(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
    if not blocks:
        blocks = re.findall(r'(\{[^{}]*"fixed".*?\})', text, re.DOTALL)
    if not blocks:
        return {'fixed': False, 'fix_summary': 'agent produced no json result'}
    try:
        return json.loads(blocks[-1])
    except Exception:
        return {'fixed': False, 'fix_summary': 'json parse error'}


async def _run_fix_agent(inc: dict, worktree: str) -> dict:
    from claude_agent_sdk import (
        query, ClaudeAgentOptions, AssistantMessage, TextBlock,
    )
    opts = ClaudeAgentOptions(
        model=MODEL,
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=['Read', 'Grep', 'Glob', 'Edit', 'Write', 'Bash'],
        disallowed_tools=['Agent', 'Task', 'WebFetch', 'WebSearch', 'NotebookEdit'],
        # bypassPermissions: headless SDK has no TTY/callback, so 'default' would
        # auto-DENY Edit/Write/Bash and the agent could never apply a patch. The
        # worktree is an isolated sandbox and the allowlist has no fund/network
        # tools, so bypassing the interactive prompt here is safe.
        permission_mode='bypassPermissions',
        cwd=worktree,
        setting_sources=[],
        max_turns=MAX_TURNS,
        thinking={'type': 'disabled'},
    )
    final_text = []
    try:
        async for msg in query(prompt=_build_fix_prompt(inc), options=opts):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        final_text.append(block.text)
    except Exception as e:
        log.warning(f'fix agent ended early: {e}')
    return _parse_result('\n'.join(final_text))


def run_fix(incident_id: str) -> dict:
    """
    Synchronous entry point. Creates worktree, runs Sonnet fix agent,
    stores diff + result in incident store, sets status=needs_approval.
    Returns summary dict. Never raises.
    """
    data = store.get_all()
    inc  = next((i for i in data['incidents'] if i['id'] == incident_id), None)
    if inc is None:
        log.warning(f'run_fix: incident {incident_id} not found')
        return {'status': 'skip', 'reason': 'not found'}

    proposal = inc.get('proposal') or {}
    if proposal.get('category') != 'code_bug':
        return {'status': 'skip', 'reason': 'not a code_bug incident'}

    store.set_agent_state('working')
    store.update(incident_id, status='investigating')

    worktree = None
    try:
        worktree = _create_worktree(incident_id)
        result   = asyncio.run(
            asyncio.wait_for(_run_fix_agent(inc, worktree), timeout=TIMEOUT_S)
        )
        diff  = _get_diff(worktree)
        files = _changed_files(worktree)   # git truth, not agent self-report

        # Update incident with patch info
        updated_proposal = dict(proposal)
        updated_proposal.update({
            'patch_diff':     diff or '(no changes)',
            'patch_worktree': worktree,
            'patch_files':    files,
            'patch_summary':  result.get('fix_summary', ''),
            'patch_ok':       result.get('fixed', False),
        })

        # gate on REAL changes (git), not just the agent's self-reported flag
        if files and diff:
            store.update(incident_id,
                         status='needs_approval',
                         proposal=updated_proposal)
            log.info(f'[Phase 3] {incident_id}: patch ready — needs approval')
        else:
            # agent couldn't fix it — keep worktree for debugging but mark detected
            store.update(incident_id,
                         status='detected',
                         proposal=updated_proposal)
            _remove_worktree(incident_id)
            log.info(f'[Phase 3] {incident_id}: agent could not fix — {result.get("fix_summary")}')

        return {'status': 'needs_approval' if (files and diff) else 'unfixable',
                'result': result, 'files': files}

    except asyncio.TimeoutError:
        store.update(incident_id, status='detected')
        log.warning(f'run_fix {incident_id} timed out after {TIMEOUT_S}s')
        if worktree:
            _remove_worktree(incident_id)
        return {'status': 'timeout'}
    except Exception as e:
        store.update(incident_id, status='detected')
        log.error(f'run_fix {incident_id} error: {e}')
        if worktree:
            _remove_worktree(incident_id)
        return {'status': 'error', 'error': str(e)}
    finally:
        store.set_agent_state('watching')


def apply_fix(incident_id: str) -> dict:
    """
    Apply an approved patch: copy changed files from worktree to live repo.
    Called by serve_dashboard.py POST /api/fix_approve.
    Returns {'ok': bool, 'files': [...], 'error': str|None}.
    """
    data = store.get_all()
    inc  = next((i for i in data['incidents'] if i['id'] == incident_id), None)
    if inc is None:
        return {'ok': False, 'error': 'incident not found'}

    proposal = inc.get('proposal') or {}
    if inc.get('status') != 'needs_approval':
        return {'ok': False, 'error': f'incident status is {inc.get("status")}, not needs_approval'}

    worktree = proposal.get('patch_worktree')
    files    = proposal.get('patch_files', [])

    if not worktree or not os.path.exists(worktree):
        return {'ok': False, 'error': 'worktree missing — patch cannot be applied'}
    if not files:
        return {'ok': False, 'error': 'no files recorded in patch'}

    applied = []
    try:
        for rel_path in files:
            src = os.path.join(worktree, rel_path)
            dst = os.path.join(_DIR, rel_path)
            if not os.path.exists(src):
                log.warning(f'apply_fix: {src} not found, skipping')
                continue
            # backup original
            if os.path.exists(dst):
                shutil.copy2(dst, dst + '.bak')
            else:
                os.makedirs(os.path.dirname(dst) or '.', exist_ok=True)
            shutil.copy2(src, dst)
            applied.append(rel_path)
            log.info(f'apply_fix: copied {rel_path}')

        _remove_worktree(incident_id)
        store.update(incident_id, status='resolved',
                     actions=(inc.get('actions') or []) + [{
                         'ts':   datetime.now().isoformat(timespec='seconds'),
                         'desc': f'patch applied: {", ".join(applied)}',
                         'ok':   True,
                     }])
        log.info(f'[Phase 3] patch applied for {incident_id}: {applied}')
        return {'ok': True, 'files': applied}
    except Exception as e:
        log.error(f'apply_fix {incident_id}: {e}')
        return {'ok': False, 'error': str(e)}


def reject_fix(incident_id: str) -> dict:
    """Reject a pending patch — remove worktree, mark incident dismissed."""
    data = store.get_all()
    inc  = next((i for i in data['incidents'] if i['id'] == incident_id), None)
    if inc:
        _remove_worktree(incident_id)
        store.update(incident_id, status='resolved',
                     actions=(inc.get('actions') or []) + [{
                         'ts':   datetime.now().isoformat(timespec='seconds'),
                         'desc': 'patch rejected by user',
                         'ok':   False,
                     }])
    return {'ok': True}


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if len(sys.argv) < 2:
        print('usage: python code_fix_agent.py <incident_id>')
        print('       python code_fix_agent.py apply <incident_id>')
        print('       python code_fix_agent.py reject <incident_id>')
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == 'apply':
        print(json.dumps(apply_fix(sys.argv[2]), ensure_ascii=False, indent=2))
    elif cmd == 'reject':
        print(json.dumps(reject_fix(sys.argv[2]), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(run_fix(cmd), ensure_ascii=False, indent=2))
