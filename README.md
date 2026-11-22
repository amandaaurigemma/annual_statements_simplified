# Annual Donor Statements

Import a Raiser's Edge NXT CSV, review each donor statement, and generate
print-ready statements and envelopes or deliver PDF attachments by email.
Email delivery uses classic Outlook on Windows or a Power Automate cloud flow.
The app stores its history and batch files locally.

## Install and run

Use 64-bit Python 3.11 or later. Desktop Outlook delivery requires a Windows
host with classic Outlook and an interactive logon session. Print delivery and
the Power Automate handoff can run on other hosts.

On Windows, run this from the repository root.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
scripts\run_app.bat
```

For Outlook setup, firewall access, and scheduled startup, see the
[deployment guide](docs/DEPLOYMENT.md).

On Linux or macOS, run this from the repository root.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
cp -n .env.example .env
.venv/bin/python -m streamlit run streamlit_app.py
```

Open the app at `http://localhost:8501` on the host, or use the configured
internal host address from an operator workstation. See the
[operator handbook](docs/OPERATIONS.md) for the statement workflow.

## Configure the app

- `.env` holds optional `TEAMS_WEBHOOK_SUCCESS`, `TEAMS_WEBHOOK_ERROR`,
  and `UI_PASSCODE_SHA256` values. Environment variables override values
  saved in the file. Keep webhook URLs and the passcode hash private.
- `config/app_config.json` controls email send mode, mailbox delegation,
  throttle and retry timing, preview resolution, and backup retention.
  The Settings page can update send mode, throttle, and preview resolution.
  Set send mode to `send` to approve a new Power Automate handoff. Use `draft`
  to save messages in classic Outlook Drafts on Windows.
- `config/csv_schema.json` maps app fields to the column headers in the
  Raiser's Edge export.
- `templates/` contains four starter templates. Replace the sample artwork
  and fill in all `[REVIEW: ...]` copy, including the return address, EIN, and
  signatures, before sending statements. For templates that request Carlito,
  place `Carlito-Regular.ttf`, `Carlito-Bold.ttf`, and `Carlito-Italic.ttf` in
  `assets/fonts/`. The app uses Helvetica when those files are absent.
  See the [template author guide](docs/TEMPLATES.md).

To generate the optional operator passcode hash without putting the passcode
in shell history, run this with your Python interpreter.

```sh
python -c "import getpass, hashlib; print(hashlib.sha256(getpass.getpass().encode()).hexdigest())"
```

## Power Automate email flow

The app exports one approved JSON package per batch, up to 10 MiB, including
PDF attachments. An operator uploads the package to SharePoint and runs an
instant cloud flow. Limit access to the packages, app data, backups, and flow
history because they contain donor information. Apply your organization's
retention and disposal rules to each copy.

### Set up SharePoint and the mailbox

1. Create a restricted document library for approved packages. Give access
   only to authorized operators and the flow connection account.
2. On the same site create a **Statement Send Ledger** list. Use the built-in
   **Title** column for the job ID and enable **Enforce unique values** on it.
   Add a **Batch ID** single-line text column and a **Send status** choice
   column with `Needs review` and `Accepted for sending`. Restrict list
   editing to the flow account and staff who reconcile sends. Keep ledger rows
   for as long as a package could be replayed. Use the same ledger for every
   run of this flow.
3. In Power Automate, create an **Instant cloud flow** with **Manually trigger
   a flow** and a required text input named **Package path**. Configure the
   SharePoint and Office 365 Outlook connections. For a shared mailbox, ask
   your Microsoft 365 administrator to grant the Outlook connection account
   the required mailbox access and sending permissions. Confirm the approved From address with your administrator and check that both
   connectors and the chosen connections are permitted by your tenant.

### Build the flow

1. Add SharePoint **Get file content using path**. Set **Site Address** to the
   restricted site and **File Path** to the trigger's **Package path**. In
   **Parse JSON**, use **File Content** as **Content**. If the connector
   output is a binary wrapper, use this expression in **Content**, with the
   action's actual internal name:
   `base64ToString(body('Get_file_content_using_path')?['$content'])`.
2. Use the following schema in **Parse JSON**. Add a condition that stops the
   flow unless `version` is `1`. Add **Apply to each** over the parsed
   `jobs` array. Leave loop concurrency off.

   ```json
   {
     "type": "object",
     "required": ["version", "batch_id", "jobs"],
     "properties": {
       "version": { "type": "integer" },
       "batch_id": { "type": "string" },
       "jobs": {
         "type": "array",
         "items": {
           "type": "object",
           "required": ["job_id", "queue_id", "to", "subject", "body_html", "attachment_name", "attachment_base64"],
           "properties": {
             "job_id": { "type": "string" },
             "queue_id": { "type": "integer" },
             "to": { "type": "string" },
             "subject": { "type": "string" },
             "body_html": { "type": "string" },
             "attachment_name": { "type": "string" },
             "attachment_base64": { "type": "string" }
           }
         }
       }
     }
   }
   ```

3. Inside the loop add SharePoint **Create item** against **Statement Send
   Ledger**. Set **Title** to the current job's `job_id`, **Batch ID** to
   the parsed `batch_id`, and **Send status** to `Needs review`. This is the
   send gate. Configure the Outlook action to run only when **Create item**
   succeeds. A duplicate job ID or SharePoint error must skip the send. A
   duplicate can make the rerun show a failed **Create item**, which is
   expected. Check the existing ledger row to see the previous outcome.
4. Add **Send an email (V2)** for the Outlook connection's own mailbox, or
   **Send an email from a shared mailbox (V2)** and set its mailbox address.
   The Outlook connection or shared mailbox configured in this action sets
   the From address. Map the current loop item's fields as follows.

   | Outlook field | Value |
   | --- | --- |
   | To | `item()?['to']` |
   | Subject | `item()?['subject']` |
   | Body (HTML) | `item()?['body_html']` |

   In **Advanced parameters**, show **Attachments** and select **Switch to
   input entire array**. If you already configured individual attachment
   fields, replace them with this whole-array mapping. Use the **Expression**
   editor to enter the following expression.

   ```text
   createArray(addProperty(addProperty(json('{}'), 'Name', item()?['attachment_name']), 'ContentBytes', item()?['attachment_base64']))
   ```

   `ContentBytes` in the Outlook attachment array takes the PDF's base64
   string directly. The connector gets the original bytes from that value.

   In the Outlook action settings, set **Retry policy** to **None**. Enable
   secure inputs and outputs where supported for actions handling package or
   message content. Limit access to flow run history.
5. Add SharePoint **Update item** after the Outlook action with success-only
   **Run after**. Use the SharePoint item ID returned by **Create item**,
   preserve **Title** and **Batch ID**, and set **Send status** to
   `Accepted for sending`. A timeout, connection failure, or error writing
   the result leaves the existing entry at `Needs review`. Check run history
   and the sending mailbox's Sent Items or ask an Exchange admin to check
   message trace. Keep the original ledger entry while reconciling. Resolve
   uncertain outcomes before approving any replacement send. Connector
   acceptance can still be followed by a bounce.

### Approve, send, and reconcile a batch

After setting up or changing the attachment mapping, run a fresh sample
batch addressed to yourself. Open the received PDF and confirm that the file
name includes the primary addressee before sending a donor batch.

1. Generate and review the statements in the app, then select **Stage emails
   for review**. In **Email Queue**, select the batch, open **Send Now with
   Power Automate**, confirm that you reviewed the recipients and statements,
   and select **Confirm and prepare handoff**.
2. Select **Download approved email package**. Upload the generated JSON to
   the restricted SharePoint library and run the flow with its SharePoint
   file path. The app stores the approved package and returns the same payload
   and job IDs on subsequent downloads. It excludes the batch from desktop
   sending once the handoff is prepared.
3. In **Email Queue**, open **Record a result from the SharePoint ledger**.
   Match each exact job ID and record **Accepted for sending** or **Needs
   review**. Investigate missing or uncertain ledger entries in the flow run
   history and the sending mailbox before recording acceptance or approving
   any replacement send.

[SharePoint connector](https://learn.microsoft.com/en-us/connectors/sharepointonline/) ·
[Office 365 Outlook connector](https://learn.microsoft.com/en-us/connectors/office365/)

## Optional Teams notification flows

1. In the Teams **Workflows** app or Power Automate, create a cloud flow with
   the Microsoft Teams trigger **When a Teams webhook request is received**.
   The app sends a JSON POST with no authentication header, so select
   **Anyone** for the trigger's authentication setting if your tenant
   permits it. Treat the generated URL as a secret.
2. Add **Apply to each** using `triggerBody()?['attachments']`. Inside the
   loop add Microsoft Teams **Post card in a chat or channel**. Choose the
   destination and use `item()?['content']` for the Adaptive Card.
3. Save the flow, copy the trigger URL, and set `TEAMS_WEBHOOK_SUCCESS` for
   desktop Outlook send completion or `TEAMS_WEBHOOK_ERROR` for Outlook
   worker interruptions. Use the same URL for both notifications or create
   one flow per destination. Restart the app to load the URLs.

[Microsoft Teams connector](https://learn.microsoft.com/en-us/connectors/teams/)

## Repository layout

| Location | Purpose |
| --- | --- |
| `streamlit_app.py`, `app.py` | Startup, navigation, and operator screens |
| `core/batch.py`, `core/intake.py` | Batch workflow, CSV ingestion, and validation |
| `core/templates.py`, `core/documents.py` | Template handling, layout, PDFs, and previews |
| `core/persistence.py` | SQLite, backups, statement history, and audit |
| `core/delivery.py`, `core/messaging.py` | Queue, Power Automate handoff, Outlook, and Teams |
| `core/config.py`, `core/models.py`, `core/utils.py` | Configuration, data models, and shared utilities |
| `config/`, `templates/` | App settings, CSV schema, and statement templates |
| `scripts/`, `docs/` | Host setup and operating guides |
| `data/` | Local database, batch artifacts, backups, and logs |
