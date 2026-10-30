# Model card — call_surge_forecast

- **Version** `v90d788d2-d717`
- **Algorithm** LightGBM regressor
- **Trained on** 2026-02-23 23:00 to 2026-03-16 23:00 UTC (2,520 rows, 5 cells)
- **Data hash** `90d788d2813ddc39dfa1ca08fb788a865b36179acb5a427b63b66e173676a704`
- **Verdict** Beats its baseline on the held-out test fold.

## Purpose

One-hour-ahead forecast of complaints per 1,000 subscribers per cell, so care staffing and proactive notification can be prepared before the calls arrive.

## Metrics

```json
{
  "validation": {
    "mae": 0.004029,
    "rmse": 0.005322,
    "r2": 0.389865,
    "mape": 47.9142,
    "mape_rows": 304,
    "rows": 360,
    "mean_actual": 0.008686,
    "pred_std": 0.00448679,
    "mae_nonzero": 0.004023,
    "nonzero_rows": 304,
    "degenerate": false
  },
  "validation_baseline": {
    "mae": 0.005718,
    "rmse": 0.007715,
    "r2": -0.282218,
    "mape": 70.3921,
    "mape_rows": 304,
    "rows": 360,
    "mean_actual": 0.008686,
    "pred_std": 0.00652384,
    "mae_nonzero": 0.005958,
    "nonzero_rows": 304,
    "degenerate": false
  },
  "test": {
    "mae": 0.005655,
    "rmse": 0.014643,
    "r2": 0.057222,
    "mape": 56.4175,
    "mape_rows": 307,
    "rows": 360,
    "mean_actual": 0.009847,
    "pred_std": 0.00452036,
    "mae_nonzero": 0.005995,
    "nonzero_rows": 307,
    "degenerate": false
  },
  "test_baseline": {
    "mae": 0.006707,
    "rmse": 0.015141,
    "r2": -0.007938,
    "mape": 74.0017,
    "mape_rows": 307,
    "rows": 360,
    "mean_actual": 0.009847,
    "pred_std": 0.00678712,
    "mae_nonzero": 0.007317,
    "nonzero_rows": 307,
    "degenerate": false
  },
  "comparison": {
    "metric": "test_mae",
    "baseline": 0.006707,
    "model": 0.005655,
    "beats_baseline": true,
    "margin": 0.001052,
    "improvement_pct": 15.6821
  },
  "cv_folds": [
    {
      "fold": 1,
      "train_rows": 200,
      "validation_rows": 190,
      "mae": 0.004309,
      "rmse": 0.005453
    },
    {
      "fold": 2,
      "train_rows": 390,
      "validation_rows": 190,
      "mae": 0.004162,
      "rmse": 0.005277
    },
    {
      "fold": 3,
      "train_rows": 580,
      "validation_rows": 190,
      "mae": 0.003828,
      "rmse": 0.00574
    },
    {
      "fold": 4,
      "train_rows": 770,
      "validation_rows": 190,
      "mae": 0.004591,
      "rmse": 0.006108
    }
  ],
  "drivers": [
    {
      "feature": "hour_of_day",
      "mean_abs_shap": 0.32654147,
      "direction": "increases",
      "rank": 1
    },
    {
      "feature": "sinr_db_lag24",
      "mean_abs_shap": 0.05149381,
      "direction": "decreases",
      "rank": 2
    },
    {
      "feature": "active_users_lag24",
      "mean_abs_shap": 0.03972865,
      "direction": "increases",
      "rank": 3
    },
    {
      "feature": "rrc_setup_success_pct_lag24",
      "mean_abs_shap": 0.03890498,
      "direction": "decreases",
      "rank": 4
    },
    {
      "feature": "erab_setup_success_pct_lag1",
      "mean_abs_shap": 0.03670773,
      "direction": "decreases",
      "rank": 5
    },
    {
      "feature": "rrc_setup_success_pct_lag1",
      "mean_abs_shap": 0.02977754,
      "direction": "decreases",
      "rank": 6
    },
    {
      "feature": "is_busy_hour",
      "mean_abs_shap": 0.02726133,
      "direction": "increases",
      "rank": 7
    },
    {
      "feature": "complaint_count_lag2",
      "mean_abs_shap": 0.02585746,
      "direction": "increases",
      "rank": 8
    },
    {
      "feature": "erab_setup_success_pct_lag24",
      "mean_abs_shap": 0.02531375,
      "direction": "decreases",
      "rank": 9
    },
    {
      "feature": "sinr_db_lag1",
      "mean_abs_shap": 0.02457185,
      "direction": "decreases",
      "rank": 10
    },
    {
      "feature": "complaint_count_roll168_mean",
      "mean_abs_shap": 0.02451678,
      "direction": "decreases",
      "rank": 11
    },
    {
      "feature": "complaint_count_roll3_mean",
      "mean_abs_shap": 0.02380777,
      "direction": "decreases",
      "rank": 12
    },
    {
      "feature": "prb_utilisation_pct_lag24",
      "mean_abs_shap": 0.02347425,
      "direction": "increases",
      "rank": 13
    },
    {
      "feature": "active_users_lag1",
      "mean_abs_shap": 0.02216787,
      "direction": "increases",
      "rank": 14
    },
    {
      "feature": "prb_utilisation_pct_lag1",
      "mean_abs_shap": 0.02124401,
      "direction": "increases",
      "rank": 15
    }
  ]
}
```

## Parameters

```json
{
  "objective": "poisson",
  "n_estimators": 400,
  "learning_rate": 0.05,
  "num_leaves": 31,
  "min_child_samples": 40,
  "subsample": 0.9,
  "subsample_freq": 1,
  "colsample_bytree": 0.8,
  "reg_lambda": 1.0,
  "random_state": 20260301,
  "n_jobs": 1,
  "force_row_wise": true,
  "deterministic": true,
  "verbose": -1
}
```

## Features

- `availability_pct_lag1`
- `availability_pct_lag2`
- `availability_pct_lag24`
- `availability_pct_lag168`
- `availability_pct_roll24_mean`
- `rrc_setup_success_pct_lag1`
- `rrc_setup_success_pct_lag2`
- `rrc_setup_success_pct_lag24`
- `rrc_setup_success_pct_lag168`
- `rrc_setup_success_pct_roll24_mean`
- `erab_setup_success_pct_lag1`
- `erab_setup_success_pct_lag2`
- `erab_setup_success_pct_lag24`
- `erab_setup_success_pct_lag168`
- `erab_setup_success_pct_roll24_mean`
- `drop_rate_pct_lag1`
- `drop_rate_pct_lag2`
- `drop_rate_pct_lag24`
- `drop_rate_pct_lag168`
- `drop_rate_pct_roll24_mean`
- `prb_utilisation_pct_lag1`
- `prb_utilisation_pct_lag2`
- `prb_utilisation_pct_lag24`
- `prb_utilisation_pct_lag168`
- `prb_utilisation_pct_roll24_mean`
- `dl_throughput_mbps_lag1`
- `dl_throughput_mbps_lag2`
- `dl_throughput_mbps_lag24`
- `dl_throughput_mbps_lag168`
- `dl_throughput_mbps_roll24_mean`
- `ul_throughput_mbps_lag1`
- `ul_throughput_mbps_lag2`
- `ul_throughput_mbps_lag24`
- `ul_throughput_mbps_lag168`
- `ul_throughput_mbps_roll24_mean`
- `latency_ms_lag1`
- `latency_ms_lag2`
- `latency_ms_lag24`
- `latency_ms_lag168`
- `latency_ms_roll24_mean`
- `active_users_lag1`
- `active_users_lag2`
- `active_users_lag24`
- `active_users_lag168`
- `active_users_roll24_mean`
- `sinr_db_lag1`
- `sinr_db_lag2`
- `sinr_db_lag24`
- `sinr_db_lag168`
- `sinr_db_roll24_mean`
- `complaint_count_lag1`
- `complaint_count_lag2`
- `complaint_count_lag24`
- `complaint_count_lag168`
- `complaint_count_roll3_mean`
- `complaint_count_roll24_mean`
- `complaint_count_roll168_mean`
- `complaints_per_1k_lag1`
- `complaints_per_1k_lag2`
- `complaints_per_1k_lag24`
- `complaints_per_1k_lag168`
- `complaints_per_1k_roll3_mean`
- `complaints_per_1k_roll24_mean`
- `complaints_per_1k_roll168_mean`
- `care_call_count_lag1`
- `care_call_count_lag2`
- `care_call_count_lag24`
- `care_call_count_lag168`
- `care_call_count_roll3_mean`
- `care_call_count_roll24_mean`
- `care_call_count_roll168_mean`
- `hour_of_day`
- `day_of_week`
- `is_weekend`
- `is_busy_hour`
- `subscribers_est`

## Limitations

- Trained on synthetic data. Every metric here is optimistic: the seed's relationships are the ones the generator put there, and a real network is noisier and less consistent.
- Validated on a single time-ordered split plus expanding-window CV. No shuffled cross-validation is used anywhere, and none should be.
- Features are cell-level. A cell whose `subscribers_est` is stale will be forecast badly and nothing downstream can detect that from the forecast.
- A one-hour horizon only. Nothing here supports a longer forecast.

## Ethical and operational notes

- The model informs a human. It executes nothing: no runbook, no customer
  message, no network change follows from its output (PRD §9, §11).
- No subscriber identifier reaches training. Features are cell-level
  aggregates; complaint counts are normalised by `cell.subscribers_est`.
- Retraining is a new version, never an edit. The registry refuses to
  overwrite a version, because the data hash recorded against it is a
  promise about which data produced which artifact.

_Generated by `make train`. Do not edit by hand._
