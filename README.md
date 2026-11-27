# Harvest Log

Harvest Log tracks fruit receipts, tank pump-overs, and lab sampling. A cellar hand can open an assigned order and see exactly which instructions to follow. A manager can change the procedure for the next order while preserving the instructions and measurements on every order already issued.

The application uses Django 5.2, Django Ninja, PostgreSQL 16 with psycopg 3, and server-rendered HTML. Start with the [local setup](#run-locally-on-macos), then try the [two-order walkthrough](#see-two-procedure-versions-side-by-side).

## Run locally on macOS

### 1. Clone the repository and start PostgreSQL

Use a Mac with Git and Homebrew installed. Replace `<repository-url>` with the clone URL supplied for review. If you received a Git bundle, use its local path in the same command.

```bash
git clone '<repository-url>' Harvest_Log
cd Harvest_Log
brew install postgresql@16 python@3.13
bash init_mac_postgres.sh
```

The script creates a private PostgreSQL cluster under `~/.harvest-log/postgres16`, starts it, and checks the connection. Its final query should show database `harvest_log`, user `harvest_app`, and server version `16.x`. The current macOS account authenticates over a private Unix socket. Rerun the script to start the same cluster after a reboot.

### 2. Create the Python environment

```bash
"$(brew --prefix python@3.13)/bin/python3.13" -m venv .venv
source .venv/bin/activate
python --version
python -m pip install -r requirements.txt
python -m pip check
```

The local commands use CPython 3.13. For exact release parity, create the virtual environment with CPython **3.13.18**, the patch version pinned in `.python-version` and enforced by `build.sh`. The requirements file locks dependency versions and wheel hashes.

### 3. Configure Django

Run this block in the same terminal. It creates a signing key on first use and reuses it across restarts. The database URL selects the application role and database, while `PGHOST` selects the socket created by the bootstrap script.

```bash
export PGHOST="$HOME/.harvest-log/run"
export PGPORT=5432
export DATABASE_URL='postgresql://harvest_app@/harvest_log'
export DJANGO_DEBUG=1
export DJANGO_ALLOWED_HOSTS='127.0.0.1,localhost'

if [ ! -s "$HOME/.harvest-log/django-secret" ]; then
  (umask 077 && openssl rand -hex 48 > "$HOME/.harvest-log/django-secret")
fi
export DJANGO_SECRET_KEY="$(cat "$HOME/.harvest-log/django-secret")"
```

In a new terminal, activate `.venv` and repeat this configuration block. If you set `HARVEST_LOCAL_ROOT` for the bootstrap script, use that directory in place of `$HOME` in the paths above.

### 4. Migrate, seed, and sign in

Choose a password for each demo account **before the first seed**. The prompts hide what you type.

```bash
python manage.py migrate
export DEMO_HAND_PASSWORD="$(python -c 'from getpass import getpass; print(getpass("Choose a password for demo_hand: "))')"
export DEMO_MANAGER_PASSWORD="$(python -c 'from getpass import getpass; print(getpass("Choose a password for demo_manager: "))')"
python manage.py seed_demo
unset DEMO_HAND_PASSWORD DEMO_MANAGER_PASSWORD
python manage.py runserver 127.0.0.1:8000
```

Open the [local sign-in page](http://127.0.0.1:8000/accounts/login/) as `demo_manager` or `demo_hand`. A clean seed prints `Created 13 demo work orders.` Twelve belong to 2026 and one belongs to 2025. Running the seed again creates zero additional orders and preserves existing work, revisions, audit history, and passwords. If an account was created before its password was set, use `python manage.py changepassword demo_hand` or `python manage.py changepassword demo_manager`.

## Review the demo

| Capability | Cellar hand | Cellar manager |
| --- | --- | --- |
| Read orders, pinned steps, measurements, and order timeline | Assigned orders | All orders |
| Start work, record a sample result, or complete work | Assigned orders in the required state | Any order in the required state |
| Issue an order and choose its assignee | — | All three work types |
| Read procedure history and publish a revision | — | All three procedures |
| View the Season Report and winery-wide audit events | — | All seasons and events |

An order moves from **Not started** to **In progress** to **Completed**. Receiving requires a positive net weight in kg, a pump-over requires positive elapsed minutes, and a sample needs at least one lab reading. The queue displays orders and steps. The API also exposes transitions, lab entry, and the per-order timeline. Routes and payloads are in the [checked-in OpenAPI schema](openapi.json) and the [local interactive API docs](http://127.0.0.1:8000/api/v1/docs).

### See two procedure versions side by side

1. Sign in as `demo_manager` and expand the in-progress sample order **HL-009** for tank **T-04** in the [work queue](http://127.0.0.1:8000/manager/). Its badge shows **Revision 1**. Read its two steps.
2. Open the [sampling procedure editor](http://127.0.0.1:8000/manager/procedures/?kind=sample). Add “Write the tank ID on the sample label.” Publish and confirm revision 2.
3. Return to the queue and issue a **Take a lab sample** order for lot **DEMO-PN-2026**, tank **T-04**, assigned to **demo_hand**. Expand both orders. The new one includes the label instruction under revision 2. HL-009 still shows its original two steps under revision 1.

The [2026 Season Report](http://127.0.0.1:8000/manager/reports/seasons/2026/) gives a second check. On a fresh seed it shows **12 orders, 8 completed, 4 readings, and 6.40 t received**. Issuing the new sample raises only the order count. The automated walkthrough then records a reading on HL-009 and completes it as the hand. The resulting totals are **13 orders, 9 completed, 5 readings, and 6.40 t**, with HL-009 still on revision 1. See the [walkthrough test](cellar/test_reports.py).

## Architecture decisions

**Publication and issuance share one lock.** Inside `transaction.atomic()`, `publish_procedure_version()` takes a PostgreSQL row lock on the parent `Procedure` with `select_for_update()`. That row exists even for the first publication. Once the lock is held, the service compares the editor’s `base_revision` with the latest revision. If two managers submit edits based on revision 1, the first can publish revision 2. The second waits, sees revision 2, and receives HTTP 409 so they can review the intervening change. Issuing an order locks the same procedure while selecting its latest published version. See the [publication and issuance services](cellar/services.py).

**An order carries its own historical instructions.** Issuance stores the selected `Version` primary key in `WorkOrder.version_id`. Both the queue and the order-detail API read ordered steps through that key. Publication inserts a new version and step rows. A unique constraint on `(procedure, revision)` prevents duplicate revision numbers. The protected foreign key prevents deletion of a referenced version through Django, and PostgreSQL triggers prevent repinning an issued order, editing historical steps, or appending steps after publication. See the [models](cellar/models.py) and [database guards](cellar/migrations/0002_procedure_versions.py).

**A change and its audit event commit together.** Publication writes the version, its ordered steps, and its `AuditEvent` in one transaction. Issuance, transitions, and lab recording also write their event alongside the business change. If the event insert fails, the change rolls back. The authenticated user supplies the actor, and PostgreSQL protects recorded events from updates and deletes. [Transaction and concurrency tests](cellar/test_publication.py)

**Each season metric counts its own records.** The outer query counts work orders by procedure kind and status for the lot’s season. Two correlated subqueries use `Subquery` and `OuterRef` to sum completed receipt weights and count individual lab readings separately. This prevents SQL fan-out. For example, the two readings on HL-010 count as two results while HL-010 counts as one order. The three completed 2026 receipts weigh 1,950, 2,100, and 2,350 kg. Their sum is 6,400 kg, or 6.40 metric tonnes. The 900 kg receipt belongs to 2025. The HTML report and API call the same [report query](cellar/reports.py).

## Brand application

The execution plan specifies **Alert Enterprise v1.70.0**. The repository ships tokens and a component stylesheet labeled **v1.71.0**, which the base template loads from Django static files alongside the logo and Aeonik fonts. The light palette maps Purple `#5F51D5` to primary actions and Ink `#262626` to text, with Coral `#FF5233` available as an accent. The sign-in stylesheet uses semantic tokens for focus and validation errors. No external CSS framework or inline styles are required.

| Brand class | HTML in Harvest Log |
| --- | --- |
| `.ae-btn` | A native `<button>` signs in or publishes a revision. The queue also uses it on the `<summary>` that opens the issue form. |
| `.ae-field` | A wrapper groups a visible `<label>` with its `<input>`, `<select>`, or `<textarea>`. |
| `.ae-table` | Queue and report tables use `<caption>` and scoped `<th>` cells. The bundled component subset has no `.ae-table` rule, so these tables use native table styling. |
| `.ae-badge` | A `<span>` displays role, status, or revision as text, so the value remains readable without color. |

The sign-in page adds a small token-based [page stylesheet](cellar/static/cellar/site.css). The queue, editor, and report load the [tokens](cellar/static/cellar/brand/1.71.0/tokens.css) and [component subset](cellar/static/cellar/brand/1.71.0/css/components.css) directly through the [base template](cellar/templates/cellar/base.html).

## How ChatGPT was used

The **Harvest Test** ChatGPT Project used Matt Bennett’s exercise brief and the [execution plan](Harvest_Log_Execution_Plan.pdf) as inputs. Project instructions fixed the stack, role boundaries, transaction rules, and permitted CSS classes across conversations. Each prompt scoped one phase and specified its acceptance checks. The workflow called for reviewing the code and test results before advancing and preserving each phase in Git.

| Phase | What the prompts asked ChatGPT to scaffold |
| --- | --- |
| 1 · Infrastructure and sign-in | Pin dependencies, bootstrap PostgreSQL, extend Django’s user model, and write the signed-out redirect and manager-access tests. |
| 2 · Domain and measurements | Model immutable procedure snapshots first, then add receipts, pump-over durations, lab readings, atomic transitions, and rollback tests. |
| 3 · API and screens | Implement the Django Ninja contract and role checks, then wire the work queue and procedure editor to those endpoints with vanilla JavaScript. |
| 4 · Analytics and fixture | Generate deterministic demo orders and reconcile API and HTML totals against independently specified weights and counts. |

AI accelerated the repetitive work of drafting migrations, request schemas, forms, and test setup. Two prompt constraints shaped the implementation. For the Season Report, the prompt prohibited aggregating across work orders, receipts, and readings in one joined row set. The regression test checks multiple readings on a sample, an unfinished receipt, and a prior-season receipt. For browser writes, the project required Django session authentication and CSRF enforcement. The shared `postJSON()` wrapper reads the current `csrftoken` cookie, sends it as `X-CSRFToken`, and includes same-origin credentials. Standard POST forms use `{% csrf_token %}`. The API tests enable CSRF checks and verify that an authenticated manager’s POST without the header receives HTTP 403. [Report tests](cellar/test_reports.py) · [API tests](cellar/test_api.py)

The first brand search failed to locate the kit. As the execution plan records, inspecting the published guide in a browser established the v1.70.0 design reference. The stylesheet headers identify the version actually shipped here.

## Run the tests

Run the Django system check and test suite in the configured shell with PostgreSQL running.

```bash
python manage.py check
python manage.py test
```

The bootstrap grants `harvest_app` permission to create the temporary test database. The tests exercise PostgreSQL row locks and triggers, historical step text, role and CSRF denials, audit rollback, and seed idempotency. To run just the two-order and Season Report checks, use `python manage.py test cellar.test_reports`.

## Deploy on Render

Use a Python web service and a persistent PostgreSQL 16 database. Set `PYTHON_VERSION=3.13.18`, supply the database’s internal URL as `DATABASE_URL`, and store a persistent `DJANGO_SECRET_KEY` in the service environment. Set `DJANGO_DEBUG=0`. For a custom domain, set `DJANGO_ALLOWED_HOSTS` to its hostname and `DJANGO_CSRF_TRUSTED_ORIGINS` to its full HTTPS origin. The settings also recognize `RENDER_EXTERNAL_HOSTNAME`.

| Service setting | Command |
| --- | --- |
| Build | `bash build.sh build` |
| Pre-deploy | `bash build.sh release` |
| Start | `python -m gunicorn config.wsgi:application --bind "0.0.0.0:$PORT"` |

If the service has no separate pre-deploy step, use `bash build.sh` as the build command to run both stages. To initialize a demo deployment, supply both demo password variables and set `SEED_DEMO=1` for its first release. Restore `SEED_DEMO=0` afterward. [Build and release script](build.sh) · [Django settings](config/settings.py)

## Next step

**Provision an individual login for each operator.** The audit event already records the authenticated user. If two people share `demo_hand`, either person’s reading has the same actor. Named accounts can keep the existing cellar-hand permissions while making each sampling result and completion attributable to the person who performed it. Deactivating an operator’s account at the end of the season preserves that history.
