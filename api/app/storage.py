# HeArt v1 API — invented by John Davis.
"""File storage abstraction. Local disk for dev; Supabase Storage in production.

v1: files land under UPLOAD_DIR/<work_id><ext>. PRODUCTION NOTE: swap this
for the Supabase Storage bucket (private bucket + signed URLs) without
changing the interface — `save()` returns the object key either way.
"""

import os
from pathlib import Path


class LocalStorage:
    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, work_id: str, filename: str, data: bytes) -> str:
        ext = Path(filename).suffix.lower() or ".bin"
        object_key = f"{work_id}{ext}"
        (self.root / object_key).write_bytes(data)
        return object_key

    def abspath(self, object_key: str) -> str:
        # Guard against path traversal: only serve keys we created.
        name = os.path.basename(object_key)
        if name != object_key or ".." in object_key:
            raise ValueError("invalid object key")
        return str(self.root / name)

    def exists(self, object_key: str) -> bool:
        try:
            return Path(self.abspath(object_key)).exists()
        except ValueError:
            return False
