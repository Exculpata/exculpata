# IANA top-level domain snapshot

`iana-tlds-alpha-by-domain.txt` is a byte-for-byte copy of
<https://data.iana.org/TLD/tlds-alpha-by-domain.txt>, fetched once without credentials.
Version line: `# Version 2026102000, Last Updated Tue Oct 20 07:07:01 2026 UTC`.
SHA-256: `92e7f493aa6d9e24f33e3ef452ef2aee1562e9c890851b88ec797f022ce641c2`.

To refresh, replace the file with a new copy, update the version and hash in this
note and the hash and count in `test_contract_iana_tlds_are_pinned` in
`tests/test_reference_network_boundary.py`, then rerun `scripts/network-contract.py`
and its tests.
