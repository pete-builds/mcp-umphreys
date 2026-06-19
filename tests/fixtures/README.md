# Test fixtures

The mcp-umphreys test suite runs with no live network and no Postgres:

* The live ATU hot-window path uses the in-memory `StubATUClient`
  (`src/mcp_umphreys/clients/stubs.py`), which returns raw ATU `setlists` rows.
* The vault path uses `FakeVaultReader` in `tests/test_tools.py`, a
  record-shaped fake whose return dicts mirror the columns the real
  `VaultReader` SELECTs from the umphreys-vault schema.

Add captured ATU response samples here only if a future test needs to replay a
real upstream payload byte-for-byte.
