"""Общие фикстуры. Тесты не ходят в сеть, не требуют токена и не трогают боевой индекс."""
import hashlib
import math
import os
import re
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # service/ — плоские импорты, как в api.py
# Пустое значение (а не удаление): load_dotenv не перезаписывает уже заданные переменные,
# иначе тесты подхватили бы реальный токен из service/.env и ходили бы в настоящий KIT.
os.environ["KIT_API_TOKEN"] = ""


class StubEmbedder:
    """Детерминированный «эмбеддер» на хешированном мешке слов: похожие тексты -> близкие векторы."""
    DIM = 128

    def encode(self, texts, normalize_embeddings=True):
        single = isinstance(texts, str)
        out = []
        for t in ([texts] if single else texts):
            v = [0.0] * self.DIM
            for w in re.findall(r"\w+", t.casefold()):
                v[int(hashlib.md5(w[:5].encode()).hexdigest(), 16) % self.DIM] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out[0] if single else out


def load_fixture(name):
    import json
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def stub_embedder():
    import catalog
    old = catalog._embedder
    catalog.set_embedder(StubEmbedder())
    yield
    catalog.set_embedder(old)


@pytest.fixture
def col(tmp_path, stub_embedder, monkeypatch):
    """Временная коллекция products в tmp-каталоге."""
    import catalog
    monkeypatch.setattr(catalog, "MIN_SCORE", 0.0)
    monkeypatch.setattr(catalog, "_live_cache", {})
    return catalog.get_collection(str(tmp_path / "chroma"))


@pytest.fixture
def rag(monkeypatch):
    """main.py без тяжёлых зависимостей: подменяем sentence_transformers и ключ."""
    fake = types.ModuleType("sentence_transformers")
    fake.SentenceTransformer = lambda *a, **k: StubEmbedder()
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    monkeypatch.setenv("GENAPI_KEY", "test-key")
    sys.modules.pop("main", None)
    import main
    yield main
    sys.modules.pop("main", None)
