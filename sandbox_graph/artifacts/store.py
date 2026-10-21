"""Durable immutable files; publication precedes any ledger reference."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from ..domain.models import EvidenceRef


class FileArtifactStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _scope_dir(self, scope):
        if not re.fullmatch(r'[A-Za-z0-9_.:-]+', scope) or scope in {'.','..'}:
            raise ValueError('invalid artifact scope')
        path = self.root / scope
        path.mkdir(exist_ok=True)
        if path.is_symlink() or path.resolve().parent != self.root:
            raise ValueError('artifact scope escapes storage root')
        return path

    def commit(self, chunks, *, scope: str, media_type: str, producer: str = 'unknown') -> EvidenceRef:
        directory = self._scope_dir(scope)
        digest, size = hashlib.sha256(), 0
        fd, tmp = tempfile.mkstemp(prefix='.stage-', dir=directory)
        try:
            with os.fdopen(fd, 'wb') as f:
                for chunk in chunks:
                    digest.update(chunk)
                    size += len(chunk)
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
            identity = digest.hexdigest()
            target = directory / identity
            try:
                os.link(tmp, target)
            except FileExistsError:
                pass
            ref = EvidenceRef(artifact_id=f'{scope}:{identity}', uri=f'{scope}/{identity}', digest=identity, size=size, media_type=media_type, scope=scope, producer=producer)
            self.verify(ref)
            manifest = directory / f'{identity}.json'
            payload = ref.model_dump_json().encode()
            if not manifest.exists():
                mfd, mtmp = tempfile.mkstemp(prefix='.manifest-', dir=directory)
                try:
                    with os.fdopen(mfd, 'wb') as f:
                        f.write(payload); f.flush(); os.fsync(f.fileno())
                    try:
                        os.link(mtmp, manifest)
                    except FileExistsError:
                        pass
                finally:
                    os.unlink(mtmp)
            else:
                existing = EvidenceRef.model_validate_json(manifest.read_bytes())
                if existing != ref:
                    raise ValueError('same content has conflicting immutable manifest')
            dfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
            return ref
        finally:
            os.unlink(tmp)

    def path_for(self, ref: EvidenceRef) -> Path:
        path = self.root / ref.uri
        expected = self._scope_dir(ref.scope) / ref.digest
        if path != expected or path.is_symlink() or path.resolve().parent != expected.parent.resolve():
            raise ValueError('invalid artifact URI')
        return path

    def verify(self, ref: EvidenceRef) -> None:
        path = self.path_for(ref)
        digest = hashlib.sha256()
        size = 0
        with path.open('rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                size += len(chunk); digest.update(chunk)
        if size != ref.size or digest.hexdigest() != ref.digest:
            raise ValueError('artifact digest or size mismatch')

    def open_range(self, ref: EvidenceRef, *, scope: str, offset: int, length: int, max_bytes: int = 65536) -> bytes:
        if scope != ref.scope:
            raise PermissionError('cross-scope artifact read')
        if offset < 0 or length < 0 or length > max_bytes:
            raise ValueError('range exceeds evidence budget')
        self.verify(ref)
        with self.path_for(ref).open('rb') as f:
            f.seek(offset)
            return f.read(length)
