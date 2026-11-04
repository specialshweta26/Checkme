# Moving attachments off the Render disk

`checkme-api` keeps attachments on a persistent disk. Render's docs say a disk "disables zero-downtime deploys": the old
instance stops before the new one starts, so the API is down during every deploy, and the web proxy logs
`getaddrinfo ENOTFOUND checkme-api-kw68`. This runbook moves the files to object storage so the disk can go.

The app code does not change. `app/storage.py` already supports S3 through `FILE_STORAGE`. Download links keep working
because each signed link holds the storage key, not a disk path, and keys stay the same.

## The three stages

| Stage | Change | Who | Can it lose files? |
|---|---|---|---|
| 1. Prepare | Merge this PR, create the bucket, add credentials, copy, verify | You, then the script | No. The app still uses the disk |
| 2. Switch | One-line PR: `FILE_STORAGE=s3`, then a final copy | You | No. The disk stays as a full copy |
| 3. Remove | PR removing the disk, **only after you confirm the files are copied** | You | This is the step that can't be undone |

## Recommendation: Cloudflare R2

| | Cloudflare R2 | Backblaze B2 | AWS S3 |
|---|---|---|---|
| Storage per GB-month | $0.015, first 10 GB free | about $0.007 | about $0.023* |
| Download fees | None | Free up to 3x stored data, then $0.01/GB | About $0.09/GB after 100 GB free* |
| Requests | $4.50 per million writes, $0.36 per million reads, with 1M and 10M free each month | Most free | $0.40 per million reads |
| Works with today's code | **Yes** | No | Only in `us-east-1` |

*AWS's pricing page did not show its storage and download rates when checked, so these come from third-party comparisons.
Check them before relying on them. R2 and B2 prices come from their own pricing pages, checked 2026-11-04.

**Why R2:** `storage.py` hardcodes `region_name="us-east-1"`, and R2's docs accept that value as an alias for its `auto`
region, so R2 needs no code change. B2 signs requests with the bucket's real region, so it would need a new setting.
B2 is cheaper per GB, but at up to 10 GB the gap is cents a month, and R2's free tier may cover all of it. The bucket
stays private: the API fetches the bytes and serves them, so no public access is needed.

## What else uses the disk

Nothing else needs it, so it can go once the files are copied. Checked in the code:

- **`app/storage.py` is the only code that writes to it.** Every attachment path goes through `put_bytes` and `get_bytes`
  (`files.py`, `agent/attachments.py`, `agent/invoke.py`, `channels/service.py`).
- **`computer_daemon.py` lists `/var/data` in `_UPLOAD_ROOTS`, but that code runs inside the E2B or Daytona sandbox**, not
  on the API, so it never touches this disk.
- **Other file writes** (`daytona_box.py` temp files, `recorder.py`, `.lgraph/` in a local checkout) go to temp folders or
  the sandbox, or are not used by the deployed code.

Step 3 below also lists `/var/data` on the live disk, in case something is there that the code doesn't show.

## Health check: not available, and not needed

Render supports `healthCheckPath` on web services only: "Private services only support default TCP checks." So
`checkme-api` can't have one, and the Blueprint should not set it.

The TCP check gives the same result here. Uvicorn runs the app's startup (including database migrations) before it opens
its port, confirmed in the installed `uvicorn/server.py`. So the new instance can't pass the check until startup finishes,
and if startup fails the old instance keeps serving. Making the API a web service would give it a public URL, which isn't
worth it.

## Old and new instances will overlap

Removing the disk means two API instances run at once on each deploy, which has never happened in this service. Render
sends traffic to the new one, then stops the old one at least 60 seconds later. Each background worker was checked:

| Worker | During the overlap |
|---|---|
| Jobs, memory, scheduler | Safe. They claim work with `FOR UPDATE SKIP LOCKED` |
| Stop button | Safe. `agent/cancel.py` passes stops between processes through Redis, and jobs through their status |
| Inbox sync | Both instances can poll. `inbox_items` has a unique `(user_id, message_id)`, so no duplicate rows, but a clash can log an error for one tick, and classification may run twice and cost a little extra model use |
| Database migrations | **New rule.** The new code migrates while the old code is still serving, so a migration must not drop or rename anything the previous version uses. Today's migrations only add |

## Runbook

Run the script from the `checkme-api` **Shell** tab in the Render dashboard, which Render supports for private services.
It runs one file at a time inside the live instance, and is safe to re-run if the shell disconnects. A deploy ends the
shell session, so wait for deploys to finish first.

### Stage 1: Prepare

**1. Merge this PR.** Nothing changes in production. `FILE_STORAGE` stays `disk`.

**2. Create the bucket and credentials.** This needs your Cloudflare account, and Cloudflare may ask for a payment method.

- Create an R2 bucket named `checkme-attachments`. Set a location hint near the API's Render region, which is shown on
  the service's page.
- Create an R2 API token with **Object Read & Write**, limited to that bucket.
- On `checkme-api` → Environment, add these three, then save and deploy:

| Key | Value |
|---|---|
| `S3_ENDPOINT_URL` | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` |
| `S3_ACCESS_KEY` | The token's access key ID |
| `S3_SECRET_KEY` | The token's secret access key |

**3. Look at the disk and test the credentials.**

```bash
cd /app && du -sh /var/data/* && find /var/data/attachments -type f ! -name '*.ctype' | wc -l
```

```bash
python scripts/migrate_attachments.py check
```

Stop if anything other than `attachments` and `lost+found` is in `/var/data`, and work out what it is first. Write down
the file count.

If `check` fails with a checksum or "not implemented" error, add these two environment variables and try again. No code
change is needed: `AWS_REQUEST_CHECKSUM_CALCULATION=when_required` and `AWS_RESPONSE_CHECKSUM_VALIDATION=when_required`.

**4. Copy and verify.**

```bash
python scripts/migrate_attachments.py copy
```

```bash
python scripts/migrate_attachments.py copy --apply
```

```bash
python scripts/migrate_attachments.py verify
```

The first command only reports. `verify` downloads every object and compares its SHA-256 with the disk. It may list a few
`MISSING` files if people uploaded during the copy, and the final copy in stage 2 picks those up. `disk_files` should
match the count from step 3, plus any new uploads.

### Stage 2: Switch

**5. Copy once more, then merge the switch** right away, so few files arrive in between:

```bash
python scripts/migrate_attachments.py copy --apply
```

```diff
       - key: FILE_STORAGE
-        value: disk
+        value: s3
```

Make this change in `render.yaml`, not the dashboard, because a later Blueprint sync could set it back. This deploy still
has the usual brief downtime. From here new uploads go to the bucket, and nothing new is written to the disk.

**6. Copy the last files as soon as the deploy is live.** Files uploaded just before the switch return "File not found"
until this runs.

```bash
python scripts/migrate_attachments.py copy --apply
```

```bash
python scripts/migrate_attachments.py verify
```

**`verify` must exit 0, with `ok` equal to `disk_files` and no `MISSING` or `DIFFERENT` lines.** `bucket_objects` can be
higher, since new uploads exist only in the bucket.

`DIFFERENT` means the first copy caught a file while it was still being written. The disk now holds the finished file, so
run `copy --apply --replace-different`, then `verify` again.

**7. Try it in the app.** Open an old attachment, upload a new one and open it, and preview a PDF. In the API logs,
`object storage not ready` at startup is harmless if `check` passed, since a bucket-limited token may not be allowed to
inspect or create buckets.

**8. Let it run for a few days.** Three days of normal use is a reasonable minimum. The disk is your fast way back, and
deploys still have downtime until stage 3.

### Stage 3: Remove the disk

**This needs your explicit go-ahead.** Check that you can answer yes to each:

- [ ] `verify` exits 0 when run again today
- [ ] Old and new attachments both open in the app
- [ ] You've confirmed that you're happy to delete the disk

**9. Merge the removal PR.**

```diff
-    disk:
-      name: checkme-files
-      mountPath: /var/data
-      sizeGB: 10
     envVars:
       - key: FILE_STORAGE
         value: s3
-      - key: STORAGE_DISK_PATH
-        value: /var/data/attachments
```

In the same PR, remove the disk from `docs/HLD.md`, `docs/diagrams.md` and the README, and set the `FILE_STORAGE` row in
`docs/LLD.md` to `s3` for Render.

Render's docs don't say what happens to a disk when its block is removed from a Blueprint. Check the service's **Disks**
page afterwards. If the disk is still attached, delete it there, because zero-downtime deploys stay off until it's gone.

**10. Confirm zero downtime.** Deploy any small change, and watch for `getaddrinfo ENOTFOUND` in the `checkme-web` logs
and try the app during the switch. Render's docs don't say whether a private service's hostname stays resolvable during a
deploy, so this test is the real proof. Keep the web proxy retry either way.

## Rollback

| Where you are | What to do | Effect on files |
|---|---|---|
| Stage 1 | Nothing needed. The app never used the bucket. Delete it if you like | None |
| Stage 2, disk still attached | Revert `FILE_STORAGE` to `disk` in `render.yaml`. Once deployed, run `restore`, then `restore --apply` | Uploads made since the switch are copied back. They return "File not found" until `restore --apply` finishes |
| Stage 3, disk gone | Add the `disk` block and `STORAGE_DISK_PATH` back, set `FILE_STORAGE=disk`, then run `restore --apply` | The bucket holds everything, so a new disk is rebuilt in full. Zero-downtime deploys stop again |

Keep the bucket and credentials until you're sure you won't roll back. `restore` never overwrites a file that differs
unless you pass `--replace-different`. Render's daily disk snapshots last seven days, but restoring one discards anything
written after it, so `restore` is the better route.

## How this was tested

- `apps/api/tests/test_migrate_attachments.py` covers the script against an in-memory S3 and the real `app.storage` code.
- The commands above ran in order with real boto3 against a local S3 server (moto), including the switch to S3 mode and
  the rollback.
- **Not tested against real R2**, because that needs your account. `check` in step 3 is the first real test, and nothing
  depends on R2 until it passes.
- **Zero-downtime behaviour is not tested**, and can only be seen in step 10.
