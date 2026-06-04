"""Force a new action at 14:00 BKK today by regenerating plan."""
import json, os
from datetime import date, datetime as dt, timedelta
from daily_briefing import plan_day, get_plan_file

actions = plan_day()
if not actions:
    print('plan_day returned empty — no eligible platforms')
    exit(1)

today = date.today()
target_bkk = '14:00'
target_utc = (dt.combine(today, dt.strptime(target_bkk, '%H:%M').time()) - timedelta(hours=7)).isoformat()

# Patch first undone action to 14:00
actions[0]['time_bkk']   = target_bkk
actions[0]['run_at_utc'] = target_utc
actions[0]['done']       = False

# If multiple actions, spread the rest after 14:00
for i in range(1, len(actions)):
    later_m = 14*60 + 30*i  # 14:30, 15:00, ...
    h, m = divmod(later_m, 60)
    bkk_t = f'{h:02d}:{m:02d}'
    utc_t = (dt.combine(today, dt.strptime(bkk_t, '%H:%M').time()) - timedelta(hours=7)).isoformat()
    actions[i]['time_bkk']   = bkk_t
    actions[i]['run_at_utc'] = utc_t
    actions[i]['done']       = False

_pf = get_plan_file()
os.makedirs(os.path.dirname(_pf), exist_ok=True)
with open(_pf, 'w') as f:
    json.dump({'date': today.isoformat(), 'actions': actions}, f, indent=2)

print(f'Plan saved — {len(actions)} action(s):')
for a in actions:
    print(f"  {a['time_bkk']}  {a['disp_type']:<7} {a['display_name']}  ${a['usd_est']:.2f}")
print('plan_sync_job will pick this up within 60s')
