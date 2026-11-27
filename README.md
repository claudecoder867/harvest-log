# Harvest Log

Harvest Log tracks fruit receipts, tank pump-overs, and lab sampling. A cellar hand can open an assigned order and see exactly which instructions to follow. A manager can change the procedure for the next order while preserving the instructions and measurements on every order already issued.

The application uses Django 5.2, Django Ninja, PostgreSQL 16 with psycopg 3, and server-rendered HTML. Open the [hosted app](https://harvest-log-5hld.onrender.com/accounts/login/) as `demo_hand` or `demo_manager` with the password for that role. Supply both reviewer passwords privately. The [commit history](https://github.com/claudecoder867/harvest-log/commits/main/) follows the work from authentication through reporting. For development, start with the [local setup](#run-locally-on-macos), then try the [two-order walkthrough](#see-two-procedure-versions-side-by-side).

## Run locally on macOS

### 1. Clone the repository and start PostgreSQL

Use a Mac with Git and Homebrew installed. If you received a Git bundle, use its local path as the clone source.

```bash
git clone https://github.com/claudecoder867/harvest-log.git Harvest_Log
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

An order moves from **Not started** to **In progress** to **Completed**. Receiving requires a positive net weight in kg, a pump-over requires positive elapsed minutes, and a sample needs at least one lab reading. Open an order by its code in the queue. The order page displays its saved revision and instructions, the action available in the current state, the recorded measurements, and the history with each actor and time. The page submits to the same API that enforces permissions and transitions. Routes and payloads are in the [checked-in OpenAPI schema](openapi.json) and the [local interactive API docs](http://127.0.0.1:8000/api/v1/docs).

### See two procedure versions side by side

1. Sign in as `demo_manager` and expand the in-progress sample order **HL-009** for tank **T-04** in the [work queue](http://127.0.0.1:8000/manager/). Its badge shows **Revision 1**. Read its two steps.
2. Open the [sampling procedure editor](http://127.0.0.1:8000/manager/procedures/?kind=sample). Add “Write the tank ID on the sample label.” Publish and confirm revision 2.
3. Return to the queue and issue a **Take a lab sample** order for lot **DEMO-PN-2026**, tank **T-04**, assigned to **demo_hand**. Expand both orders. The new one includes the label instruction under revision 2. HL-009 still shows its original two steps under revision 1.

The [2026 Season Report](http://127.0.0.1:8000/manager/reports/seasons/2026/) gives a second check. On a fresh seed it shows **12 orders, 8 completed, 4 readings, and 6.40 t received**. Issuing the new sample raises only the order count. To finish the walkthrough, sign in as `demo_hand`, open **HL-009** by its code, record **21.8 °Brix** under **Soluble solids**, then select **Complete order**. Sign back in as the manager and revisit the 2026 report. The resulting totals are **13 orders, 9 completed, 5 readings, and 6.40 t**, with HL-009 still on revision 1. See the [walkthrough test](cellar/test_reports.py).

## Architecture decisions

**Publication and issuance share one lock.** Inside `transaction.atomic()`, `publish_procedure_version()` takes a PostgreSQL row lock on the parent `Procedure` with `select_for_update()`. That row exists even for the first publication. Once the lock is held, the service compares the editor’s `base_revision` with the latest revision. If two managers submit edits based on revision 1, the first can publish revision 2. The second waits, sees revision 2, and receives HTTP 409 so they can review the intervening change. Issuing an order locks the same procedure while selecting its latest published version. See the [publication and issuance services](cellar/services.py).

**An order carries its own historical instructions.** Issuance stores the selected `Version` primary key in `WorkOrder.version_id`. Both the queue and the order-detail API read ordered steps through that key. Publication inserts a new version and step rows. A unique constraint on `(procedure, revision)` prevents duplicate revision numbers. The protected foreign key prevents deletion of a referenced version through Django, and PostgreSQL triggers prevent repinning an issued order, editing historical steps, or appending steps after publication. See the [models](cellar/models.py) and [database guards](cellar/migrations/0002_procedure_versions.py).

**A change and its audit event commit together.** Publication writes the version, its ordered steps, and its `AuditEvent` in one transaction. Issuance, transitions, and lab recording also write their event alongside the business change. If the event insert fails, the change rolls back. The authenticated user supplies the actor, and PostgreSQL protects recorded events from updates and deletes. [Transaction and concurrency tests](cellar/test_publication.py)

**Each season metric counts its own records.** The outer query counts work orders by procedure kind and status for the lot’s season. Two correlated subqueries use `Subquery` and `OuterRef` to sum completed receipt weights and count individual lab readings separately. This prevents SQL fan-out. For example, the two readings on HL-010 count as two results while HL-010 counts as one order. The three completed 2026 receipts weigh 1,950, 2,100, and 2,350 kg. Their sum is 6,400 kg, or 6.40 metric tonnes. The 900 kg receipt belongs to 2025. The HTML report and API call the same [report query](cellar/reports.py).

## Brand application

All screens use the published Alert Enterprise wordmark, locally served Aeonik Regular and Bold, Purple primary actions, and Ink body text. The app uses v1.71.1 tokens, three original SVG navigation icons, and the table rules from the publisher’s developer pack. It retains the verified v1.71.0 font, button, field and badge subset. Asset paths preserve the version of each source. [Brand guide](https://brand.alertenterprise.app/guidelines/) · [Application stylesheet](cellar/static/cellar/site.css)

| Class | Use in the app |
| --- | --- |
| `.ae-btn` | Issue, save a measurement, finish an order or confirm publication. Secondary buttons use the outline palette. |
| `.ae-field` | Visible labels, units, inputs and inline validation. |
| `.ae-table` | Captioned queue and season breakdown. On a phone, queue rows stack and the report table scrolls within its own region. |
| `.ae-badge` | Text identifies role, order state and pinned revision. |

A shared navigation bar gives managers access to the queue, procedure editor and season report. A hand sees their queue. Both use the same order detail screen and audit history. Keyboard users can skip navigation, focus errors and cancel publication.

## How ChatGPT was used

The **Harvest Test** ChatGPT Project used Matt Bennett’s exercise brief and the [execution plan](Harvest_Log_Execution_Plan.pdf) as inputs. Project instructions fixed the stack, role boundaries, transaction rules, and permitted CSS classes across conversations. Each prompt scoped one phase and specified its acceptance checks. The workflow called for reviewing the code and test results before advancing and preserving each phase in Git.

| Phase | What the prompts asked ChatGPT to scaffold |
| --- | --- |
| 1 · Infrastructure and sign-in | Pin dependencies, bootstrap PostgreSQL, extend Django’s user model, and write the signed-out redirect and manager-access tests. |
| 2 · Domain and measurements | Model immutable procedure snapshots first, then add receipts, pump-over durations, lab readings, atomic transitions, and rollback tests. |
| 3 · API and screens | Implement the Django Ninja contract and role checks, then wire the work queue and procedure editor to those endpoints with vanilla JavaScript. |
| 4 · Analytics and fixture | Generate deterministic demo orders and reconcile API and HTML totals against independently specified weights and counts. |

AI accelerated the repetitive work of drafting migrations, request schemas, forms, and test setup. Two prompt constraints shaped the implementation. For the Season Report, the prompt prohibited aggregating across work orders, receipts, and readings in one joined row set. The regression test checks multiple readings on a sample, an unfinished receipt, and a prior-season receipt. For browser writes, the project required Django session authentication and CSRF enforcement. The shared `postJSON()` wrapper reads the current `csrftoken` cookie, sends it as `X-CSRFToken`, and includes same-origin credentials. Standard POST forms use `{% csrf_token %}`. The API tests enable CSRF checks and verify that an authenticated manager’s POST without the header receives HTTP 403. [Report tests](cellar/test_reports.py) · [API tests](cellar/test_api.py)

The first brand search failed to locate the kit. As the execution plan records, inspecting the published guide in a browser established the v1.70.0 design reference. A later review found that the signed-in templates omitted the application stylesheet and the bundled subset lacked table rules. The release added the published table rules and icons, then added browser checks for loaded fonts, both roles and phone widths.

## Run the tests

Run the Django system check and test suite in the configured shell with PostgreSQL running.

```bash
python manage.py check
python manage.py test
```

The bootstrap grants `harvest_app` permission to create the temporary test database. The tests exercise PostgreSQL row locks and triggers, historical step text, role and CSRF denials, audit rollback, and seed idempotency. To run just the two-order and Season Report checks, use `python manage.py test cellar.test_reports`.

The GitHub Actions workflow also runs a Chromium walkthrough on an isolated Django test database. It verifies the served OpenAPI document against the checked-in schema, exercises the revision-1/revision-2 demonstration, and checks the 2025 and 2026 totals. Run it locally after installing `playwright==1.56.0` and Chromium with `python -m playwright install chromium`, then run `python manage.py test qa.browser`. [Continuous integration](.github/workflows/test.yml)

## Deploy on Render

Use a Python web service and a persistent PostgreSQL 16 database. Set `PYTHON_VERSION=3.13.18`, supply the database’s internal URL as `DATABASE_URL`, and store a persistent `DJANGO_SECRET_KEY` in the service environment. Set `DJANGO_DEBUG=0`. For a custom domain, set `DJANGO_ALLOWED_HOSTS` to its bare hostname (for this demo, `harvest-log-5hld.onrender.com`) and `DJANGO_CSRF_TRUSTED_ORIGINS` to its full HTTPS origin. The settings also recognize `RENDER_EXTERNAL_HOSTNAME`.

| Service setting | Command |
| --- | --- |
| Build | `bash build.sh build` |
| Pre-deploy | `bash build.sh release` |
| Start | `python -m gunicorn config.wsgi:application --bind "0.0.0.0:$PORT"` |

If the service has no separate pre-deploy step, use `bash build.sh` as the build command to run both stages. To initialize a demo deployment, supply both demo password variables and set `SEED_DEMO=1` for its first release. Restore `SEED_DEMO=0` afterward. [Build and release script](build.sh) · [Django settings](config/settings.py)

## Next step

**Provision an individual login for each operator.** The audit event already records the authenticated user. If two people share `demo_hand`, either person’s reading has the same actor. Named accounts can keep the existing cellar-hand permissions while making each sampling result and completion attributable to the person who performed it. Deactivating an operator’s account at the end of the season preserves that history.
