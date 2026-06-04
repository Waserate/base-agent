"""Run the daily job immediately — for manual testing."""
import state
from agent import daily_job

state.init_db()
daily_job()
