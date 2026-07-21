import asyncio
import logging
from pathlib import Path
from urllib.parse import urlparse

import aioboto3
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, NoCredentialsError

from services.base import Base
from services.settings import SETTING_DEFINITIONS, Settings

logger = logging.getLogger(__name__)


class S3(Base):
  def __init__(self):
    super().__init__()

    self.s3_config = None

    self._session = aioboto3.Session()

    self.load()

    if not self.s3_config:
      logger.warning("No complete S3 config found in settings, S3 functionality will be disabled")

  def load(self):
    with self.lock:
      settings = Settings()
      config = settings.get("s3")
      self.s3_config = config if settings.is_valid("s3") else None

  def check_config(self, config: dict):
    """Validate a candidate S3 config. It must be fully valid or fully empty"""
    try:
      provided = [config.get(key) for key in SETTING_DEFINITIONS["s3"]]
      if not any(provided):
        return True
      if not all(provided):
        return False

      client_kwargs = self._get_client_kwargs(config)
      client = boto3.client('s3', **client_kwargs)
      client.list_objects_v2(
          Bucket=config["bucket"],
          Prefix=config["path"].strip('/'),
          MaxKeys=1,
      )
    except Exception:
      return False

    return True

  def check_config_exists(self):
    if not self.s3_config:
      raise Exception("S3 configuration is not set. Please set it in settings.")

  def get_full_path(self, s3_path):
    """
    Get the full S3 path by combining the base path from config with the provided s3_path.
    """
    self.check_config_exists()
    return f"{self.s3_config['path'].strip('/')}/{s3_path.strip('/')}"

  def _get_region(self, endpoint):
    hostname = urlparse(endpoint).hostname or ""
    if hostname.startswith("s3.") and hostname.endswith(".backblazeb2.com"):
      parts = hostname.split(".")
      if len(parts) >= 4:
        return parts[1]

    return None

  def _get_client_kwargs(self, config=None):
    if config is None:
      config = self.s3_config

    client_kwargs = {
        "aws_access_key_id": config['access_key'],
        "aws_secret_access_key": config['secret_key'],
        "endpoint_url": config['endpoint'],
        "config": Config(signature_version='s3v4'),
    }

    region_name = self._get_region(config['endpoint'])
    if region_name:
      client_kwargs["region_name"] = region_name

    return client_kwargs

  def create_presigned_upload_url(self, s3_path, expires_in=3600, raw_path=False):
    """
    Create a presigned PUT URL for direct uploads.
    """
    self.check_config_exists()

    client = boto3.client('s3', **self._get_client_kwargs())

    if raw_path:
      s3_key = s3_path.strip('/')
    else:
      s3_key = self.get_full_path(s3_path)

    return client.generate_presigned_url(
        'put_object',
        Params={
            'Bucket': self.s3_config['bucket'],
            'Key': s3_key,
            'ContentType': 'application/octet-stream',
        },
        ExpiresIn=expires_in,
        HttpMethod='PUT',
    )

  def create_presigned_download_url(self, s3_path, expires_in=3600, raw_path=False):
    """
    Create a presigned GET URL for direct downloads.
    """
    self.check_config_exists()

    client = boto3.client('s3', **self._get_client_kwargs())

    if raw_path:
      s3_key = s3_path.strip('/')
    else:
      s3_key = self.get_full_path(s3_path)

    return client.generate_presigned_url(
        'get_object',
        Params={
            'Bucket': self.s3_config['bucket'],
            'Key': s3_key,
        },
        ExpiresIn=expires_in,
        HttpMethod='GET',
    )

  async def delete_key(self, s3_path, filter=None, max_retries=3, raw_path=False):
    """
    Delete files/folders from S3 based on a filter function (async).

    Args:
      s3_path: S3 path relative to base config path (will be combined)
      filter: Callable that takes S3 key/path string and returns True to delete, False to keep.
                  Example: lambda key: "old_" in key
                  If None, deletes all objects at the prefix.
      max_retries: Maximum number of retry attempts (default: 3)
      raw_path: If True, s3_path is treated as the full key without combining with base path.


    Returns:
      List of deleted S3 paths
    """
    self.check_config_exists()

    session = self._session

    for attempt in range(max_retries):
      try:
        async with session.client('s3', **self._get_client_kwargs()) as s3_client:
          bucket_name = self.s3_config['bucket']
          deleted_paths = []

          # List all objects at the given prefix
          if raw_path:
            prefix = s3_path.strip('/')
          else:
            prefix = self.get_full_path(s3_path)
          paginator = s3_client.get_paginator('list_objects_v2')
          pages = paginator.paginate(Bucket=bucket_name, Prefix=prefix)

          objects_to_delete = []
          async for page in pages:
            if 'Contents' not in page:
              continue

            for obj in page['Contents']:
              # Skip if it's a "directory" marker
              if obj['Key'].endswith('/'):
                continue

              # Apply filter function if provided
              if filter is None or filter(obj['Key']):
                objects_to_delete.append({'Key': obj['Key']})

          # Delete all matching objects
          if objects_to_delete:
            # S3 delete_objects is limited to 1000 objects per request
            for i in range(0, len(objects_to_delete), 1000):
              batch = objects_to_delete[i:i + 1000]
              response = await s3_client.delete_objects(
                  Bucket=bucket_name,
                  Delete={'Objects': batch}
              )

              # Track deleted paths
              if 'Deleted' in response:
                for deleted_obj in response['Deleted']:
                  deleted_paths.append(deleted_obj['Key'])

              # Log any errors
              if 'Errors' in response:
                for error_obj in response['Errors']:
                  raise Exception(f"Failed to delete {error_obj['Key']}: {error_obj['Message']}")

          return deleted_paths

      except NoCredentialsError:
        raise Exception("S3 credentials are not valid.")
      except ClientError as e:
        if attempt < max_retries - 1:
          wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
          await asyncio.sleep(wait_time)
        else:
          raise Exception(f"Failed to delete from S3 after {max_retries} attempts: {e}")
      except Exception:
        if attempt < max_retries - 1:
          wait_time = 2 ** attempt
          await asyncio.sleep(wait_time)
        else:
          raise

  async def upload_to_key(self, file_path, s3_path):
    """
    Upload file or directory to S3 (async).
    - If file_path is a file, upload it directly
    - If file_path is a directory, upload all files recursively

    Args:
      file_path: Local file or directory path to upload
      s3_path: S3 path relative to base config path (will be combined)
    """
    self.check_config_exists()

    session = self._session

    try:
      async with session.client('s3', **self._get_client_kwargs()) as s3_client:
        bucket_name = self.s3_config['bucket']
        # Combine base path with s3_path using explicit / separator
        base_s3_path = self.get_full_path(s3_path)
        local_path = Path(file_path)

        if not local_path.exists():
          raise FileNotFoundError(f"The file or directory {file_path} was not found.")

        if local_path.is_file():
          # Single file upload
          s3_path = f"{base_s3_path}/{local_path.name}"
          await s3_client.upload_file(str(local_path), bucket_name, s3_path)
          return s3_path

        elif local_path.is_dir():
          # Directory upload - recursively upload all files
          uploaded_files = []
          for file in local_path.rglob('*'):
            if file.is_file():
              relative_path = file.relative_to(local_path)
              s3_path = f"{base_s3_path}/{local_path.name}/{relative_path}"
              await s3_client.upload_file(str(file), bucket_name, s3_path)
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
    self.check_config_exists()

    session = self._session

    try:
      # Use the provided s3_path as-is (should already be the full key from Backup record)
      bucket_name = self.s3_config['bucket']
      # Ensure s3_key doesn't have leading slash (S3 keys don't start with /)
      s3_key = s3_path.strip('/')
      if not raw_path:
        s3_key = self.get_full_path(s3_key)

      async with session.client('s3', **self._get_client_kwargs()) as s3_client:
        # Ensure local directory exists
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)

        # Download file
        await s3_client.download_file(bucket_name, s3_key, local_path)

        return local_path

    except NoCredentialsError:
      raise Exception("S3 credentials are not valid.")
    except ClientError as e:
      raise Exception(f"Failed to download from S3: {e}")
    except Exception as e:
      raise Exception(f"Failed to download from S3: {e}")

  async def get_keys(self, prefix):
    """
    Get all S3 keys under the given prefix.

    Args:
      prefix: Relative path prefix (e.g., "/backups/platform")

    Returns:
      List of full S3 keys (relative to bucket) for all objects found
    """
    self.check_config_exists()

    session = self._session
    keys = []

    try:
      async with session.client('s3', **self._get_client_kwargs()) as s3_client:
        bucket_name = self.s3_config['bucket']

        # Get the full S3 prefix (combine base path with provided prefix)
        full_prefix = self.get_full_path(prefix)

        # List all objects at the prefix
        paginator = s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=bucket_name, Prefix=full_prefix)

        async for page in pages:
          if 'Contents' not in page:
            continue

          for obj in page['Contents']:
            # Skip directory markers
            if obj['Key'].endswith('/'):
              continue

            # Add the full S3 key to our list
            keys.append(obj['Key'])

        return keys

    except NoCredentialsError:
      raise Exception("S3 credentials are not valid.")
    except ClientError as e:
      raise Exception(f"Failed to list S3 keys: {e}")
    except Exception as e:
      raise Exception(f"Failed to list S3 keys: {e}")
