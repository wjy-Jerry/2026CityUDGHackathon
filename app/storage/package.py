"""Portable generated applications, excluding runtime data and caches."""
import io
import zipfile
from pathlib import Path


def application_package(root: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob('*')):
            relative = path.relative_to(root)
            if (not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve())
                    or relative.parts[0] == 'data'
                    or any(part in {'__pycache__', '.pytest_cache', '.git'} for part in relative.parts)
                    or path.name.startswith('.coverage') or path.suffix == '.pyc'):
                continue
            archive.writestr(relative.as_posix(), path.read_bytes())
    return buffer.getvalue()
