"""One provider vision probe with a synthetic image; no QQ or private content."""
import asyncio
import base64
import json
from pathlib import Path
import sqlite3
import struct
import sys
import time
import zlib
from datetime import datetime
from zoneinfo import ZoneInfo
import aiohttp

ROOT=next(parent for parent in Path(__file__).resolve().parents if (parent / 'pyproject.toml').is_file())
(ROOT / 'logs/reports').mkdir(parents=True, exist_ok=True)
sys.path.insert(0,str(ROOT))
from bot.usage import record

async def main():
    cfg=json.loads((ROOT/'config/settings.json').read_text())
    width,height=200,100
    pixels=bytearray()
    for y in range(height):
        pixels.append(0)
        for x in range(width):pixels.extend((255,0,0) if x<width//2 else (0,0,255))
    def chunk(kind,data):return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
    png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(pixels))+chunk(b'IEND',b'')
    day=datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d')
    db=sqlite3.connect(ROOT/'data/memory.sqlite3',timeout=5)
    db.execute('BEGIN IMMEDIATE')
    row=db.execute('SELECT calls FROM usage WHERE day=?',(day,)).fetchone()
    if row and row[0]>=cfg['daily_model_limit']:
        db.rollback();raise RuntimeError('每日模型额度已满，未调用')
    db.execute('INSERT INTO usage VALUES(?,1) ON CONFLICT(day) DO UPDATE SET calls=calls+1',(day,));db.commit()
    body={'model':cfg['model'],'response_format':{'type':'json_object'},'thinking':{'type':'disabled'},'max_tokens':150,'stream':False,
        'messages':[{'role':'user','content':[{'type':'text','text':'Identify the dominant color of the LEFT half and RIGHT half of this image. Return JSON only: {"left":"color in English","right":"color in English"}.'},
                    {'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(png).decode()}}]}]}
    started=time.monotonic();usage={};result={}
    async with aiohttp.ClientSession(trust_env=False) as session:
        async with session.post('https://api.deepseek.com/chat/completions',headers={'Authorization':'Bearer '+cfg['api_key']},json=body,allow_redirects=False,timeout=aiohttp.ClientTimeout(total=90)) as response:
            status=response.status
            if status==200:
                payload=await response.json();usage=payload.get('usage',{})
                try: result=json.loads(payload['choices'][0]['message']['content'])
                except (ValueError,KeyError,IndexError):pass
    record(db,usage,cfg['model'],'vision_probe',chat='');db.commit();db.close()
    report={'model':cfg['model'],'http_status':status,'left_correct':str(result.get('left','')).lower()=='red','right_correct':str(result.get('right','')).lower()=='blue','synthetic_image_only':True,'qq_messages_sent':0,'elapsed_seconds':round(time.monotonic()-started,2)}
    (ROOT/'logs/reports/qq-life-vision-check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False))

asyncio.run(main())
