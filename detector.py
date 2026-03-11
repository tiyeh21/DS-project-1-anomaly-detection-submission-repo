# dectector.py 
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from typing import Optional

# loggint setup
import logging
logger = logging.getLogger(__name__)


class AnomalyDetector:

    def __init__(self, z_threshold: float = 3.0, contamination: float = 0.05):
        self.z_threshold = z_threshold
        self.contamination = contamination  # expected fraction of anomalies

    def zscore_flag(
        self,
        values: pd.Series,
        mean: float,
        std: float
    ) -> pd.Series:
        """
        Flag values more than z_threshold standard deviations from the
        established baseline mean. Returns a Series of z-scores.
        """
        try:
            if std == 0:
                logger.warning(f"zscore_flag: std is 0 for this channel — returning zero scores.")
                return pd.Series([0.0] * len(values))
            return (values - mean).abs() / std
        except Exception as e:
            logger.error(f"zscore_flag: Failed to compute z-scores: {e}")
            return pd.Series([0.0] * len(values))

    def isolation_forest_flag(self, df: pd.DataFrame, numeric_cols: list[str]) -> tuple:
        """
        Multivariate anomaly detection across all numeric channels simultaneously.
        IsolationForest returns -1 for anomalies, 1 for normal points.
        Scores closer to -1 indicate stronger anomalies.
        """
        try:
            model = IsolationForest(
                contamination=self.contamination,
                random_state=42,
                n_estimators=100
            )
            X = df[numeric_cols].fillna(df[numeric_cols].median())
            model.fit(X)

            labels = model.predict(X)            # -1 = anomaly, 1 = normal
            scores = model.decision_function(X)  # lower = more anomalous

            anomaly_count = int((labels == -1).sum())
            logger.info(f"IsolationForest: {anomaly_count}/{len(labels)} rows flagged as anomalies "
                        f"across columns: {numeric_cols}")

            return labels, scores

        except Exception as e:
            logger.error(f"isolation_forest_flag: Failed to run IsolationForest: {e}")
            # Return safe fallback arrays — all normal, zero scores
            n = len(df)
            return np.ones(n, dtype=int), np.zeros(n)

    def run(
        self,
        df: pd.DataFrame,
        numeric_cols: list[str],
        baseline: dict,
        method: str = "both"
    ) -> pd.DataFrame:
        try:
            result = df.copy()
            logger.info(f"AnomalyDetector.run: Starting detection on {len(df)} rows "
                        f"using method='{method}', columns={numeric_cols}")

            # --- Z-score per channel ---
            if method in ("zscore", "both"):
                for col in numeric_cols:
                    try:
                        stats = baseline.get(col)
                        if stats and stats["count"] >= 30:  # need enough history to trust baseline
                            z_scores = self.zscore_flag(df[col], stats["mean"], stats["std"])
                            result[f"{col}_zscore"] = z_scores.round(4)
                            result[f"{col}_zscore_flag"] = z_scores > self.z_threshold
                            flagged = int((z_scores > self.z_threshold).sum())
                            logger.info(f"Z-score — channel: {col}, flagged: {flagged}/{len(df)} rows "
                                        f"(mean={round(stats['mean'], 4)}, std={round(stats['std'], 4)})")
                        else:
                            # Not enough baseline history yet — flag as unknown
                            result[f"{col}_zscore"] = None
                            result[f"{col}_zscore_flag"] = None
                            count = stats["count"] if stats else 0
                            logger.info(f"Z-score — channel: {col} skipped, "
                                        f"insufficient baseline observations ({count}/30 needed)")
                    except Exception as e:
                        logger.error(f"Z-score: Failed for channel '{col}': {e}")
                        result[f"{col}_zscore"] = None
                        result[f"{col}_zscore_flag"] = None

            # --- IsolationForest across all channels ---
            if method in ("isolation", "both"):
                labels, scores = self.isolation_forest_flag(df, numeric_cols)
                result["if_label"] = labels           # -1 or 1
                result["if_score"] = scores.round(4)  # continuous anomaly score
                result["if_flag"] = labels == -1

            # --- Consensus flag: anomalous by at least one method ---
            if method == "both":
                try:
                    zscore_flags = [
                        result[f"{col}_zscore_flag"]
                        for col in numeric_cols
                        if f"{col}_zscore_flag" in result.columns
                        and result[f"{col}_zscore_flag"].notna().any()
                    ]
                    if zscore_flags:
                        any_zscore = pd.concat(zscore_flags, axis=1).any(axis=1)
                        result["anomaly"] = any_zscore | result["if_flag"]
                    else:
                        result["anomaly"] = result["if_flag"]

                    total_anomalies = int(result["anomaly"].sum())
                    logger.info(f"Consensus flag: {total_anomalies}/{len(result)} rows marked anomaly=True")

                except Exception as e:
                    logger.error(f"Consensus flagging failed: {e}")
                    result["anomaly"] = result.get("if_flag", False)

            return result

        except Exception as e:
            logger.error(f"AnomalyDetector.run: Unexpected error during detection: {e}")
            # Return original df with anomaly column set to False as a safe fallback
            df["anomaly"] = False
            return df