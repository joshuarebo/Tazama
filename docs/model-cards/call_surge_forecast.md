# Model card — call_surge_forecast

- **Version** `v2da60515-4c7f`
- **Algorithm** LightGBM regressor
- **Trained on** 2026-02-23 23:00 to 2026-03-16 23:00 UTC (20,160 rows, 40 cells)
- **Data hash** `2da605152e81a5677c073b04e0702c7f8084117b7eef882d22bcffb980e13bd9`
- **Verdict** Beats its baseline on the held-out test fold.

## Purpose

One-hour-ahead forecast of complaints per 1,000 subscribers per cell, so care staffing and proactive notification can be prepared before the calls arrive.

## Metrics

```json
{
  "validation": {
    "mae": 0.008998,
    "rmse": 0.093407,
    "r2": -0.009366,
    "mape": 100.0,
    "mape_rows": 56,
    "rows": 2880,
    "mean_actual": 0.008998
  },
  "validation_baseline": {
    "mae": 0.01866,
    "rmse": 0.127716,
    "r2": -0.887032,
    "mape": 98.2143,
    "mape_rows": 56,
    "rows": 2880,
    "mean_actual": 0.008998
  },
  "test": {
    "mae": 0.011305,
    "rmse": 0.106295,
    "r2": -0.011441,
    "mape": 100.0,
    "mape_rows": 71,
    "rows": 2880,
    "mean_actual": 0.011305
  },
  "test_baseline": {
    "mae": 0.01891,
    "rmse": 0.130446,
    "r2": -0.52328,
    "mape": 94.3662,
    "mape_rows": 71,
    "rows": 2880,
    "mean_actual": 0.011305
  },
  "comparison": {
    "metric": "test_mae",
    "baseline": 0.01891,
    "model": 0.011305,
    "beats_baseline": true,
    "margin": 0.007604,
    "improvement_pct": 40.2145
  },
  "cv_folds": [
    {
      "fold": 1,
      "train_rows": 1600,
      "validation_rows": 1520,
      "mae": 0.004439,
      "rmse": 0.037667
    },
    {
      "fold": 2,
      "train_rows": 3120,
      "validation_rows": 1520,
      "mae": 0.005983,
      "rmse": 0.067232
    },
    {
      "fold": 3,
      "train_rows": 4640,
      "validation_rows": 1520,
      "mae": 0.008059,
      "rmse": 0.085964
    },
    {
      "fold": 4,
      "train_rows": 6160,
      "validation_rows": 1520,
      "mae": 0.009971,
      "rmse": 0.081696
    }
  ],
  "drivers": []
}
```

## Parameters

```json
{
  "objective": "regression_l1",
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
