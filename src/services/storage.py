"""
Cloudflare R2 object storage via S3-compatible API (boto3).

R2 speaks the S3 protocol — boto3 is pointed at the R2 endpoint URL.
Requires R2-specific credentials (not the Cloudflare account API token):
  CLOUDFARE_ACCOUNT_ID, CLOUDFARE_R2_ACCESS_KEY_ID, CLOUDFARE_R2_SECRET_ACCESS_KEY
Create these at: Cloudflare Dashboard → R2 → Manage R2 API Tokens
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


class CloudflareR2Storage:
    _instance: "CloudflareR2Storage | None" = None
    _client: Any | None = None

    def __new__(cls) -> "CloudflareR2Storage":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def is_enabled(self) -> bool:
        from config import settings
        return bool(
            settings.cloudfare_account_id
            and settings.cloudfare_r2_access_key_id
            and settings.cloudfare_r2_secret_access_key
        )

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3
            from botocore.config import Config
            from config import settings

            self._client = boto3.client(
                "s3",
                endpoint_url=f"https://{settings.cloudfare_account_id}.r2.cloudflarestorage.com",
                aws_access_key_id=settings.cloudfare_r2_access_key_id,
                aws_secret_access_key=settings.cloudfare_r2_secret_access_key,
                region_name="auto",
                config=Config(signature_version="s3v4"),
            )
            logger.info(
                "Cloudflare R2 client initialized (bucket: %s)", settings.cloudfare_r2_bucket
            )
        return self._client

    # ------------------------------------------------------------------
    # Internal sync helpers (boto3 is synchronous — run via to_thread)
    # ------------------------------------------------------------------

    def _upload_sync(
        self,
        content: bytes,
        object_key: str,
        mime_type: str,
        bucket: str,
    ) -> None:
        client = self._get_client()
        client.put_object(
            Bucket=bucket,
            Key=object_key,
            Body=content,
            ContentType=mime_type or "application/octet-stream",
        )
        logger.info("Uploaded to R2: bucket=%s key=%s", bucket, object_key)

    def _delete_sync(self, object_key: str, bucket: str) -> None:
        client = self._get_client()
        client.delete_object(Bucket=bucket, Key=object_key)
        logger.info("Deleted from R2: bucket=%s key=%s", bucket, object_key)

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def upload_file(
        self,
        content: bytes,
        filename: str,
        mime_type: str,
    ) -> tuple[str, str]:
        """
        Upload *content* to R2.
        Returns (object_key, public_url).
        public_url is empty if CLOUDFARE_R2_PUBLIC_URL is not configured.
        """
        from config import settings

        object_key = f"{uuid.uuid4()}/{filename}"

        await asyncio.to_thread(
            self._upload_sync,
            content,
            object_key,
            mime_type,
            settings.cloudfare_r2_bucket,
        )

        public_url = ""
        if settings.cloudfare_r2_public_url:
            public_url = f"{settings.cloudfare_r2_public_url.rstrip('/')}/{object_key}"

        return object_key, public_url

    async def delete_file(self, object_key: str) -> None:
        """Delete an object from R2 by its key."""
        from config import settings
        await asyncio.to_thread(self._delete_sync, object_key, settings.cloudfare_r2_bucket)


r2_storage = CloudflareR2Storage()
