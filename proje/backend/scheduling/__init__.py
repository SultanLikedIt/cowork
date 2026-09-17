"""Scheduled and watch tasks: saved prompts the backend runs on its own.

cron.py     five-field cron in a named timezone
store.py    SQLite access (scheduled_tasks, scheduled_runs)
prompts.py  {tarih}/{hafta}/{ay}/{son_calisma} and previous-run context
watch.py    change detection (url_hash, research_diff)
runner.py   the scheduler loop; starts runs through main.submit_job
api.py      /scheduled-tasks routes
config.py   SCHEDULE_* environment settings
"""
