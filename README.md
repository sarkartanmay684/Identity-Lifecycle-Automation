# identity-lifecycle-automation

Build B: a Microsoft Entra identity lifecycle automation lab.

**Current stage: setup scaffold.** Python modules are placeholders. No joiner, mover, leaver, authentication test, or dry-run implementation is included yet. No tests or CI run have been claimed.

Start with [docs/setup.md](docs/setup.md). Use your local Windows computer and your existing personal lab tenant. Required Azure virtual machines: **0**.

## Intended behavior

Read fictional HR records, provision cloud-only employee users, reconcile department access, and offboard terminated users. Demonstrate safe dry-run, repeatable operations, structured audit logs, mocked tests, and a comparison with native Lifecycle Workflows.

## Supplied setup files

| File | Purpose |
|---|---|
| `.gitignore` | Protect local secrets, environments, and runtime files from Git |
| `.env.example` | Names of the four local settings |
| `requirements.txt` | Dependencies for the later Python work |
| `config/group_map.json` | Shared and department-specific lab group names |
| `data/hr_feed.csv` | Fictional joiner sample; replace its domain placeholder |
| `docs/setup.md` | Click-by-click setup walkthrough |
| `docs/lab-inventory.example.json` | Blank inventory template; copy to `.local.json` |
| `src/*.py` | Placeholders for the coding phase |

Only `.env.example` belongs in Git. Keep your populated `.env` private. The group map records intended rules; enforcement is added to code later.

## Required settings

`ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET`, and `ENTRA_UPN_DOMAIN`.

A `.env` file will be read explicitly by the later configuration code using python-dotenv. It does not automatically modify Windows environment variables.

## Status checklist

- [ ] Tenant and verified domain confirmed
- [ ] Four assigned security groups created
- [ ] Two cloud-only manager users created
- [ ] Application credentials and permissions configured
- [ ] Manual Graph requests verified
- [ ] Application-only token verified
- [ ] Joiner implemented
- [ ] Mover implemented
- [ ] Leaver and session measurement completed
- [ ] Dry-run, failure handling, audit logs, and tests implemented
- [ ] CI and secret scanning configured
- [ ] Native comparison completed, subject to eligible licensing

Update this README as each stage is actually completed.

## Python foundation

The Python foundation is implemented and verified against the lab tenant.
It loads the application configuration, authenticates to Microsoft Graph,
and checks the configured tenant and verified domain.

Run the following commands from the project root on Windows.

Install dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Check dependency compatibility:

```powershell
.\.venv\Scripts\python.exe -m pip check
```

Run the foundation tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_foundation.py
```

Check the application connection:

```powershell
.\.venv\Scripts\python.exe -m src.check_connection
```

Verification completed on 2026-10-16:

- Dependency check: No broken requirements found.
- Foundation tests: 12 passed.
- Application connection: success.
- Expected tenant: true.
- Verified domain: hybrididentity.site.
- Domain verification: true.
- Connection check: read only.

The connection check reads organization information without changing
directory objects.

CSV validation, joiner, mover, leaver, and lifecycle dry-run functionality
remain under development. The 12 passing tests cover the foundation;
lifecycle automation tests are still pending.

Detailed setup and verification evidence is recorded in docs/setup.md.

## CSV validation and joiner preview

This checkpoint validates the HR feed and previews account creation, birthright groups, and manager assignments for new employees.

Run from the project root using the existing virtual environment:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest -q tests/test_foundation.py tests/test_joiner_preview.py
& '.\.venv\Scripts\python.exe' -m src.preview_joiners --validate-only
& '.\.venv\Scripts\python.exe' -m src.preview_joiners --dry-run
```

The local validator needs no tenant connection. The directory preview uses the four existing Entra environment settings and sends only Graph GET requests. It prints structured JSON with proposed actions and a summary showing zero directory writes.

Employees are matched by employeeId. New usernames use deterministic numeric suffixes when necessary. Duplicate identities, ambiguous groups, invalid managers, and unsupported account/group types stop the preview.

Existing employees and effective terminations are reported as pending work for later reconciliation. This checkpoint does not apply changes and has no --apply mode.

See [the preview guide](docs/joiner-preview.md) for behavior and limitations, and [setup evidence](docs/setup.md) for actual local results.

### Verified checkpoint

On 2026-10-16:
- All 63 foundation and preview tests passed.
- The HR feed validated successfully with two employees.
- The live joiner dry run planned two accounts, four group
  memberships, and two manager assignments.
- The preview performed zero directory writes.

Actual joiner execution, mover automation, and leaver
automation remain under development.

Live execution status: PASSED on 2026-10-16.

Both lab employees were provisioned and independently verified.
An unchanged rerun completed with zero directory writes and unchanged
user object IDs. Provisioning recovered successfully after the initial
verification delays.

The combined automated test suite passed 97 tests.
See docs/setup.md, section 15.2, for execution and recovery evidence.

Mover and leaver automation remain pending.