import sys, os, sqlite3, json, datetime
con = sqlite3.connect(r"C:\JSE\settings\job_applications.db")
q = lambda s: con.execute(s).fetchone()[0]
print(json.dumps({
 "ts": datetime.datetime.now().isoformat(timespec="seconds"),
 "new_total": q("SELECT COUNT(*) FROM jobs WHERE pipeline_stage='new'"),
 "new_scored": q("SELECT COUNT(*) FROM jobs WHERE pipeline_stage='new' AND match_score IS NOT NULL"),
 "new_unscored": q("SELECT COUNT(*) FROM jobs WHERE pipeline_stage='new' AND match_score IS NULL"),
}))
