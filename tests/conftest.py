import pytest
from datetime import date

from oonrev import synth
from oonrev.ledger import load
from oonrev.close import run_close

AS_OF = date(2026, 9, 30)


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("data")
    synth.generate(out, seed=11)
    return out


@pytest.fixture(scope="session")
def ledger(data_dir):
    return load(data_dir)


@pytest.fixture(scope="session")
def close(ledger):
    return run_close(ledger, AS_OF)
