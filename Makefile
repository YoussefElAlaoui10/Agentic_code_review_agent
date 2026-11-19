PY ?= python3
export PYTHONPATH := src

.PHONY: help demo test proof speedup quality bench mcp all clean

help:
	@echo "make demo     - review the sample PR offline"
	@echo "make test     - run the unit test suite (49 tests, no dependencies)"
	@echo "make proof    - three independent checks that the fan-out overlaps"
	@echo "make speedup  - sequential vs parallel scaling table + SVG charts"
	@echo "make quality  - precision/recall/F1 against labelled defects"
	@echo "make bench    - speedup + quality, writing everything to results/"
	@echo "make all      - test + proof + bench"

demo:
	$(PY) -m prreview review samples/demo_pr.diff --mock -c 8

test:
	$(PY) -m unittest discover -s tests -t .

proof:
	$(PY) scripts/prove_concurrency.py

speedup:
	$(PY) -m prreview speedup samples/demo_pr.diff --mock --mock-latency 0.20 \
		--concurrencies 1,2,4,8,12 --repeats 3

quality:
	$(PY) -m prreview quality --mock --mock-latency 0.02 \
		--thresholds 0.0,0.45,0.6,0.75,0.9

bench: speedup quality

mcp:
	$(PY) -m prreview serve-mcp --mock

all: test proof bench

clean:
	rm -rf __pycache__ */__pycache__ */*/__pycache__ .pytest_cache
