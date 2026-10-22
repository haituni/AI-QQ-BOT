from __future__ import annotations
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
import numpy as np
from bot.sticker_rag import StickerRetrieval, init_store, DIMENSIONS, MODEL_KEY

class Encoder:
    def __init__(self):self.calls=[];self.after=None
    def ready(self):return True
    def encode(self,texts,*,query=False):
        self.calls.append((list(texts),query))
        # Distinct query/document wording intentionally has no shared substring.
        meanings={'傲娇的猫':0,'口是心非':0,'流泪的小狗':1,'伤心求安慰':1,'迷惑的企鹅':2,'这是啥':2,'重新描述':3,'未知请求':4}
        result=np.zeros((len(texts),DIMENSIONS),dtype=np.float32)
        for i,text in enumerate(texts):result[i,meanings[text]]=1
        if self.after:self.after()
        return result

class Retrieval(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.db.execute('CREATE TABLE life_media(id TEXT PRIMARY KEY,description TEXT,saved INTEGER)');init_store(self.db)
        self.encoder=Encoder();self.rag=StickerRetrieval(Path(self.tmp.name),self.db,encoder=self.encoder)
        for identifier,desc in [('a','傲娇的猫'),('b','流泪的小狗'),('c','迷惑的企鹅')]:self.db.execute('INSERT INTO life_media VALUES(?,?,1)',(identifier,desc))
        self.db.commit()
    async def asyncTearDown(self):self.db.close();self.tmp.cleanup()
    async def search(self,query):return await self.rag.search(query,lambda:None)
    async def test_semantics_not_substring_and_no_unrelated_fallback(self):
        result=await self.search('口是心非');self.assertEqual([x['id'] for x in result['stickers']],['a'])
        self.assertEqual(result['stickers'][0]['score'],1)
        self.assertEqual((await self.search('未知请求'))['stickers'],[])
        self.assertEqual(len([x for x in self.encoder.calls if not x[1]]),1)
    async def test_persistent_cache_reused_and_description_change_reindexed(self):
        await self.search('伤心求安慰')
        self.rag=StickerRetrieval(Path(self.tmp.name),self.db,encoder=self.encoder)
        await self.search('这是啥');self.assertEqual(len(self.encoder.calls),3)
        self.db.execute("UPDATE life_media SET description='重新描述' WHERE id='a'");self.db.commit()
        result=await self.search('口是心非');self.assertEqual(result['stickers'],[])
        self.assertEqual(self.encoder.calls[-2],(['重新描述'],False))
    async def test_delete_and_corrupt_cache_cannot_return_phantom_candidates(self):
        await self.search('口是心非')
        self.db.execute("DELETE FROM life_media WHERE id='a'")
        self.db.execute("UPDATE sticker_vectors SET vector=? WHERE media_id='b'",(b'bad',));self.db.commit()
        self.assertEqual((await self.search('口是心非'))['stickers'],[])
        self.assertIsNone(self.db.execute("SELECT 1 FROM sticker_vectors WHERE media_id='a'").fetchone())
        self.assertEqual(self.encoder.calls[-2],(['流泪的小狗'],False))
    async def test_permission_revoked_while_embedding_prevents_cache_write(self):
        revoked=False
        def after():
            nonlocal revoked
            revoked=True
        def guard():
            if revoked:raise ValueError('revoked')
        self.encoder.after=after
        with self.assertRaisesRegex(ValueError,'revoked'):await self.rag.search('口是心非',guard)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sticker_vectors').fetchone()[0],0)
    async def test_missing_description_is_explicit_and_invalid_query_rejected(self):
        self.db.execute("INSERT INTO life_media VALUES('d','',1)");self.db.commit()
        result=await self.search('这是啥');self.assertEqual(result['without_description'],1)
        for query in ('','  ','a'*161):
            with self.assertRaises(ValueError):await self.search(query)
    async def test_returns_at_most_five_with_scores(self):
        for i in range(8):self.db.execute('INSERT INTO life_media VALUES(?,?,1)',('x'+str(i),'傲娇的猫'))
        self.db.commit();result=await self.search('口是心非');self.assertEqual(len(result['stickers']),5)
        self.assertEqual(result['total_saved'],11)
