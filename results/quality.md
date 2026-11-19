### Review quality against labelled defects

| Configuration | Threshold | Published | TP | FP | FN | Precision | Recall | F1 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| rule-baseline | - | 22 | 14 | 8 | 2 | 0.64 | 0.88 | **0.74** |
| agents@conf>=0.00 | 0.00 | 18 | 11 | 7 | 5 | 0.61 | 0.69 | **0.65** |
| agents@conf>=0.45 | 0.45 | 18 | 11 | 7 | 5 | 0.61 | 0.69 | **0.65** |
| agents@conf>=0.60 | 0.60 | 13 | 10 | 3 | 6 | 0.77 | 0.62 | **0.69** |
| agents@conf>=0.75 | 0.75 | 11 | 9 | 2 | 7 | 0.82 | 0.56 | **0.67** |
| agents@conf>=0.90 | 0.90 | 5 | 5 | 0 | 11 | 1.00 | 0.31 | **0.48** |
