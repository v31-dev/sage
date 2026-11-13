import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, NoCredentialsError

import config
from services.base import Base
from services.settings import SETTING_DEFINITIONS, fields_set, validate_section
from services.updates import Updates, UpdateSource
from utils.errors import Conflict
from utils.executor import run_in_executor_with_context
from utils.retry import retry_async

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StorageTarget:
  """Where backups are stored: an endpoint, a bucket and the base path in it, with the credentials for them."""
  endpoint: str
  bucket: str
  path: str
  access_key: str = field(repr=False)
  secret_key: str = field(repr=False)

  @classmethod
  def of(cls, values: dict) -> "StorageTarget":
    return cls(values["endpoint"], values["bucket"], values["path"].strip("/"), values["access_key"], values["secret_key"])

  @property
  def identity(self) -> str:
    """The store it names, apart from the credentials that reach it."""
    return hashlib.sha256(json.dumps([self.endpoint, self.bucket, self.path]).encode()).hexdigest()[:16]


def region_of(endpoint: str) -> str | None:
  hostname = urlparse(endpoint).hostname or ""
  if hostname.startswith("s3.") and hostname.endswith(".backblazeb2.com"):
    parts = hostname.split(".")
    if len(parts) >= 4:
      return parts[1]
  return None


def client_kwargs(endpoint: str, access_key: str, secret_key: str) -> dict:
  kwargs = {
      "aws_access_key_id": access_key,
      "aws_secret_access_key": secret_key,
      "endpoint_url": endpoint,
      "config": Config(signature_version='s3v4'),
  }
  if region := region_of(endpoint):
    kwargs["region_name"] = region
  return kwargs


class S3(Base):
  """The configured store, as a session that `install` replaces when the settings are saved or restored."""

  def __init__(self):
    super().__init__()
    self._session = None

  def install(self, values: dict):
    """Use the store `values` name, a saved S3 section; none unless every field is set."""
    session = S3Session(StorageTarget.of(values)) if fields_set("s3", values) else None
    if session is None:
      logger.warning("No complete S3 config found in settings, S3 functionality will be disabled")
    with self.lock:
      previous, self._session = self._session, session
    if (previous and previous.identity) != (session and session.identity):
      Updates().changed(UpdateSource.BACKUPS)

  def session(self) -> "S3Session":
    """The configured store's session, for an operation to hold from start to end; a Conflict when S3 is not set."""
    with self.lock:
      session = self._session
    if session is None:
      raise Conflict("S3 is not configured.")
    return session

  def check_config(self, config: dict, write: bool = False):
    """Check a candidate S3 config against its bucket; raises with the reason. A config with
    no values at all is valid (S3 off). With write, also writes and deletes a marker object."""
    validate_section("s3", config)
    if not any(config.get(key) for key in SETTING_DEFINITIONS["s3"]):
      return

    kwargs = client_kwargs(config["endpoint"], config["access_key"], config["secret_key"])
    kwargs["config"] = Config(signature_version='s3v4', connect_timeout=5, read_timeout=10,
                              retries={"max_attempts": 2})
    client = boto3.session.Session().client('s3', **kwargs)
    prefix = config["path"].strip('/')
    client.list_objects_v2(Bucket=config["bucket"], Prefix=prefix, MaxKeys=1)
    if write:
      marker = f"{prefix}/.sage-check"
      client.put_object(Bucket=config["bucket"], Key=marker, Body=b"")
      client.delete_object(Bucket=config["bucket"], Key=marker)


class S3Session:
  """One storage target with its own client. An operation holds one session from start to end, so a settings save
  meanwhile never moves its work to another store."""

  def __init__(self, target: StorageTarget):
    self.target = target
    self._client = boto3.session.Session().client(
        's3', **client_kwargs(target.endpoint, target.access_key, target.secret_key))

  @property
  def identity(self) -> str:
    return self.target.identity

  def get_full_path(self, s3_path):
    """The key of `s3_path` under the target's base path."""
    return f"{self.target.path}/{s3_path.strip('/')}"

  def create_presigned_upload_url(self, s3_path, expires_in, raw_path=False):
    """
    Create a presigned PUT URL for direct uploads.
    """
    if raw_path:
      s3_key = s3_path.strip('/')
    else:
      s3_key = self.get_full_path(s3_path)

    return self._client.generate_presigned_url(
        'put_object',
        Params={
            'Bucket': self.target.bucket,
            'Key': s3_key,
            'ContentType': 'application/octet-stream',
        },
        ExpiresIn=expires_in,
        HttpMethod='PUT',
    )

  def create_presigned_download_url(self, s3_path, expires_in, raw_path=False, filename=None):
    """
    Create a presigned GET URL for direct downloads. With a filename, the response asks
    the browser to save the file under that name.
    """
    if raw_path:
      s3_key = s3_path.strip('/')
    else:
      s3_key = self.get_full_path(s3_path)

    params = {
        'Bucket': self.target.bucket,
        'Key': s3_key,
    }
    if filename:
      params['ResponseContentDisposition'] = f'attachment; filename="{filename}"'

    return self._client.generate_presigned_url(
        'get_object',
        Params=params,
        ExpiresIn=expires_in,
        HttpMethod='GET',
    )

  def key_exists(self, s3_path, raw_path=False) -> bool:
    """True if the object is in the bucket. Blocking."""
    s3_key = s3_path.strip('/') if raw_path else self.get_full_path(s3_path)
    try:
      self._client.head_object(Bucket=self.target.bucket, Key=s3_key)
      return True
    except ClientError as e:
      if e.response.get('Error', {}).get('Code') in ('404', 'NoSuchKey', 'NotFound'):
        return False
      raise

  async def delete_key(self, s3_path, filter=None, raw_path=False):
    """
    Delete files/folders from S3 based on a filter function (async). A failure is
    retried after each of `config.S3_RETRY_DELAYS`, except for invalid credentials.

    Args:
      s3_path: S3 path relative to base config path (will be combined)
      filter: Callable that takes S3 key/path string and returns True to delete, False to keep.
                  Example: lambda key: "old_" in key
                  If None, deletes all objects at the prefix.
      raw_path: If True, s3_path is treated as the full key without combining with base path.


    Returns:
      List of deleted S3 paths
    """
    async def attempt():
      bucket_name = self.target.bucket
      deleted_paths = []

      # List all objects at the given prefix
      if raw_path:
        prefix = s3_path.strip('/')
      else:
        prefix = self.get_full_path(s3_path)
      keys = await run_in_executor_with_context(self._list_keys, prefix)

      # Apply filter function if provided
      objects_to_delete = [{'Key': key} for key in keys if filter is None or filter(key)]

      # S3 delete_objects is limited to 1000 objects per request
      for i in range(0, len(objects_to_delete), 1000):
        batch = objects_to_delete[i:i + 1000]
        response = await run_in_executor_with_context(
            self._client.delete_objects, Bucket=bucket_name, Delete={'Objects': batch})

        # Track deleted paths
        for deleted_obj in response.get('Deleted', []):
          deleted_paths.append(deleted_obj['Key'])

        for error_obj in response.get('Errors', []):
          raise Exception(f"Failed to delete {error_obj['Key']}: {error_obj['Message']}")

      return deleted_paths

    try:
      return await retry_async(
          attempt, delays=config.S3_RETRY_DELAYS, should_retry=lambda error: not isinstance(error, NoCredentialsError))
    except NoCredentialsError:
      raise Exception("S3 credentials are not valid.")
    except ClientError as e:
      raise Exception(f"Failed to delete from S3 after {len(config.S3_RETRY_DELAYS) + 1} attempts: {e}")

  async def upload_to_key(self, file_path, s3_path):
    """
    Upload file or directory to S3 (async).
    - If file_path is a file, upload it directly
    - If file_path is a directory, upload all files recursively

    Args:
      file_path: Local file or directory path to upload
      s3_path: S3 path relative to base config path (will be combined)
    """
    try:
      bucket_name = self.target.bucket
      # Combine base path with s3_path using explicit / separator
      base_s3_path = self.get_full_path(s3_path)
      local_path = Path(file_path)

      if not local_path.exists():
        raise FileNotFoundError(f"The file or directory {file_path} was not found.")

      if local_path.is_file():
        # Single file upload
        s3_path = f"{base_s3_path}/{local_path.name}"
        await run_in_executor_with_context(self._client.upload_file, str(local_path), bucket_name, s3_path)
        return s3_path

      elif local_path.is_dir():
        # Directory upload - recursively upload all files
        uploaded_files = []
        for file in local_path.rglob('*'):
          if file.is_file():
            relative_path = file.relative_to(local_path)
            s3_path = f"{base_s3_path}/{local_path.name}/{relative_path}"
            await run_in_executor_with_context(self._client.upload_file, str(file), bucket_name, s3_path)
            uploaded_files.append(s3_path)

        return uploaded_files  # Return list of uploaded file paths

    except FileNotFoundError as e:
      raise Exception(str(e))
    except NoCredentialsError:
      raise Exception("S3 credentials are not valid.")
    except ClientError as e:
      raise Exception(f"Failed to upload to S3: {e}")

  async def download_key(self, s3_path, local_path, raw_path=False):
    """
    Download file from S3 to local filesystem.

    Args:
      s3_path: Full S3 key/path (relative to bucket, not combined with base config path)
      local_path: Local file path where to save
      raw_path: If True, s3_path is treated as the full key without combining with base path

    Returns:
      Path to downloaded file
    """
    try:
      # Use the provided s3_path as-is (should already be the full key from Backup record)
      bucket_name = self.target.bucket
      # Ensure s3_key doesn't have leading slash (S3 keys don't start with /)
      s3_key = s3_path.strip('/')
      if not raw_path:
        s3_key = self.get_full_path(s3_key)

      # Ensure local directory exists
      Path(local_path).parent.mkdir(parents=True, exist_ok=True)

      await run_in_executor_with_context(self._client.download_file, bucket_name, s3_key, local_path)
      return local_path

    except NoCredentialsError:
      raise Exception("S3 credentials are not valid.")
    except ClientError as e:
      raise Exception(f"Failed to download from S3: {e}")
    except Exception as e:
      raise Exception(f"Failed to download from S3: {e}")

  def _list_keys(self, full_prefix: str) -> list[str]:
    """Every object key under a full prefix, skipping directory markers. Blocking."""
    paginator = self._client.get_paginator('list_objects_v2')
    return [
        obj['Key']
        for page in paginator.paginate(Bucket=self.target.bucket, Prefix=full_prefix)
        for obj in page.get('Contents', [])
        if not obj['Key'].endswith('/')
    ]

  async def get_keys(self, prefix):
    """
    Get all S3 keys under the given prefix.

    Args:
      prefix: Relative path prefix (e.g., "/backups/platform")

    Returns:
      List of full S3 keys (relative to bucket) for all objects found
    """
    try:
      return await run_in_executor_with_context(self._list_keys, self.get_full_path(prefix))
    except NoCredentialsError:
      raise Exception("S3 credentials are not valid.")
    except ClientError as e:
      raise Exception(f"Failed to list S3 keys: {e}")
    except Exception as e:
      raise Exception(f"Failed to list S3 keys: {e}")
