"""
Consumes S3 ObjectCreated events (delivered via SQS), then:
  1. Downloads the original image from the source bucket
  2. Validates it (format/size)
  3. Resizes + watermarks it
  4. Uploads the thumbnail to the destination bucket
  5. Writes metadata to DynamoDB
  6. Publishes a completion/failure notification to SNS

Uses partial batch failure reporting so only the failed SQS records get
retried / sent to the DLQ, not the whole batch.
"""
import io
import json
import logging
import os
import time
import uuid
from urllib.parse import unquote_plus

import boto3
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

s3 = boto3.client("s3")
ddb = boto3.resource("dynamodb")
sns = boto3.client("sns")

DEST_BUCKET = os.environ["DEST_BUCKET"]
TABLE = ddb.Table(os.environ["METADATA_TABLE"])
TOPIC_ARN = os.environ["NOTIFY_TOPIC_ARN"]

THUMBNAIL_SIZE = (400, 400)
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
MAX_SOURCE_BYTES = 25 * 1024 * 1024  # 25 MB guardrail


def lambda_handler(event, context):
    batch_item_failures = []

    for record in event["Records"]:
        message_id = record["messageId"]
        try:
            _process_record(record)
        except Exception as exc:
            logger.exception("Failed processing message %s: %s", message_id, exc)
            batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}


def _process_record(record):
    body = json.loads(record["body"])

    for s3_record in body.get("Records", []):
        bucket = s3_record["s3"]["bucket"]["name"]
        key = unquote_plus(s3_record["s3"]["object"]["key"])
        image_id = str(uuid.uuid4())

        try:
            original_bytes = _download(bucket, key)
            img = _validate_and_open(original_bytes)
            thumbnail_bytes, fmt = _resize_and_watermark(img)

            dest_key = f"thumbnails/{key.rsplit('.', 1)[0]}_thumb.{fmt.lower()}"
            s3.put_object(
                Bucket=DEST_BUCKET,
                Key=dest_key,
                Body=thumbnail_bytes,
                ContentType=f"image/{fmt.lower()}",
            )

            _write_metadata(image_id, bucket, key, dest_key, img.size, fmt)
            _notify(
                subject="Image processed successfully",
                message=f"{key} -> {dest_key} (imageId={image_id})",
            )

        except (UnidentifiedImageError, ValueError) as validation_err:
            # Bad input — don't retry, just notify and move on.
            logger.warning("Validation failed for %s/%s: %s", bucket, key, validation_err)
            _notify(subject="Image processing failed (validation)", message=str(validation_err))
            continue

        except Exception:
            # Unknown failure — re-raise so this record is retried / DLQ'd.
            _notify(subject="Image processing failed", message=f"{bucket}/{key} failed unexpectedly")
            raise


def _download(bucket, key):
    head = s3.head_object(Bucket=bucket, Key=key)
    if head["ContentLength"] > MAX_SOURCE_BYTES:
        raise ValueError(f"Object {key} exceeds max size ({head['ContentLength']} bytes)")
    obj = s3.get_object(Bucket=bucket, Key=key)
    return obj["Body"].read()


def _validate_and_open(raw_bytes):
    img = Image.open(io.BytesIO(raw_bytes))
    img.verify()  # raises UnidentifiedImageError if corrupt
    img = Image.open(io.BytesIO(raw_bytes))  # reopen after verify()
    if img.format not in ALLOWED_FORMATS:
        raise ValueError(f"Unsupported format: {img.format}")
    return img


def _resize_and_watermark(img):
    img = img.convert("RGB") if img.format == "JPEG" else img.convert("RGBA")
    img.thumbnail(THUMBNAIL_SIZE, Image.LANCZOS)

    # Simple text-free watermark: semi-transparent corner overlay.
    # (Swap in ImageDraw.text with a bundled font from the Lambda layer for a real logo/text mark.)
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    watermark_box = Image.new("RGBA", (img.width, 24), (0, 0, 0, 90))
    overlay.paste(watermark_box, (0, img.height - 24))
    img = Image.alpha_composite(img.convert("RGBA"), overlay) if img.mode == "RGBA" else img

    buf = io.BytesIO()
    fmt = "PNG" if img.mode == "RGBA" else "JPEG"
    img.convert("RGB" if fmt == "JPEG" else "RGBA").save(buf, format=fmt, quality=85)
    return buf.getvalue(), fmt


def _write_metadata(image_id, src_bucket, src_key, dest_key, dimensions, fmt):
    TABLE.put_item(
        Item={
            "imageid": image_id,
            "sourceBucket": src_bucket,
            "sourceKey": src_key,
            "destinationKey": dest_key,
            "width": dimensions[0],
            "height": dimensions[1],
            "format": fmt,
            "processedAt": int(time.time()),
        }
    )


def _notify(subject, message):
    try:
        sns.publish(TopicArn=TOPIC_ARN, Subject=subject[:100], Message=message)
    except Exception:
        logger.exception("Failed to publish SNS notification")
