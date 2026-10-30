from types import SimpleNamespace

from app.cache import _used_memory, rate_limited, reclaim_if_full


class MemRedis:
    def __init__(self, used=0, keys=None):
        self.used = used
        self.keys = list(keys or [])
        self.flushed = False
        self.deleted: list = []

    async def info(self, _section="memory"):
        return {"used_memory": self.used}

    async def flushdb(self):
        self.flushed = True
        self.keys = []
        self.used = 1_000

    async def scan(self, cursor, match="emb:*", count=400):
        hits = [k for k in self.keys if str(k).startswith("emb:")]
        return 0, hits

    async def delete(self, *keys):
        self.deleted.extend(keys)
        keep = set(keys)
        self.keys = [k for k in self.keys if k not in keep]


class BoomRedis:
    async def incr(self, _k):
        raise ConnectionError("refused")

    async def info(self, _section="memory"):
        raise ConnectionError("refused")


def test_used_memory_reads_str_or_bytes_keys():
    assert _used_memory({"used_memory": 12}) == 12
    assert _used_memory({b"used_memory": 9}) == 9
    assert _used_memory({}) == 0


async def test_reclaim_flushes_when_instance_is_full():
    r = MemRedis(used=200 * 1024 * 1024, keys=["emb:aa", "sess:1"])
    out = await reclaim_if_full(r)
    assert out["ok"] is True and out["flushed"] is True
    assert r.flushed is True


async def test_reclaim_drops_embeddings_when_under_limit():
    r = MemRedis(used=10 * 1024 * 1024, keys=["emb:aa", "emb:bb", "sess:1"])
    out = await reclaim_if_full(r)
    assert out["ok"] is True and out["flushed"] is False
    assert out["deleted_emb"] == 2
    assert "composio:toolkits:v1" in r.deleted
    assert "sess:1" in r.keys


async def test_reclaim_and_rate_limit_survive_redis_down(monkeypatch):
    from app import cache

    monkeypatch.setattr(cache, "get_redis", lambda: BoomRedis())
    out = await reclaim_if_full()
    assert out["ok"] is False
    assert await rate_limited("x", 3) is False
