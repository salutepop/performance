"""Workloads — optional inputs fed into a monitoring Session.

The project's primary job is monitoring; workloads are just one kind of input
that produces I/O worth observing. This package holds:

  - fio_runner.py : builds & runs a single fio job
  - reporter.py   : TC result directory layout + JSON saving
  - tc_runner.py  : discovers and runs test cases (workloads/cases/)
  - cases/        : the test cases themselves (.json + .py)
  - scenarios/    : the newer self-checking Scenario framework

A test case is just a recipe for issuing I/O inside a Session.
"""
