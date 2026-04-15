# ENG-908619 — Root Cause Analysis & Simulation Tests

This repository contains the full RCA and simulation test suite for
[CR 497217](https://nugerrit.ntnxdpro.com/c/nutest-py3-tests/+/497217)
fixing sequential app domain PC deployment failures (ENG-908619).

## Files

| File | Description |
|------|-------------|
| [`ENG-908619_RCA_and_Simulation_Report.md`](ENG-908619_RCA_and_Simulation_Report.md) | Full RCA with log evidence timeline, SDK code references, and test results |
| [`test_eng908619_simulation.py`](test_eng908619_simulation.py) | 18 simulation tests across 5 scenarios |

## Run the tests

```bash
python3 test_eng908619_simulation.py -v
```

**Result: 18/18 PASS — 0 failures — 0.001s runtime**
