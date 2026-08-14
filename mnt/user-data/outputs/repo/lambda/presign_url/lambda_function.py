"""
API Gateway (GET /upload-url) -> returns a pre-signed S3 PUT URL so clients
upload directly to S3 without proxying bytes through Lambda/API Gateway.
"""
import json
import os
import uuid

import boto3
from botocore.config import Config

s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
SOURCE_BUCKET = os.environ["SOURCE_BUCKET"]
URL_EXPIRY_SECONDS = 300

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}


def lambda_handler(event, context):
    params = event.get("queryStringParameters") or {}
    content_type = params.get("contentType", "image/jpeg")

    if content_type not in ALLOWED_CONTENT_TYPES:
        return _response(400, {"error": f"Unsupported contentType: {content_type}"})

    ext = content_type.split("/")[-1]
    key = f"uploads/{uuid.uuid4()}.{ext}"

    url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": SOURCE_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=URL_EXPIRY_SECONDS,
    )

    return _response(200, {"uploadUrl": url, "key": key, "expiresIn": URL_EXPIRY_SECONDS})


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
