"""Local Chinese semantic retrieval over saved sticker descriptions.

No query or description leaves this machine. Embeddings are derived cache data;
source descriptions and saved-media permissions are checked before returning hits.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from pathlib import Path
from typing import Callable

MODEL_REVISION = '75c43b069aac4d136ba6bc1122f995fedcfd2781'
MODEL_KEY = 'bge-small-zh-v1.5-q8-cls:' + MODEL_REVISION
MODEL_FILES = {
    'model_quantized.onnx': ('onnx/model_quantized.onnx', '15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc'),
    'tokenizer.json': ('tokenizer.json', '48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26'),
}
QUERY_PREFIX = '为这个句子生成表示以用于检索相关文章：'
DIMENSIONS = 512
TOP_K = 5
MIN_SCORE = .30


def init_store(db) -> None:
    db.execute('''CREATE TABLE IF NOT EXISTS sticker_vectors(
        media_id TEXT PRIMARY KEY,model TEXT NOT NULL,text_hash TEXT NOT NULL,vector BLOB NOT NULL)''')


class LocalEncoder:
    """Lazy, checksum-verified ONNX encoder; inference runs off the event loop."""
    def __init__(self, root: Path):
        self.folder = root / 'runtime/models/bge-small-zh-v1.5'
        self._lock = threading.RLock()
        self._session = None
        self._tokenizer = None

    def ready(self) -> bool:
        return all((self.folder / name).is_file() for name in MODEL_FILES)

    def _load(self) -> None:
        if self._session is not None:
            return
        if not self.ready():
            raise ValueError('本地表情语义模型尚未安装，请在项目根目录运行 .venv/Scripts/python.exe scripts/setup_sticker_rag.py')
        for name, (_, expected) in MODEL_FILES.items():
            if hashlib.sha256((self.folder / name).read_bytes()).hexdigest() != expected:
                raise ValueError('本地表情语义模型校验失败，请重新安装，未降级为字符串匹配')
        os.environ['ORT_DISABLE_TELEMETRY'] = '1'
        import onnxruntime as ort
        from tokenizers import Tokenizer
        ort.disable_telemetry_events()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        self._tokenizer = Tokenizer.from_file(str(self.folder / 'tokenizer.json'))
        self._tokenizer.enable_truncation(max_length=512)
        self._tokenizer.enable_padding(pad_id=0, pad_token='[PAD]')
        self._session = ort.InferenceSession(str(self.folder / 'model_quantized.onnx'),
                                            sess_options=options, providers=['CPUExecutionProvider'])

    def encode(self, texts: list[str], *, query: bool = False):
        import numpy as np
        with self._lock:
            self._load()
            encoded = self._tokenizer.encode_batch([(QUERY_PREFIX if query else '') + text for text in texts])
            available = {
                'input_ids': np.array([item.ids for item in encoded], dtype=np.int64),
                'attention_mask': np.array([item.attention_mask for item in encoded], dtype=np.int64),
                'token_type_ids': np.array([item.type_ids for item in encoded], dtype=np.int64),
            }
            inputs = {item.name: available[item.name] for item in self._session.get_inputs()}
            hidden = self._session.run(None, inputs)[0]
            vectors = hidden[:, 0, :] if hidden.ndim == 3 else hidden
            vectors = np.asarray(vectors, dtype=np.float32)
            if vectors.shape != (len(texts), DIMENSIONS) or not np.isfinite(vectors).all():
                raise ValueError('本地语义模型输出无效')
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            if (norms <= 0).any():
                raise ValueError('本地语义向量无效')
            return vectors / norms


class StickerRetrieval:
    def __init__(self, root: Path, db, *, encoder=None):
        self.db = db
        self.encoder = encoder or LocalEncoder(root)
        self.lock = asyncio.Lock()

    def invalidate(self, identifier: str) -> None:
        self.db.execute('DELETE FROM sticker_vectors WHERE media_id=?', (identifier,))
        self.db.commit()

    def status(self) -> dict:
        total = self.db.execute('SELECT count(*) FROM life_media WHERE saved=1').fetchone()[0]
        indexed = self.db.execute('SELECT count(*) FROM sticker_vectors WHERE model=?', (MODEL_KEY,)).fetchone()[0]
        return {'method': 'local_dense_embeddings', 'model': MODEL_KEY,
                'ready': self.encoder.ready(), 'saved': total, 'cached_vectors': indexed, 'top_k': TOP_K}

    async def search(self, query: str, guard: Callable[[], None]) -> dict:
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 160:
            raise ValueError('请填写1～160字的表情语义关键词或使用意图')
        import numpy as np
        async with self.lock:
            guard()
            rows = self.db.execute('SELECT id,description FROM life_media WHERE saved=1 ORDER BY id').fetchall()
            total = len(rows)
            usable = [dict(row) for row in rows if row['description'].strip()]
            self.db.execute("DELETE FROM sticker_vectors WHERE media_id NOT IN (SELECT id FROM life_media WHERE saved=1 AND trim(description)!='')")
            self.db.commit()
            if not usable:
                return self.result([], total, total, 0)
            cached = {row['media_id']: dict(row) for row in self.db.execute('SELECT * FROM sticker_vectors')}
            pending = []
            vectors = {}
            for item in usable:
                digest = hashlib.sha256(item['description'].encode()).hexdigest()
                item['text_hash'] = digest
                cache = cached.get(item['id'])
                if cache and cache['model'] == MODEL_KEY and cache['text_hash'] == digest and len(cache['vector']) == DIMENSIONS * 4:
                    vector = np.frombuffer(cache['vector'], dtype='<f4')
                    if np.isfinite(vector).all() and .99 <= float(np.linalg.norm(vector)) <= 1.01:
                        vectors[item['id']] = vector
                        continue
                pending.append(item)
            for start in range(0, len(pending), 16):
                batch = pending[start:start+16]
                encoded = await asyncio.to_thread(self.encoder.encode, [item['description'] for item in batch])
                guard()
                for item, vector in zip(batch, encoded, strict=True):
                    current = self.db.execute('SELECT description FROM life_media WHERE id=? AND saved=1', (item['id'],)).fetchone()
                    if not current or current[0] != item['description']:
                        continue
                    vector = np.asarray(vector, dtype='<f4')
                    if vector.shape != (DIMENSIONS,) or not np.isfinite(vector).all():
                        raise ValueError('语义索引向量无效')
                    vectors[item['id']] = vector
                    self.db.execute('INSERT OR REPLACE INTO sticker_vectors VALUES(?,?,?,?)',
                                    (item['id'], MODEL_KEY, item['text_hash'], vector.tobytes()))
                self.db.commit()
            query_vector = (await asyncio.to_thread(self.encoder.encode, [query.strip()], query=True))[0]
            guard()
            hits = []
            for item in usable:
                vector = vectors.get(item['id'])
                if vector is None:
                    continue
                current = self.db.execute('SELECT description FROM life_media WHERE id=? AND saved=1', (item['id'],)).fetchone()
                if not current or current[0] != item['description']:
                    continue
                score = float(np.dot(vector, query_vector))
                if score >= MIN_SCORE:
                    hits.append({'id': item['id'], 'description': item['description'],
                                 'score': round(max(-1., min(1., score)), 4)})
            hits.sort(key=lambda item: (-item['score'], item['id']))
            return self.result(hits[:TOP_K], total, total-len(usable), len(vectors))

    @staticmethod
    def result(hits: list[dict], total: int, missing: int, indexed: int) -> dict:
        return {'stickers': hits, 'total_saved': total, 'indexed': indexed,
                'without_description': missing, 'retrieval': 'local_dense_embeddings',
                'model': MODEL_KEY, 'score_type': 'cosine_similarity_not_probability',
                'notice': '这些是按描述语义召回的候选，不是已发送的图片。由你结合上下文挑选；需要时inspect_images确认，再调用qq_send发送。无合适候选可以不用，或联网找新素材；不要只口头声称已经发了。'}
