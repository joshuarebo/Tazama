"""Models, in the order PRD section 9 insists on.

`baseline` is the thing to beat; `forecast` (LightGBM + SHAP) is the first model allowed to
replace it; `anomaly` and `priority` are the unsupervised and scoring halves; `scaffolds`
holds the deep-learning and reinforcement-learning sketches, which stay disabled until
they beat LightGBM on the eval set.

Nothing is imported eagerly here: the optional `ml` extra must not be required just to
import `tazama.ai.ml.models`.
"""

from __future__ import annotations
