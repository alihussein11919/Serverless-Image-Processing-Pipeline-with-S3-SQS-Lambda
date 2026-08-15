# Serverless Image Processing Pipeline
<img width="1932" height="542" alt="Serverless Image Processing Pipeline with S3, SQS   Lambda" src="https://github.com/user-attachments/assets/42c57c26-b152-4b23-855c-514233247746" />
Upload an image via a pre-signed URL → it's automatically validated, resized,
and watermarked → the thumbnail and metadata are stored → a notification is
sent and the result is served back over HTTPS.

Built and deployed manually (AWS Console, no IaC) in an AWS Academy Learner
Lab, then verified end-to-end through the public API.

## Solution Overview

This project solves a common backend need: accepting user image uploads,
processing them into standardized thumbnails, and making the results
available and discoverable, without provisioning or managing any servers.
The design decouples every stage with a queue so that traffic spikes,
transient failures, and processing time don't cascade into upload failures,
and every component scales to zero when idle.

## Architecture Diagram
<img width="1932" height="542" alt="Serverless Image Processing Pipeline with S3, SQS   Lambda" src="https://github.com/user-attachments/assets/42c57c26-b152-4b23-855c-514233247746" />
**Flow:** A client requests a pre-signed upload URL from **API Gateway**,
which invokes a **Lambda** function to generate it. The client uploads
directly to the **S3 source bucket** using that URL. The upload triggers an
**S3 event notification** to an **SQS queue**, which buffers the event and
decouples ingestion from processing. A second **Lambda** function (with a
**Lambda Layer** bundling Pillow) polls the queue, validates the image,
resizes and watermarks it, uploads the thumbnail to the **S3 destination
bucket**, writes metadata to **DynamoDB**, and publishes a result to an
**SNS** topic. Failed messages that exhaust retries land in a **dead-letter
queue (DLQ)** for inspection rather than being silently dropped. A separate
**AWS Step Functions** state machine wraps the same processing Lambda with
retry/catch logic, as a manually-invoked demonstration of orchestration —
it is not in the automatic upload path. The Image are then Cached to **Cloudfront** for quick, low-latency delivery.

## Services & Design Decisions

| Service | Role | Why |
|---|---|---|
| **API Gateway** | Public endpoint for pre-signed URL requests | Decouples clients from needing AWS credentials to upload |
| **Lambda (presign)** | Generates a scoped, time-limited S3 PUT URL | Only compute that can actually call `generate_presigned_url` — API Gateway alone cannot |
| **S3 (source)** | Receives raw uploads | Direct client→S3 upload avoids proxying file bytes through Lambda/API Gateway payload limits |
| **SQS + DLQ** | Buffers S3 events; isolates poison messages | Decouples ingestion from processing; a burst of uploads or a slow Lambda doesn't cause dropped events. Failed messages retry up to 3 times, then move to the DLQ instead of looping forever or being lost |
| **Lambda (process) + Layer** | Validates, resizes, watermarks | Pillow is packaged as a Layer, separate from function code, so it can be reused/updated independently |
| **DynamoDB** | Image metadata store | Serverless, on-demand billing, no capacity planning needed; no VPC required since it's a managed public-endpoint service reached via IAM |
| **SNS** | Job completion/failure notification | Simple pub/sub fan-out for alerting |
| **Step Functions** | Orchestration demo with retry/catch | Shows visual execution history and structured error handling around the same Lambda |
| **CloudFront** | *Designed, not deployed* — see Known Limitations | Intended as the production caching/delivery layer |

**No VPC anywhere in this design.** Every service used (S3, SQS, Lambda,
DynamoDB, SNS, API Gateway, CloudFront) is a fully managed, public-endpoint
AWS service reached over IAM — none of them require a private subnet, ENI,
or NAT Gateway. A VPC would only become necessary if a future component
(e.g. RDS, ElastiCache, or an internal-only service) required private
networking.

## Deployment / Setup Instructions

This was built manually via the AWS Console, in this order (order matters —
several resources reference each other's ARNs):

1. **DynamoDB** — create table `Image-metadata`, partition key `imageid`
   (String), on-demand billing.
2. **SNS** — create topic `Email_Alerts`, add an email subscription, confirm it.
3. **S3** — create the source and destination buckets (leave event
   notifications off for now).
4. **SQS** — create the dead-letter queue first, then the main queue with a
   redrive policy pointing at the DLQ (`maxReceiveCount: 3`), visibility
   timeout ≥ 6x the Lambda timeout (180s for a 30s function).
5. Add a **queue policy** on the main queue allowing `s3.amazonaws.com` to
   `SQS:SendMessage`, scoped via `aws:SourceArn` to the source bucket.
6. Go back to the **source bucket** and add an S3 event notification
   (`s3:ObjectCreated:*` → the SQS queue).
7. Build a **Pillow Lambda Layer** (must be built for Lambda's Linux
   runtime — a local `pip install` will not work; see `lambda/build-layer.md`).
8. Create the **process-image Lambda**: attach the layer, set environment
   variables (`DEST_BUCKET`, `METADATA_TABLE`, `NOTIFY_TOPIC_ARN`), attach
   the SQS trigger (batch size 5, report batch item failures enabled),
   bump timeout to 30s and memory to 512MB, use `LabRole` as the execution
   role (see Known Limitations).
9. Create the **presign-url Lambda**: set `SOURCE_BUCKET` env var, same
   execution role.
10. Create an **API Gateway** REST API, resource `/upload-url`, method
    `GET`, Lambda proxy integration to the presign Lambda, deploy to a
    stage.
11. Add **bucket policies and lifecycle rules** (see below).
12. *(Optional)* Build the **Step Functions** state machine
    (`stepfunctions/workflow.asl.json`) wrapping the process-image Lambda.

### Bucket policies & lifecycle rules

- **Source bucket**: HTTPS-only deny policy (`aws:SecureTransport: false` →
  deny); lifecycle rule expiring `uploads/*` after 30 days, since raw
  uploads are only needed transiently.
- **Destination bucket**: public read scoped to `thumbnails/*` only (see
  Known Limitations — this substitutes for CloudFront); lifecycle rule
  transitioning `thumbnails/*` to Standard-IA after 60 days.

## Known Limitations

- **CloudFront could not be deployed.**
<img width="1397" height="732" alt="Screenshot 2026-08-14 215047" src="https://github.com/user-attachments/assets/1cd82f26-a7d9-486c-9d76-656352494094" />
  `cloudfront:CreateOriginAccessControl`
  and `cloudfront:CreateDistribution` are both restricted in this AWS
  Academy Learner Lab's IAM policy. The architecture includes CloudFront as
  the intended production delivery/caching layer; in this environment,
  thumbnails are served via a scoped public-read S3 bucket policy instead.
  In production, CloudFront + Origin Access Control (private bucket, no
  direct public access) would be the correct approach.
- **`iam:CreateRole` is restricted.** All Lambda and Step Functions
  execution roles use the lab-provisioned `LabRole` rather than
  least-privilege custom policies. In production, each function would get
  a narrowly-scoped role (e.g. the processing Lambda would only get
  `s3:GetObject` on the source bucket and `s3:PutObject` on the
  destination bucket, not broad access).
- **SNS email delivery** was intermittently auto-unsubscribed, most likely
  due to email security systems pre-fetching (and thereby triggering) the
  one-click unsubscribe link in AWS's confirmation/notification emails.
  `sns.publish()` calls are confirmed successful in CloudWatch Logs
  regardless of subscription state.
- **The pre-signed URL API Gateway endpoint has no authentication.** Any
  caller who has the endpoint URL can request an upload URL. Adding an
  API key (Usage Plans) or a Cognito authorizer would be the next step for
  a production deployment.



