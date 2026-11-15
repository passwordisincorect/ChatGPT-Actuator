# SCH3 validation summary

**Evidence date:** 2026-11-15
**Qualification source fingerprint:** `637f9aa98b46cf1a950ebc2f2817f944ceebe820e700c3dd8922a2f3241f7312`
**Environment:** Windows, Altium Designer 26.10.1, one standalone schematic sheet

This is a sanitized summary of a prior local qualification run. The fingerprint identifies the candidate source tested in that run; it is not a claim that every later branch integration has the same fingerprint or has been requalified. Private fixture files, native harnesses, screenshots, and raw logs remain local and are not part of this repository.

## Recorded results

- 163 offline checks passed against the original qualified snapshot.
- 8 native workflow cases passed on the identified source snapshot.
- 6 adversarial ERC cases passed; compile success was evaluated separately from ERC status.
- The project was reopened and compiled in AD26, and the resulting schematic was visually observed in the UI.
- Qualification covered one sheet and the exercised configuration only.

The native results were observed with the identified AD26 version and a private single-sheet fixture. Since the native harness and fixture are not shipped, these results are not independently reproducible from this repository. The offline verifier `scripts/sch3_offline_verify.py` is included, but its output cannot recreate or substitute for the private native run.

## Scope and limits

This evidence does not establish correctness for multi-sheet hierarchy, PCB layout or routing, all 33 lint rules, exhaustive component rotations, every Altium project setting, or arbitrary schematics. ERC and structural checks report evidence within their supported scope; they do not prove overall circuit correctness. For edits, inspect the design and compile/ERC results in the user's own project before accepting a change.

The qualification covers the SCH3 schematic adapter and EDA source files at the stated fingerprint. Transport, MCP server startup, packaging, or other repository integration outside that native EDA source fingerprint does not itself add a new native certification claim. Any source change affecting the qualified EDA behavior requires fresh qualification before extending these claims.

## Public-checkout verification

The offline verifier and Altium-focused unit tests can be run from a Windows Python 3.12 environment:

```powershell
python scripts/sch3_offline_verify.py
python -m unittest discover -s tests -p "test_*altium*.py" -v
```

These commands exercise offline behavior only. Private native harnesses and raw qualification receipts are intentionally omitted. The public integration passed **282 tests**, with zero failures, errors or skips, using Python 3.12 and `ResourceWarning` treated as an error. Isolated real MCP/Admin and OAuth gateway checks registered **166 tools**, including all six new SCH3 tools; authentication, write approval and token revocation were exercised on loopback fixtures. These tests do not drive Altium or the physical desktop. The verifier runs the full offline repository suite; the second command selects only Altium-focused tests.

Real PowerShell tests use a 60-second timeout budget; the deliberate timeout test remains at 0.4 seconds. Product timeout and resource limits are unchanged. This test budget does not guarantee that PowerShell starts within the product's default 20-second timeout on every machine. `powershell_info` retains its existing fields and also returns `timed_out`, `cancelled`, `duration_seconds`, and `output_truncated`; exit code 0 alone does not prove completion. Tests parse the returned JSON, validate `PSVersion`, and cover a timeout with exit code 0.

The seven adapter files included in the fingerprint remain byte-for-byte identical to the qualified snapshot. `.gitattributes` preserves those raw bytes across Git checkouts. Public server/transport and packaging integration is validated separately.
