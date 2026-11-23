# BFAT Flipkart Review Report Automation

This project extends the existing Selenium scraper with request-scoped product discovery, date windows, Excel reports, email delivery, a GitHub Actions workflow on a self-hosted Windows runner, and a Google Apps Script form trigger. It reuses the original review parser, pagination logic, date filtering, authentication flow, classifier, and workbook writer.

## Architecture

```text
Google Form
  -> linked Google Sheets response row
  -> installable Apps Script form-submit trigger
  -> Apps Script validates the request and calls the GitHub API workflow_dispatch directly
  -> GitHub Actions runs run_request.py on your self-hosted PC runner
  -> product resolver opens the supplied Flipkart product URL and reads the product title
  -> existing Selenium scraper collects accessible individual reviews in the date range
  -> one request-specific four-sheet workbook is written under reports/<REQUEST_ID>/
  -> Gmail SMTP sends only that workbook to the form recipient
  -> workflow calls the authenticated Apps Script web endpoint to update the response row
```

Requests use separate folders, status files, diagnostics folders, and email-delivery markers. GitHub Actions serializes runs because the self-hosted runner uses one persistent Chrome profile; each request still has its own workbook and recipient. SMTP retries are limited to failures that establish the message was not accepted. The email marker is claimed before delivery, so an interrupted or uncertain send is not automatically repeated. Inspect the marker before any manual resend.

Flipkart may change its page structure, show no reviews, limit public access, or require an OTP/CAPTCHA. The scraper reports these cases; it does not solve or bypass security checks. It opens the exact URL supplied in the form and stops if that URL is not a product page. Date coverage is reported as observed rather than assumed.

## Project files

Existing core modules retained and extended:

- `Flipkart_Reviews_Hearables.py` – existing scrape orchestration and classification, plus request metadata.
- `fk_collect.py`, `fk_parsing.py`, `fk_browser.py`, `fk_login.py` – existing collection, parsing, Selenium, and safe optional login.
- `fk_excel.py` – existing workbook styling, deduplication, date filtering, aggregate ratings, and placeholder removal; current workbooks have exactly four sheets.
- `Auto_Email_Flipkart_OnlyJk.py` – existing bulk mailer plus request-scoped, at-most-once report delivery.
- `request_flow.py` – request validation and calendar-safe week/month/year range calculation.
- `product_resolver.py` – validates a Flipkart product URL and reads the product name from the listing page.
- `run_request.py` – one-request execution entry point used by GitHub Actions and local runs.
- `google_apps_script.gs` – Apps Script form trigger and authenticated status callback.
- `.github/workflows/flipkart-request.yml` – parameterized workflow for the self-hosted Windows runner.
- `update_google_sheet.py` – workflow completion callback client.
- `.env.example` – placeholders only; no real `.env` is included.
- `tests/` – offline fixtures and tests.

## Workbook format

Each generated workbook contains exactly these four sheets:

1. **Positive Reviews** – individually extracted 4- and 5-star review records.
2. **Negative Reviews** – individually extracted 1-, 2-, and 3-star review records.
3. **Star Ratings** – Flipkart's aggregate star totals and overall rating, explicitly labeled all-time and separate from the requested period.
4. **Time** – request ID, product, requested duration, date window, scrape/report timestamps, extracted count, collection/coverage status, stop reason, and errors.

No placeholder rows are synthesized from aggregate counts. Missing fields remain `N/A` or blank. Keyword complaint classification is kept with the review record when supported by its text. A month-only/relative date is flagged as uncertain according to the existing date parser.

## Local installation (Windows PowerShell)

Use Python 3.10 or newer and install a current Chrome browser. Selenium Manager normally obtains a matching driver; if the machine has no driver-download access, install a matching ChromeDriver and put it on `PATH`.

```powershell
cd 'C:\path\to\BFAT_Flipkart_AudioProducts_v2.1'
Copy-Item .env.example .env
# Edit .env in a local editor and enter your own credentials. Never add .env to a ZIP or Git.
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pytest tests -q
node --test tests/google_apps_script_inputs.test.cjs
```

For a local one-request run, use a new request ID and actual dates that match the duration. Example:

```powershell
$env:SMTP_USER = 'sender@gmail.com'
$env:SMTP_APP_PASSWORD = 'your-gmail-app-password'
python .\run_request.py `
  --request-id REQ-20261022-901 `
  --product-url 'https://www.flipkart.com/.../p/...' `
  --duration 3 `
  --duration-unit months `
  --recipient-email 'recipient@example.com' `
  --start-date 2026-07-22 `
  --end-date 2026-10-22
```

The product URL is the only product input. The scraper opens that listing and reads the product title from Flipkart for the report. It stops if the URL is invalid, redirects away from a Flipkart product page, or the page has no readable title.

Legacy CSV mode remains available for the existing audio product list:

```powershell
python .\Flipkart_Reviews_Hearables.py --only 'Airdopes 800' --headless
```

The legacy bulk email command is retained for backward compatibility, but automated form requests use the isolated `send_request_report` path.

## Google Form and Sheets setup

1. Create a Google Form with these exact question titles:
   - `Flipkart Product URL` – short answer, required. In the screenshot, switch **Required** on for this question.
   - `Duration` – short answer with response validation for an integer from 1 to 120, required.
   - `Duration Unit` – dropdown: `Weeks`, `Months`, `Years`, required.
2. In Google Forms settings, enable **Collect email addresses**. Apps Script uses the collected respondent email as the report recipient, so users do not need a separate email question.
3. Link the Form to a Google spreadsheet. Keep the form response sheet name (often `Form Responses 1`) for configuration.
4. In **Extensions → Apps Script**, paste `google_apps_script.gs`.
5. In **Project Settings → Script Properties**, set:
   - `SPREADSHEET_ID` – the linked spreadsheet ID.
   - `RESPONSE_SHEET_NAME` – exact response tab name.
   - `GITHUB_TOKEN` – fine-grained personal access token limited to this repository, with **Actions: Read and write**.
   - `GITHUB_REPOSITORY` – `owner/repository`, e.g. `harshithadevadiga99-maker/Flipkart-ReviewAutomation`.
   - `GITHUB_WORKFLOW` – optional, defaults to `flipkart-request.yml`.
   - `GITHUB_REF` – optional, defaults to `main`.
   - `SHEETS_CALLBACK_SECRET` – a long random secret, also stored as a GitHub Actions repository secret.
6. Add an **installable trigger** for `onFormSubmit`, event source **From spreadsheet**, event type **On form submit**. Authorize spreadsheet access and external requests (GitHub API) when prompted.
7. Deploy the script as a web app for the callback: **Execute as me** and allow access to anyone. The callback checks `SHEETS_CALLBACK_SECRET`; store a long random value, do not use a guessable string. Save the deployed `/exec` URL as GitHub Actions secret `SHEETS_CALLBACK_URL` and the same shared key as `SHEETS_CALLBACK_SECRET`.

Apps Script calculates the date range on the submission date in the spreadsheet script timezone, generates `REQ-YYYYMMDD-NNN`, and starts the GitHub Actions workflow directly through the GitHub API with the URL-only request and dates. The respondent email is passed as `RECIPIENT_EMAIL`. There is no Gmail listener, trigger inbox or Task Scheduler job: the only program that must be running on your PC is the self-hosted GitHub Actions runner. The GitHub token stays in Apps Script **Script Properties**; never put it in the source code. GitHub Actions posts the final status back through the shared-secret callback.

### Simplified request inputs

`Flipkart Product URL` is the only product field and must be required. Paste the exact product listing URL; the scraper opens that page and reads its title for the report. Google Forms collects the report recipient email automatically.
## GitHub Actions and self-hosted runner setup

Create a private GitHub repository containing this project. On the PC that should scrape, install and register a GitHub Actions self-hosted runner for that repository with the `self-hosted`, `windows`, and `x64` labels. Run the runner under the same Windows user that owns the persistent Chrome profile and has access to Chrome, Python 3.10+. Keep the PC awake and the runner service online. The workflow checks out the repository and installs `requirements.txt` for each run. The signed-in Chrome profile is stored under that runner user's `%LOCALAPPDATA%\FlipkartAutomation\ChromeProfile` so checkout cleanup does not erase it.

Add these GitHub repository Actions secrets:

- `FKART_LOGIN_ID`, `FKART_PASSWORD`, `FKART_EMAIL`, `FKART_APP_PASSWORD` – Flipkart and optional email OTP login values.
- `SMTP_USER`, `SMTP_APP_PASSWORD` – Gmail sender and App Password used to email the Excel report.
- `SHEETS_CALLBACK_SECRET`, `SHEETS_CALLBACK_URL` – shared secret and deployed Apps Script `/exec` URL.
- `ALERT_TO` – optional failure alert destination.

Restrict repository and Actions access to authorized staff. The workbook contains customer-submitted review content. Artifacts are retained for 14 days by default; protect the Google Sheet and Apps Script project as well.

The workflow runs headless, emails the workbook, updates the sheet even when scraping fails, and uploads the request status and workbook as a run artifact. Review `reports/<REQUEST_ID>/request_status.json` and `diagnostics/<REQUEST_ID>/` on the runner when troubleshooting.

## Gmail login and OTP setup

For Gmail SMTP, enable Google 2-Step Verification and create an App Password for the sender account. Store it only in GitHub Actions secrets or a local ignored `.env`. Do not use the account's normal password. The sender is read from `SMTP_USER`; the password is read from `SMTP_APP_PASSWORD`.

For an email-based Flipkart login, set `FKART_LOGIN_ID` to the account email and configure `FKART_EMAIL` plus `FKART_APP_PASSWORD` if you want the existing read-only Gmail OTP check.

Set `FKART_LOGIN_ID` to your Flipkart email address. Flipkart emails the OTP to that inbox, and the script reads it over Gmail IMAP using `FKART_EMAIL` and `FKART_APP_PASSWORD` (a Gmail app password with IMAP enabled), accepting only a fresh message from a `flipkart.com` sender. If `FKART_LOGIN_ID` is a phone number, the OTP is not read automatically and you must type it yourself in a visible browser. The Selenium browser opens Flipkart's login page and later runs the feedback collection.

To force a new login and collect one chosen product, first run the offline suite with `python -m pytest tests`. Then run `python Flipkart_Reviews_Hearables.py --show-browser --fresh-login --product-url "<Flipkart product URL>"`. Fresh-login uses the separate `.chrome-profile-fresh-login` profile and clears Flipkart cookies plus local/session storage only inside that profile. It does not modify the normal `.chrome-profile` or data for other websites. The configured login ID is filled and submitted automatically, and the OTP is read from Gmail and submitted automatically. If Flipkart requests a voice call, answer it yourself; CAPTCHA and other security prompts must also be completed by you. After signed-in status is verified, the supplied product URL is scraped using the configured date range and project settings. For bulk scraping, restore the configured CSV (`Flipkart_Hearables_URLs.csv`). To test login without scraping, add `--login-only` instead of `--product-url`.

**OTP privacy:** The Python project does not log OTP values or include them in its run report. The Gmail inbox is read over IMAP only to select one fresh Flipkart code; use a Gmail app password and keep it in `.env` or GitHub secrets.

Form-triggered GitHub Actions runs use a background (headless) browser and the dedicated persistent profile under `%LOCALAPPDATA%\FlipkartAutomation\ChromeProfile` on the self-hosted runner. A successful sign-in is reused on later form runs. Email login reads the OTP automatically from Gmail. Flipkart may request a phone call for some logins, but it does not call every time and the script cannot request a call. Login is required; if it is not confirmed, the scrape stops instead of silently collecting anonymously. For a visible one-time sign-in, run the following in PowerShell under the runner's Windows account so it uses the same profile:

```powershell
$env:FKART_CHROME_PROFILE_DIR = "$env:LOCALAPPDATA\FlipkartAutomation\ChromeProfile"
python .\Flipkart_Reviews_Hearables.py --login-only --show-browser
```

CAPTCHA and other browser-only verification must be completed in a visible run.

Review collection has no fixed page-count or review-count cap by default (`max_pages: 0`, `max_reviews: 0`). It continues through available review pages until the requested date range is reached or Flipkart stops returning more reviews. A `Partial` result still means the scraper could not verify that the requested range was fully covered.

Set `ALERT_TO` as a GitHub Actions secret or in `.env` to an administrator mailbox if failure alerts are desired. Alerts contain request status and diagnostic context, not secrets.

## Status and troubleshooting

Request statuses written to each request folder and the sheet include `Pending`, `Running`, `Completed`, `Partial`, `Failed`, and `Email Sent`. The request folder is `reports/<REQUEST_ID>/`; it contains that request's workbook, `request_status.json`, and `.email_delivery.json` delivery marker. A marker in `delivery_uncertain` state is deliberately not resent automatically because SMTP may have accepted the message before a connection error.

- **Product page not found or title missing:** verify the Flipkart product URL opens a product listing page with a readable title.
- **No review records / changed page:** inspect the request status and its diagnostic HTML/screenshot; selectors and text parsing are in `fk_config.json` and `fk_parsing.py`.
- **CAPTCHA or OTP required:** do not bypass it; configure authorized login if possible or use public reviews when available.
- **Email failed:** verify the Gmail App Password and SMTP credentials. Inspect the request delivery marker before any manual retry.
- **Sheet not updated:** check GitHub Actions callback secrets, Apps Script deployment URL/permissions, and Apps Script execution logs.
- **Partial coverage:** check the workbook Time sheet and `request_status.json` for coverage and pagination stop reason. A partial result does not claim that every accessible review was reached.

## Testing and live checks

Offline tests use saved HTML fixtures and mocks; they do not access Flipkart, Google, GitHub, or Gmail. The Python suite covers URL-only request validation, direct-URL title resolution, date calculations, review parsing/classification, duplicate handling, request isolation, email-recipient/workbook isolation, email idempotence, and the four-sheet Excel output. Node.js tests cover form email selection and product URL validation.

After configuring services, submit a test Form response with a low review window and a known product URL. Confirm the new request ID/status on the response row, GitHub Actions run inputs and result, the four workbook sheets and date range, and arrival of one attachment at the specified recipient. Then test a nonexistent product and a deliberately invalid URL and confirm the sheet records failure. A live run is only verified when performed in your configured environment; this package does not claim live Flipkart, Google, GitHub, or SMTP verification.


