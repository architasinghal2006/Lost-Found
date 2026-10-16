import hashlib
import io
import os
import re

import numpy as np
from PIL import Image

DIM = 512


def _unit(vector):
    norm = np.linalg.norm(vector)
    vector = vector / norm if norm else vector
    return vector.astype(np.float32)


def _random_vector(text):
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
    return np.random.default_rng(seed).standard_normal(DIM)


def _png_caption(image_bytes):
    try:
        return Image.open(io.BytesIO(image_bytes)).info.get("caption")
    except Exception:
        return None


class StubEmbedder:
    # Deterministic stand-in for CLIP so the app runs with no model download.
    # Text: each word becomes a fixed random vector; the query vector is their sum.
    # Images: seed.py hides a caption in the PNG; real uploads get a vector from their bytes.
    name = "stub"
    viability_threshold = 0.15

    def embed_text(self, text):
        words = re.findall(r"[a-z0-9]+", str(text).lower())
        if not words:
            return np.zeros(DIM, dtype=np.float32)
        return _unit(sum(_random_vector("word:" + w) for w in words))

    def embed_image(self, image_bytes):
        caption = _png_caption(image_bytes)
        if caption:
            return self.embed_text(caption)
        return _unit(_random_vector("bytes:" + hashlib.sha256(image_bytes).hexdigest()))


class ClipEmbedder:
    # CLIP ViT-B/32 on CPU. Enabled with LF_USE_CLIP=1. Weights download once, then load from cache.
    name = "clip"
    viability_threshold = 0.22

    def __init__(self):
        import open_clip
        import torch
        self.torch = torch
        self.model, _, self.preprocess = open_clip.create_model_and_transforms("ViT-B-32", pretrained="openai")
        self.model.eval()
        self.tokenizer = open_clip.get_tokenizer("ViT-B-32")

    def embed_text(self, text):
        with self.torch.no_grad():
            vector = self.model.encode_text(self.tokenizer([str(text)]))[0].numpy()
        return _unit(vector)

    def embed_image(self, image_bytes):
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        with self.torch.no_grad():
            vector = self.model.encode_image(self.preprocess(image).unsqueeze(0))[0].numpy()
        return _unit(vector)


_embedder = None


def get_embedder():
    global _embedder
    if _embedder is None:
        _embedder = ClipEmbedder() if os.environ.get("LF_USE_CLIP") == "1" else StubEmbedder()
    return _embedder


def to_blob(vector):
    return np.asarray(vector, dtype=np.float32).tobytes()


def from_blob(blob):
    return np.frombuffer(blob, dtype=np.float32)
