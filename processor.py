# processor.py
import json
import io
import boto3
import pandas as pd
from datetime import datetime

# This is the logging setup.
import logging
logger = logging.getLogger(__name__)

from baseline import BaselineManager
from detector import AnomalyDetector

s3 = boto3.client("s3")

NUMERIC_COLS = ["temperature", "humidity", "pressure", "wind_speed"]  # students configure this


def process_file(bucket: str, key: str):
    logger.info(f"process_file: Started — s3://{bucket}/{key}")

    # Here, we download the file
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
        df = pd.read_csv(io.BytesIO(response["Body"].read()))
        logger.info(f"process_file: Loaded {len(df)} rows, columns: {list(df.columns)}")
    except Exception as e:
        logger.error(f"process_file: Failed to download or parse s3://{bucket}/{key}: {e}")
        return None

    # Here, we load in the current baseline
    try:
        baseline_mgr = BaselineManager(bucket=bucket)
        baseline = baseline_mgr.load()
    except Exception as e:
        logger.error(f"process_file: Failed to load baseline: {e}")
        return None

    # Here, we update baseline with values from this batch BEFORE scoring
    #  Here, I use only non-null values for each channel
    for col in NUMERIC_COLS:
        try:
            if col in df.columns:
                clean_values = df[col].dropna().tolist()
                if clean_values:
                    baseline = baseline_mgr.update(baseline, col, clean_values)
                else:
                    logger.warning(f"process_file: Column '{col}' has no non-null values — skipping baseline update.")
            else:
                logger.warning(f"process_file: Expected column '{col}' not found in data — skipping.")
        except Exception as e:
            logger.error(f"process_file: Failed to update baseline for column '{col}': {e}")

    #  This Run detection
    try:
        detector = AnomalyDetector(z_threshold=3.0, contamination=0.05)
        scored_df = detector.run(df, NUMERIC_COLS, baseline, method="both")
        anomaly_count = int(scored_df["anomaly"].sum()) if "anomaly" in scored_df.columns else 0
        logger.info(f"process_file: Detection complete — {anomaly_count}/{len(df)} anomalies flagged")
    except Exception as e:
        logger.error(f"process_file: Detection failed: {e}")
        return None

    #  This Write scored file to processed/ prefix
    try:
        output_key = key.replace("raw/", "processed/")
        csv_buffer = io.StringIO()
        scored_df.to_csv(csv_buffer, index=False)
        s3.put_object(
            Bucket=bucket,
            Key=output_key,
            Body=csv_buffer.getvalue(),
            ContentType="text/csv"
        )
        logger.info(f"process_file: Scored CSV written to s3://{bucket}/{output_key}")
    except Exception as e:
        logger.error(f"process_file: Failed to write scored CSV to S3: {e}")
        return None

    #  This Save updated baseline back to S3
    # (baseline_mgr.save also syncs the log file to S3)
    try:
        baseline_mgr.save(baseline)
    except Exception as e:
        logger.error(f"process_file: Failed to save baseline: {e}")

    # this is the processing summary
    try:
        summary = {
            "source_key": key,
            "output_key": output_key,
            "processed_at": datetime.utcnow().isoformat(),
            "total_rows": len(df),
            "anomaly_count": anomaly_count,
            "anomaly_rate": round(anomaly_count / len(df), 4) if len(df) > 0 else 0,
            "baseline_observation_counts": {
                col: baseline.get(col, {}).get("count", 0) for col in NUMERIC_COLS
            }
        }

        summary_key = output_key.replace(".csv", "_summary.json")
        s3.put_object(
            Bucket=bucket,
            Key=summary_key,
            Body=json.dumps(summary, indent=2),
            ContentType="application/json"
        )
        logger.info(f"process_file: Summary written to s3://{bucket}/{summary_key} — "
                    f"anomaly_rate={summary['anomaly_rate']}, "
                    f"baseline_counts={summary['baseline_observation_counts']}")
    except Exception as e:
        logger.error(f"process_file: Failed to write summary JSON: {e}")
        return None

    logger.info(f"process_file: Finished — s3://{bucket}/{key}")
    return summary