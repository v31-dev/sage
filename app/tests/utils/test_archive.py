import hashlib
import hmac
import os
import subprocess

import pytest

from utils.archive import MAC_LENGTH, BackupDecryptError, decrypt_file, encrypt_file

PASSPHRASE = "correct horse battery staple"


@pytest.fixture
def plain(tmp_path):
  path = tmp_path / "plain.bin"
  path.write_bytes(os.urandom(2_500_000))
  return path


@pytest.fixture
def encrypted(plain, tmp_path):
  path = tmp_path / "plain.bin.enc"
  encrypt_file(plain, path, PASSPHRASE)
  return path


def test_round_trip_of_a_file_larger_than_one_chunk(plain, encrypted, tmp_path):
  restored = tmp_path / "restored.bin"
  decrypt_file(encrypted, restored, PASSPHRASE)
  assert restored.read_bytes() == plain.read_bytes()


def test_round_trip_of_an_empty_file(tmp_path):
  source, target, restored = tmp_path / "empty", tmp_path / "empty.enc", tmp_path / "restored"
  source.write_bytes(b"")
  encrypt_file(source, target, PASSPHRASE)
  decrypt_file(target, restored, PASSPHRASE)
  assert restored.read_bytes() == b""


def test_the_encrypted_file_hides_the_content(plain, encrypted):
  assert encrypted.read_bytes()[16:64] != plain.read_bytes()[:48]
  assert encrypted.stat().st_size > plain.stat().st_size


def test_the_format_is_plain_openssl_followed_by_the_mac(plain, encrypted):
  """Anyone with openssl can read a backup once the trailing MAC is cut off."""
  data = encrypted.read_bytes()
  assert data.startswith(b"Salted__")
  result = subprocess.run(
      ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-pass", "env:PASSPHRASE"],
      input=data[:-MAC_LENGTH], env={**os.environ, "PASSPHRASE": PASSPHRASE}, capture_output=True, check=True)
  assert result.stdout == plain.read_bytes()


def test_a_wrong_passphrase_is_refused_before_anything_is_written(encrypted, tmp_path):
  restored = tmp_path / "restored.bin"
  with pytest.raises(BackupDecryptError, match="integrity check"):
    decrypt_file(encrypted, restored, "another passphrase")
  assert not restored.exists()


@pytest.mark.parametrize("where", ["start", "middle", "end"])
def test_a_changed_byte_is_refused_before_anything_is_written(encrypted, tmp_path, where):
  data = bytearray(encrypted.read_bytes())
  index = {"start": 20, "middle": len(data) // 2, "end": len(data) - 1}[where]
  data[index] ^= 0xFF
  encrypted.write_bytes(bytes(data))
  restored = tmp_path / "restored.bin"
  with pytest.raises(BackupDecryptError, match="integrity check"):
    decrypt_file(encrypted, restored, PASSPHRASE)
  assert not restored.exists()


def test_a_truncated_file_is_refused(encrypted, tmp_path):
  encrypted.write_bytes(encrypted.read_bytes()[:-1])
  with pytest.raises(BackupDecryptError, match="integrity check"):
    decrypt_file(encrypted, tmp_path / "restored.bin", PASSPHRASE)


@pytest.mark.parametrize("size", [0, 1, 63])
def test_a_file_too_short_to_be_a_backup_is_refused(tmp_path, size):
  short = tmp_path / "short.enc"
  short.write_bytes(b"x" * size)
  with pytest.raises(BackupDecryptError, match="too short"):
    decrypt_file(short, tmp_path / "restored.bin", PASSPHRASE)


def test_a_file_with_a_valid_mac_but_no_valid_encryption_is_reported(tmp_path):
  """The MAC (sha256 of "sage-archive-mac:<passphrase>\\n", then HMAC-SHA256) says the key is right; openssl can't read the body."""
  body = b"Salted__" + b"\0" * 8 + b"not a valid ciphertext block"
  key = hashlib.sha256(b"sage-archive-mac:" + PASSPHRASE.encode() + b"\n").digest()
  forged = tmp_path / "forged.enc"
  forged.write_bytes(body + hmac.new(key, body, hashlib.sha256).digest())
  with pytest.raises(BackupDecryptError, match="could not be decrypted"):
    decrypt_file(forged, tmp_path / "restored.bin", PASSPHRASE)


def test_the_encryption_key_is_the_default_passphrase(plain, tmp_path):
  encrypted, restored = tmp_path / "default.enc", tmp_path / "restored.bin"
  encrypt_file(plain, encrypted)
  decrypt_file(encrypted, restored, os.environ["ENCRYPTION_KEY"])
  assert restored.read_bytes() == plain.read_bytes()
  decrypt_file(encrypted, tmp_path / "again.bin")
  with pytest.raises(BackupDecryptError):
    decrypt_file(encrypted, tmp_path / "wrong.bin", PASSPHRASE)


def test_two_encryptions_of_the_same_file_differ(plain, encrypted, tmp_path):
  again = tmp_path / "again.enc"
  encrypt_file(plain, again, PASSPHRASE)
  assert again.read_bytes() != encrypted.read_bytes()


def test_a_missing_source_is_reported(tmp_path):
  with pytest.raises(Exception, match="openssl failed to encrypt the file"):
    encrypt_file(tmp_path / "missing", tmp_path / "out.enc", PASSPHRASE)
