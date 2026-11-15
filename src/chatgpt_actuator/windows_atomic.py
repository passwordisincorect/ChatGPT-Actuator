"""Local Windows conditional publication with a transaction-held target lock.

TxF is deliberately required rather than falling back to a check/rename race.
Unsupported volumes, EFS and future Windows versions fail before publication.
This module changes no filesystem, service or owner permission settings.
"""
from functools import lru_cache
from contextlib import contextmanager
import errno
import hashlib
import os
from pathlib import Path
import uuid


@lru_cache(maxsize=1)
def _api():
    if os.name != 'nt':
        raise OSError(errno.ENOTSUP, 'Guarded publication requires local Windows transactional file support')
    import ctypes
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    manager = ctypes.WinDLL('KtmW32', use_last_error=True)
    manager.CreateTransaction.argtypes = [w.LPVOID, w.LPVOID, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.LPWSTR]
    manager.CreateTransaction.restype = w.HANDLE
    manager.CommitTransaction.argtypes = [w.HANDLE]
    manager.CommitTransaction.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    kernel.CreateFileTransactedW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID,
        w.DWORD, w.DWORD, w.HANDLE, w.HANDLE, w.LPVOID, w.LPVOID]
    kernel.CreateFileTransactedW.restype = w.HANDLE
    kernel.MoveFileTransactedW.argtypes = [w.LPCWSTR, w.LPCWSTR, w.LPVOID,
        w.LPVOID, w.DWORD, w.HANDLE]
    kernel.MoveFileTransactedW.restype = w.BOOL
    kernel.DeleteFileTransactedW.argtypes = [w.LPCWSTR, w.HANDLE]
    kernel.DeleteFileTransactedW.restype = w.BOOL
    return ctypes, kernel, manager


def _open_target(path, transaction):
    return _open_file(path, transaction, 3)


def _open_file(path, transaction, disposition):
    ctypes, kernel, _ = _api()
    # A transacted read/write handle locks content and namespace modifications
    # from non-transacted writers, including writers which ignore our sidecar.
    handle = kernel.CreateFileTransactedW(str(path), 0xC0000000, 7, None, disposition,
        0x80, None, transaction, None, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


@contextmanager
def _stream(handle):
    import msvcrt
    _, kernel, _ = _api()
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(descriptor, 'r+b') as stream:
        yield stream


def _write_payload(stream, data):
    stream.seek(0)
    stream.write(data)
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())


def _commit(transaction):
    ctypes, _, manager = _api()
    if not manager.CommitTransaction(transaction):
        raise ctypes.WinError(ctypes.get_last_error())


def replace_expected(path, data, expected, *, conflict_error=RuntimeError):
    path = Path(path).absolute()
    ctypes, kernel, manager = _api()
    transaction = manager.CreateTransaction(None, None, 0, 0, 0, 30000,
        'Actuator guarded file publication')
    if transaction == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        try:
            handle = _open_target(path, transaction)
        except FileNotFoundError as exc:
            raise conflict_error('File changed or missing; read the current version before retrying') from exc
        with _stream(handle) as stream:
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(65536), b''):
                digest.update(chunk)
            if digest.hexdigest() != expected.lower():
                raise conflict_error('File changed or missing; read the current version before retrying')
        # Stage a new file, rather than mutating the original inode. Hard-link
        # aliases keep their old data, including aliases outside a workspace.
        if data is None:
            if not kernel.DeleteFileTransactedW(str(path), transaction):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            staged = path.parent / ('.actuator-write-' + uuid.uuid4().hex)
            with _stream(_open_file(staged, transaction, 1)) as stream:
                _write_payload(stream, data)
            if not kernel.MoveFileTransactedW(str(staged), str(path), None, None, 1, transaction):
                raise ctypes.WinError(ctypes.get_last_error())
        # Close the file handle before commit. The transaction's writer lock
        # survives that close, so no external modification can enter the gap.
        _commit(transaction)
    finally:
        # The last transaction handle closes without commit on every failure or
        # process death, and Windows restores the preimage automatically.
        kernel.CloseHandle(transaction)
