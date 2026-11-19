### Scaling on samples/demo_pr.diff (local diff)

| Mode | Concurrency | Wall clock (s) | Stdev | Speedup | Efficiency | Peak overlap | Findings |
| --- | --- | --- | --- | --- | --- | --- | --- |
| sequential | 1 | 5.22 | 0.00 | 1.00x | 100% | 1 | 18 |
| parallel | 1 | 5.22 | 0.00 | 1.00x | 100% | 1 | 18 |
| parallel | 2 | 2.81 | 0.00 | 1.86x | 93% | 2 | 18 |
| parallel | 4 | 1.45 | 0.00 | 3.59x | 90% | 4 | 18 |
| parallel | 8 | 0.88 | 0.00 | 5.94x | 74% | 8 | 18 |
| parallel | 12 | 0.58 | 0.00 | 9.02x | 75% | 12 | 18 |
