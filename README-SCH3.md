# Altium schematic tools (SCH3 candidate)

**Experimental `1.3.0rc1` prerelease candidate; no automatic runtime activation.** This branch adds an opt-in workflow for standalone, single-sheet `.PrjPcb` projects in Altium Designer 26. It does not qualify multi-sheet designs, PCB layout, or every electrical rule. Review this guide and the [validation summary](docs/ALTIUM-SCH3-VALIDATION.md) before use.

## Windows setup and configuration

The candidate was developed and exercised on Windows with Python 3.12 and Altium Designer 26.10.1. Install the project into a Python 3.12 environment using the repository's normal Windows setup instructions (or `python -m pip install -e .`). Keep the tracked `config/config.json` as the public read-only template; copy it to a private local config and point the local runtime at that copy using the documented `CHATGPT_ACTUATOR_CONFIG` setting. Do not commit machine-specific configuration or credentials.

The public template enables read-only EDA access within configured project roots; editing is disabled. In the private config, review `eda.enabled` and `eda.allow_read`, enable `eda.allow_edit` only when needed, and set `eda.allowed_project_roots` to the project directories the adapter may access, `eda.altium_library_roots` to permitted libraries, and `eda.altium_executable` to the local AD26 executable. Keep `allow_manufacturing` disabled for this schematic workflow. Never put human login credentials, license data, or private machine paths in the repository.

The documented source package version for this candidate is `1.3.0rc1`. The offline verifier `scripts/sch3_offline_verify.py` is included in the source tree. Private native harnesses, fixtures, and raw evidence logs are deliberately not included; native qualification is therefore not reproducible from this public repository alone.

This candidate retains owner checks, source hashes, isolated staging, request keys, and operation recovery. All native behavior described below is scoped to the evidence in the validation summary; historical evidence is not proof for arbitrary projects or future source changes.

`eda_altium_sch_inspect(project_path)` compiles the saved source and returns an owner-scoped `snapshot_id`. Do not use it as evidence for unsaved editor changes. A failed compile can return a physical diagnostic snapshot only when exact project/document membership is available; its connectivity stays incomplete. Fresh ERC validation is skipped after a failed compile, so stale violations cannot certify the design. `eda_altium_sch_check_pin_contracts(snapshot_id, contracts, require_all_pins=true)` accepts `{component,pin,net}` assignments or `{component,pin,nc:true,reason}` contracts. Full pin coverage is required by default. `eda_altium_sch_diff_connectivity(before_snapshot_id, after_snapshot_id)` compares two owned snapshots. Its result reports connectivity differences; it does not judge an ECO's intent.

`eda_altium_sch_lint(snapshot_id, intent?)` evaluates supported structural and declared electrical checks. Pull-up and decoupling checks need an explicit intent; missing data returns `incomplete`. Single-pin nets are warnings. A schematic spacing check is not PCB placement evidence.

`eda_altium_sch_erc(project_path)` compiles the project and reads native Altium violations. Compile success alone is not ERC success; unavailable or incomplete violation/configuration evidence must remain `incomplete`.

`eda_altium_sch_edit_preview` and `eda_altium_sch_edit_apply` work on a copy. Preview digests bind the requested edits and expected connectivity table. Every topology change must provide the complete expected pin table. Apply reopens the staged document, compiles it, and verifies the requested object and connectivity changes before publication.

`eda_altium_sch_edit_commit(output_dir, request_key)` applies a verified copy to its clean source and records a hash-checked backup. It refuses unsaved source documents or changed source hashes. `eda_altium_sch_edit_restore(commit_id, request_key)` restores that backup only while the committed output still matches its recorded hash. Preserve operation IDs and inspect operation status after a timeout; never assume a timed-out native edit was not delivered.

Mutation values and coordinates in public edit requests use mils. Snapshot geometry uses the native Altium coordinate units recorded by the snapshot schema. Native behavior is qualified only within the AD26.10.1 single-sheet scope recorded in the validation summary. The private fixtures and harness used for that qualification are not shipped.

## Edit requests

Existing `set_component_value` and `rename_net` requests retain their fields. The latter rejects power ports and unsupported naming objects; edit their individual primitives with an explicit complete connectivity table instead.

| Operation | Required fields in addition to `op` |
| --- | --- |
| `add_component` | `library_path,symbol,reference,value,x,y`; optional `rotation,part,uid` |
| `remove_object` | `uid,expected` |
| `move_component` | `uid,expected,x,y,rotation` |
| `set_component_parameter` | `uid,expected,name,expected_value,value` |
| `add_wire` | `vertices:[[x,y],...]`; optional `uid` |
| `add_label` | `text,x,y`; optional `rotation,uid` |
| `add_junction` | `x,y`; optional `uid` |
| `add_power` | `text,x,y`; optional `rotation,style,uid` |
| `add_noerc` | `component,pin,reason,x,y`; optional `uid` |
| `update_object` | `uid,expected,replacement` using the same primitive's add operation |

`expected` is the exact object row from inspection. Existing UID selectors must be unique; a preview cannot change the same object twice. Component moves do not stretch wires. Parameter names `Comment` and `Value` use the existing value operation. New parameters require `expected_value:""` and are hidden. Inspect the selected library before previewing component additions; preview uses the verified cache and does not start native library reads.

Structural edits require `expected_connectivity` covering every resulting physical pin with `{component,pin,net}` or `{component,pin,nc:true,reason}`. The literal net name `NC` remains an electrical net. A No-ERC directive must name a matching NC contract/reason and coincide with its physical pin; all pins under that directive must be explicitly NC. Unsupported buses, ports and other incomplete geometry prevent qualification.

The target pin set comes from captured source pins, requested component removals, and verified library pins for additions. Missing or extra target pins are rejected before native dispatch. Repairs starting from an incomplete compile also require this full table for value/parameter edits.

Preview returns concrete normalized changes and the target connectivity. For general edits `after` is `null` and `after_status` is `requires_native_readback`; it never represents an unexecuted edit as observed native output. For the two legacy edits, `after` is only a prediction.

## Declared electrical intent

Declare both `decoupling` and `pullups`. Empty arrays mean explicitly no requirements in that family; omitting a family yields `incomplete`. Optional `contracts` checks specific mandatory pins. Resistance/capacitance ranges use base SI values.

```json
{
  "decoupling": [{
    "power": {"component": "U1", "pin": "8"},
    "power_net": "VCC", "ground_net": "GND",
    "capacitor": {"component": "C1", "pins": ["1", "2"]},
    "min_value": 8e-8, "max_value": 1.2e-7
  }],
  "pullups": [{
    "signal_net": "SDA", "power_net": "VCC",
    "resistor": {"component": "R1", "pins": ["1", "2"]},
    "min_value": 4465, "max_value": 4935
  }]
}
```

ERC settings are bound to the unchanged `.PrjPcb` hash. Missing explicit Error Reporting/Connection Matrix settings, mismatched native settings, unknown severities or incomplete violation capture prevent a PASS. No settings or suppressions are silently changed.

Configuration qualification is scoped to AD26.10.1: the saved Error Reporting table must contain exactly 163 keys (`Type1` through `Type165`, excluding `Type120` and `Type121`) and the complete 17-by-17 connection matrix. Sparse or unfamiliar tables return `incomplete`. The adapter closes only the exact clean source project, reloads its saved files, checks membership, compilation, source hashes and dirty project options, and binds both native operation receipts to the adapter fingerprint. This is reported as `saved_project_natively_reloaded`; it is not an independently captured native settings array. Ordinal-derived `project_type` and `project_level` diagnostics remain unverified and cannot certify configuration. Native adversarial evidence checks real violation severity, matrix effects and suppression separately.

## Verification and activation

Run the repository's Altium-focused offline tests from the source checkout with `python -m unittest discover -s tests -p "test_*altium*.py" -v`. These automated tests do not replace an AD26 native check. `python scripts/sch3_offline_verify.py` is included and runs the offline verifier from a real Python module entry point on Windows; its output is local evidence, not a native qualification receipt.

Native validation was previously performed against a private fixture and desktop session. The native harnesses, fixture designs, and raw logs are not shipped, so this repository does not provide a reproducible native acceptance command. The public validation summary records the source fingerprint and bounded results; do not infer qualification for another fingerprint, project, or AD26 configuration. This candidate does not activate itself. Commit and restore are explicit operations.

Commit/restore are explicit calls. On a crash before an atomic replacement, a retry can prove `not_applied` from the saved hashes; it does not replay the write. A new request key is needed for a new attempt. Changed external bytes prevent automatic overwrite or restoration. Keep the returned backup record until the changes are accepted.

A durable `preparing` record precedes backup creation. Unknown verification is polled before recovery; retries do not replace the source twice. Commit performs a fresh object/connectivity/ERC check. Restoring an originally uncompilable baseline verifies original bytes and objects and reports its limited connectivity scope explicitly.

Owned staging runs a complete native read/compile/validation pass, saves first-read sheet updates and the full native project serialization, then reopens and verifies against the saved hashes. Normalization is request-keyed and precedes the final verification baseline; source inspection keeps its unsaved-document guard. Minimal generated project files failed an AD26 clean-session open control, while complete native serialization reopened and compiled. Component moves preserve the rigid transform of comment text while retaining its original autoposition setting.
