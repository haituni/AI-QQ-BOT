"""Durable daily usage from provider-reported counters; never guessed billing."""
from __future__ import annotations
import time
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
TZ=ZoneInfo('Asia/Shanghai')


def init_usage(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS token_daily(
        day TEXT,model TEXT,purpose TEXT,calls INTEGER DEFAULT 0,reported INTEGER DEFAULT 0,
        input_tokens INTEGER DEFAULT 0,output_tokens INTEGER DEFAULT 0,cache_hit INTEGER DEFAULT 0,
        cache_miss INTEGER DEFAULT 0,cache_reported INTEGER DEFAULT 0,reasoning INTEGER DEFAULT 0,
        PRIMARY KEY(day,model,purpose));''')
    # Historical metrics lack model/cache dimensions; preserve only the counters actually recorded.
    if not db.execute("SELECT 1 FROM memory_control WHERE key='usage_migrated'").fetchone():
        for r in db.execute('SELECT time,input_tokens,output_tokens FROM model_metrics').fetchall():
            record(db,({'prompt_tokens':r[1],'completion_tokens':r[2]} if r[1]+r[2]>0 else {}),'历史记录（型号未知）','legacy',r[0])
        db.execute("INSERT INTO memory_control VALUES('usage_migrated','true')")
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='token_by_chat'").fetchone():
        db.execute('''CREATE TABLE token_by_chat(
            day TEXT,model TEXT,purpose TEXT,calls INTEGER,reported INTEGER,input_tokens INTEGER,
            output_tokens INTEGER,cache_hit INTEGER,cache_miss INTEGER,cache_reported INTEGER,reasoning INTEGER,
            chat TEXT NOT NULL DEFAULT '',PRIMARY KEY(day,model,purpose,chat))''')
        db.execute("INSERT INTO token_by_chat SELECT *,'' FROM token_daily")
    db.commit()


def number(value):return value if type(value)is int and 0<=value<=10**9 else 0


def record(db,usage,model,purpose,stamp=None,*,chat=""):
    day=datetime.fromtimestamp(stamp if stamp is not None else time.time(),TZ).strftime('%Y-%m-%d')
    reported=int(all(type(usage.get(k))is int and 0<=usage[k]<=10**9 for k in ('prompt_tokens','completion_tokens')))
    inp=number(usage.get('prompt_tokens'));out=number(usage.get('completion_tokens'))
    cache=int(reported and all(type(usage.get(k))is int and 0<=usage[k]<=inp for k in ('prompt_cache_hit_tokens','prompt_cache_miss_tokens')) and usage.get('prompt_cache_hit_tokens',0)+usage.get('prompt_cache_miss_tokens',0)==inp)
    hit=number(usage.get('prompt_cache_hit_tokens')) if cache else 0
    miss=number(usage.get('prompt_cache_miss_tokens')) if cache else 0
    details=usage.get('completion_tokens_details',{})
    reason=number(details.get('reasoning_tokens')) if isinstance(details,dict) else 0
    db.execute('''INSERT INTO token_daily VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(day,model,purpose) DO UPDATE SET
        calls=calls+excluded.calls,reported=reported+excluded.reported,input_tokens=input_tokens+excluded.input_tokens,
        output_tokens=output_tokens+excluded.output_tokens,cache_hit=cache_hit+excluded.cache_hit,cache_miss=cache_miss+excluded.cache_miss,
        cache_reported=cache_reported+excluded.cache_reported,reasoning=reasoning+excluded.reasoning''',
        (day,str(model)[:100],str(purpose)[:40],1,reported,inp,out,hit,miss,cache,reason))


    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='token_by_chat'").fetchone():
        db.execute('''INSERT INTO token_by_chat VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(day,model,purpose,chat) DO UPDATE SET
            calls=calls+excluded.calls,reported=reported+excluded.reported,input_tokens=input_tokens+excluded.input_tokens,
            output_tokens=output_tokens+excluded.output_tokens,cache_hit=cache_hit+excluded.cache_hit,
            cache_miss=cache_miss+excluded.cache_miss,cache_reported=cache_reported+excluded.cache_reported,
            reasoning=reasoning+excluded.reasoning''',
            (day,str(model)[:100],str(purpose)[:40],1,reported,inp,out,hit,miss,cache,reason,str(chat)))


def report(db,days=7,chat=None):
    if type(days)is not int or days not in (1,7,30,90):raise ValueError('请选择今日、7天、30天或90天')
    today=datetime.now(TZ).date();start=today-timedelta(days=days-1)
    fields='coalesce(sum(calls),0) calls,coalesce(sum(reported),0) reported,coalesce(sum(input_tokens),0) input_tokens,coalesce(sum(output_tokens),0) output_tokens,coalesce(sum(cache_hit),0) cache_hit,coalesce(sum(cache_miss),0) cache_miss,coalesce(sum(cache_reported),0) cache_reported,coalesce(sum(reasoning),0) reasoning'
    where='day>=?';args=[str(start)]
    if chat is not None:
        where+=' AND chat=?';args.append(chat)
    total=dict(db.execute('SELECT '+fields+' FROM token_by_chat WHERE '+where,args).fetchone())
    rows={r['day']:dict(r) for r in db.execute('SELECT day,'+fields+' FROM token_by_chat WHERE '+where+' GROUP BY day ORDER BY day',args)}
    daily=[rows.get(str(start+timedelta(days=i)),{'day':str(start+timedelta(days=i)),'input_tokens':0,'output_tokens':0,'calls':0}) for i in range(days)]
    models=[dict(r) for r in db.execute('SELECT model,'+fields+' FROM token_by_chat WHERE '+where+' GROUP BY model',args)]
    purposes=[dict(r) for r in db.execute('SELECT purpose,'+fields+' FROM token_by_chat WHERE '+where+' GROUP BY purpose',args)]
    conversations=[dict(r) for r in db.execute('SELECT chat,'+fields+' FROM token_by_chat WHERE day>=? GROUP BY chat ORDER BY sum(input_tokens+output_tokens) DESC',(str(start),))]
    return {'days':days,'chat':chat,'total':total,'daily':daily,'models':models,'purposes':purposes,'conversations':conversations,'timezone':'Asia/Shanghai'}
