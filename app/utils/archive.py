"""Encryption of backup files, one format for platform backups and volume archives.

A backup is `openssl enc -aes-256-cbc -pbkdf2 -salt` of the file, followed by the 32 bytes
of an HMAC-SHA256 of that ciphertext (encrypt-then-MAC), both from ENCRYPTION_KEY. The
worker scripts `backup.sh` and `restore.sh` write and read the same format for volumes.
The files stream through openssl, so memory stays flat however large they are, and any
machine with openssl can read one (see the commands on Settings → S3).
"""
import hashlib
import hmac
import os
import subprocess
from pathlib import Path

from utils.common import get_env

MAC_LENGTH = 32
_CHUNK = 1024 * 1024
# `Salted__`, the 8-byte salt and one 16-byte block of ciphertext, then the MAC.
_MIN_SIZE = 16 + 16 + MAC_LENGTH
_PASSPHRASE_ENV = "SAGE_BACKUP_PASSPHRASE"


class BackupDecryptError(Exception):
  """The file can't be read with this key; the message completes "the backup …"."""


def _mac_key(passphrase: str) -> bytes:
  # As in the worker scripts, which derive it from the passphrase file, whose last line
  # ends with a newline.
  return hashlib.sha256(b"sage-archive-mac:" + passphrase.encode() + b"\n").digest()


def _mac(path: Path, key: bytes, length: int) -> bytes:
  mac = hmac.new(key, digestmod=hashlib.sha256)
  with path.open("rb") as file:
    while length > 0:
      chunk = file.read(min(_CHUNK, length))
      if not chunk:
        break
      mac.update(chunk)
      length -= len(chunk)
  return mac.digest()


def _openssl(extra: list[str], passphrase: str, **popen) -> subprocess.Popen:
  # The passphrase goes through the environment, never a command line.
  return subprocess.Popen(
      ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-pass", f"env:{_PASSPHRASE_ENV}", *extra],
      env={**os.environ, _PASSPHRASE_ENV: passphrase},
      stderr=subprocess.PIPE, **popen)


def encrypt_file(source: Path, target: Path, passphrase: str | None = None):
  """Write the encrypted `source` to `target`. Blocking: run it off the event loop."""
  passphrase = passphrase or get_env("ENCRYPTION_KEY")
  process = _openssl(["-salt", "-in", str(source), "-out", str(target)], passphrase)
  _, error = process.communicate()
  if process.returncode != 0:
    raise Exception(f"openssl failed to encrypt the file: {error.decode(errors='replace').strip()}")
  with target.open("ab") as file:
    file.write(_mac(target, _mac_key(passphrase), target.stat().st_size))


def decrypt_file(source: Path, target: Path, passphrase: str | None = None):
  """Write the decrypted `source` to `target` after checking its MAC, so a corrupted or
  altered file, or one from another key, is refused before anything is decrypted.
  Raises BackupDecryptError. Blocking: run it off the event loop."""
  passphrase = passphrase or get_env("ENCRYPTION_KEY")
  size = source.stat().st_size
  if size < _MIN_SIZE:
    raise BackupDecryptError("is too short to be valid")
  with source.open("rb") as file:
    file.seek(size - MAC_LENGTH)
    trailer = file.read(MAC_LENGTH)
  if not hmac.compare_digest(_mac(source, _mac_key(passphrase), size - MAC_LENGTH), trailer):
    raise BackupDecryptError(
        "failed its integrity check: it is corrupted or altered, or was made with a different "
        "encryption key")

  # Everything but the MAC goes to openssl, in chunks.
  with source.open("rb") as file, target.open("wb") as out, \
          _openssl(["-d"], passphrase, stdin=subprocess.PIPE, stdout=out) as process:
    try:
      remaining = size - MAC_LENGTH
      while remaining > 0:
        chunk = file.read(min(_CHUNK, remaining))
        process.stdin.write(chunk)
        remaining -= len(chunk)
      process.stdin.close()
    except BrokenPipeError:
      pass  # openssl stopped early; its error says why
    error = process.stderr.read()
  if process.returncode != 0:
    raise BackupDecryptError(f"could not be decrypted: {error.decode(errors='replace').strip()}")
