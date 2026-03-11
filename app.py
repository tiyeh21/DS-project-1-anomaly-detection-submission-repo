# app.py
import io
import json
import logging
import os
import boto3
import pandas as pd
import requests
from datetime import datetime
from fastapi import FastAPI, BackgroundTasks, Request
from baseline import BaselineManager
from processor import process_file

# This is the logging setup for the entire application. All modules import this logger to ensure consistent logging to the same file and format.
LOG_FILE = "/opt/anomaly-detection/app.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),  # also prints to stdout/screen
    ],
)
logger = logging.getLogger(__name__)

# app and aws-setup
app = FastAPI(title="Anomaly Detection Pipeline")

s3 = boto3.client("s3")
BUCKET_NAME = os.environ["BUCKET_NAME"]

logger.info(f"Application started. Using S3 bucket: {BUCKET_NAME}")


# SNS subscription confirmation and message handler

@app.post("/notify")
async def handle_sns(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"/notify: Failed to parse request body: {e}")
        return {"status": "error", "detail": "Invalid JSON body"}

    msg_type = request.headers.get("x-amz-sns-message-type")
    logger.info(f"/notify: Received SNS message type: {msg_type}")

    #  Here, SNS sends a SubscriptionConfirmation before it will deliver any messages.
    # Here, visiting the SubscribeURL confirms the subscription.
    if msg_type == "SubscriptionConfirmation":
        try:
            confirm_url = body["SubscribeURL"]
            requests.get(confirm_url, timeout=10)
            logger.info(f"/notify: SNS subscription confirmed via {confirm_url}")
            return {"status": "confirmed"}
        except Exception as e:
            logger.error(f"/notify: Failed to confirm SNS subscription: {e}")
            return {"status": "error", "detail": "Subscription confirmation failed"}

    if msg_type == "Notification":
        try:
            s3_event = json.loads(body["Message"])
            for record in s3_event.get("Records", []):
                key = record["s3"]["object"]["key"]
                if key.startswith("raw/") and key.endswith(".csv"):
                    logger.info(f"/notify: New file arrived — queuing for processing: {key}")
                    background_tasks.add_task(process_file, BUCKET_NAME, key)
        except Exception as e:
            logger.error(f"/notify: Failed to parse S3 event from SNS message: {e}")
            return {"status": "error", "detail": "Failed to parse S3 event"}

    return {"status": "ok"}


# this is the query endpoint

@app.get("/anomalies/recent")
def get_recent_anomalies(limit: int = 50):
    """Return rows flagged as anomalies across the 10 most recent processed files."""
    try:
        paginator = s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=BUCKET_NAME, Prefix="processed/")

        objects = [
            obj
            for page in pages
            for obj in page.get("Contents", [])
            if obj["Key"].endswith(".csv")
        ]

        recent_objects = sorted(
            objects,
            key=lambda obj: obj["LastModified"],
            reverse=True
        )[:10]

        keys = [obj["Key"] for obj in recent_objects]

        all_anomalies = []
        for key in keys:
            try:
                response = s3.get_object(Bucket=BUCKET_NAME, Key=key)
                df = pd.read_csv(io.BytesIO(response["Body"].read()))
                if "anomaly" in df.columns:
                    flagged = df[df["anomaly"]].copy()
                    flagged["source_file"] = key
                    all_anomalies.append(flagged)
            except Exception as e:
                logger.error(f"/anomalies/recent: Failed to read {key}: {e}")
                continue

        if not all_anomalies:
            return {"count": 0, "anomalies": []}

        combined = pd.concat(all_anomalies).head(limit)
        logger.info(f"/anomalies/recent: Returned {len(combined)} anomaly rows")
        return {"count": len(combined), "anomalies": combined.to_dict(orient="records")}

    except Exception as e:
        logger.error(f"/anomalies/recent: Unexpected error: {e}")
        return {"error": str(e)}


@app.get("/anomalies/summary")
def get_anomaly_summary():
    """Aggregate anomaly rates across all processed files using their summary JSONs."""
    try:
        paginator = s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=BUCKET_NAME, Prefix="processed/")

        summaries = []
        for page in pages:
            for obj in page.get("Contents", []):
                if obj["Key"].endswith("_summary.json"):
                    try:
                        response = s3.get_object(Bucket=BUCKET_NAME, Key=obj["Key"])
                        summaries.append(json.loads(response["Body"].read()))
                    except Exception as e:
                        logger.error(f"/anomalies/summary: Failed to read {obj['Key']}: {e}")
                        continue

        if not summaries:
            return {"message": "No processed files yet."}

        total_rows = sum(s["total_rows"] for s in summaries)
        total_anomalies = sum(s["anomaly_count"] for s in summaries)

        logger.info(f"/anomalies/summary: {len(summaries)} files, {total_anomalies}/{total_rows} anomalies")
        return {
            "files_processed": len(summaries),
            "total_rows_scored": total_rows,
            "total_anomalies": total_anomalies,
            "overall_anomaly_rate": round(total_anomalies / total_rows, 4) if total_rows > 0 else 0,
            "most_recent": sorted(summaries, key=lambda x: x["processed_at"], reverse=True)[:5],
        }

    except Exception as e:
        logger.error(f"/anomalies/summary: Unexpected error: {e}")
        return {"error": str(e)}


@app.get("/baseline/current")
def get_current_baseline():
    """Show the current per-channel statistics the detector is working from."""
    try:
        baseline_mgr = BaselineManager(bucket=BUCKET_NAME)
        baseline = baseline_mgr.load()

        channels = {}
        for channel, stats in baseline.items():
            if channel == "last_updated":
                continue
            channels[channel] = {
                "observations": stats["count"],
                "mean": round(stats["mean"], 4),
                "std": round(stats.get("std", 0.0), 4),
                "baseline_mature": stats["count"] >= 30,
            }

        logger.info(f"/baseline/current: Returned stats for {len(channels)} channels")
        return {
            "last_updated": baseline.get("last_updated"),
            "channels": channels,
        }

    except Exception as e:
        logger.error(f"/baseline/current: Failed to load baseline: {e}")
        return {"error": str(e)}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "bucket": BUCKET_NAME,
        "timestamp": datetime.utcnow().isoformat()
    }