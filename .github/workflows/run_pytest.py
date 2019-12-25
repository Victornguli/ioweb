name: Check with pytest

on: [push, pull_request]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v1
    - uses: actions/setup-python@v1
      with:
        python-version: 3.5
    - run: |
        python -m pip install --upgrade pip
        pip install -r requirements_dev.txt
        pip install -e .
    - run: |
        pytest