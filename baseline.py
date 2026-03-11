# baseline.py
import json
import math
import os
import boto3
from datetime import datetime
from typing import Optional
from botocore.exceptions import ClientError

# this is the loggins setup that app.py draws from
import logging
logger = logging.getLogger(__name__)

s3 = boto3.client("s3")

LOG_FILE = "/opt/anomaly-detection/app.log"


def sync_log_to_s3(bucket: str):
    """Push the local log file to s3://BUCKET_NAME/logs/app.log."""
    try:
        if not os.path.exists(LOG_FILE):
            logger.warning(f"Log file does not exist yet: {LOG_FILE}")
            return
        s3.upload_file(LOG_FILE, bucket, "logs/app.log")
        logger.info("Log file synced to S3: logs/app.log")
    except Exception as e:
        logger.error(f"Failed to sync log file to S3: {e}")


class BaselineManager:
    """
    Maintains a per-channel running baseline using Welford's online algorithm,
    which computes mean and variance incrementally without storing all past data.
    """

    def __init__(self, bucket: str, baseline_key: str = "state/baseline.json"):
        self.bucket = bucket
        self.baseline_key = baseline_key

    def load(self) -> dict:
        try:
            response = s3.get_object(Bucket=self.bucket, Key=self.baseline_key)
            baseline = json.loads(response["Body"].read())
            logger.info(f"Baseline loaded from S3: {self.baseline_key}")
            return baseline
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code in ("NoSuchKey", "404"):
                logger.info("No existing baseline found in S3 — starting fresh.")
                return {}
            logger.error(f"Failed to load baseline from S3: {e}")
            return {}
        except Exception as e:
            logger.error(f"Failed to load baseline from S3: {e}")
            return {}

    def save(self, baseline: dict):
        try:
            baseline["last_updated"] = datetime.utcnow().isoformat()
            s3.put_object(
                Bucket=self.bucket,
                Key=self.baseline_key,
                Body=json.dumps(baseline, indent=2),
                ContentType="application/json"
            )
            logger.info(
                f"Baseline saved to S3: {self.baseline_key} "
                f"(channels: {[k for k in baseline if k != 'last_updated']})"
            )

            # This sync logs to S3 every time baseline is pushed
            sync_log_to_s3(self.bucket)

        except Exception as e:
            logger.error(f"Failed to save baseline to S3: {e}")

    def update(self, baseline: dict, channel: str, new_values: list[float]) -> dict:
        """
        Welford's online algorithm for numerically stable mean and variance.
        Each channel tracks: count, mean, M2 (sum of squared deviations).
        Variance = M2 / count, std = sqrt(variance).
        """
        try:
            if channel not in baseline:
                baseline[channel] = {"count": 0, "mean": 0.0, "M2": 0.0}

            state = baseline[channel]

            clean_values = []
            for value in new_values:
                if isinstance(value, (int, float)) and not math.isnan(value):
                    clean_values.append(float(value))

            for value in clean_values:
                state["count"] += 1
                delta = value - state["mean"]
                state["mean"] += delta / state["count"]
                delta2 = value - state["mean"]
                state["M2"] += delta * delta2

            if state["count"] >= 2:
                variance = state["M2"] / state["count"]
                state["std"] = math.sqrt(variance)
            else:
                state["std"] = 0.0

            baseline[channel] = state

            logger.info(
                f"Baseline updated — channel: {channel}, "
                f"count: {state['count']}, "
                f"mean: {round(state['mean'], 4)}, "
                f"std: {round(state['std'], 4)}"
            )

        except Exception as e:
            logger.error(f"Failed to update baseline for channel '{channel}': {e}")

        return baseline

    def get_stats(self, baseline: dict, channel: str) -> Optional[dict]:
        try:
            return baseline.get(channel)
        except Exception as e:
            logger.error(f"Failed to get stats for channel '{channel}': {e}")
            return None