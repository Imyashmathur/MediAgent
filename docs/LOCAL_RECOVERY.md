# Local synthetic backup and recovery

This is a **manual development procedure for synthetic data only**, not a production disaster-recovery system. It was exercised using isolated Compose projects with synthetic PostgreSQL records and files. It does not provide encryption, off-site copies, automated schedules, point-in-time recovery, retention enforcement, or a production recovery-time guarantee. Independent security and operational review is required before any real patient data is used.

This runbook supports **local document storage only**. It does not back up or restore S3 objects, object versions, or delayed writes. Before snapshotting, the required preflight below rejects any object-upload intent or document-object link, including deleted or unresolved records. Do not use this runbook as a recovery method for an object-backed deployment; independently design and test an object-aware backup and reconciliation process.

The database, upload volume, and knowledge-import volume must represent the same quiescent point. PostgreSQL's `pg_dump` makes a consistent database snapshot, but it cannot coordinate the two file volumes. Stop all writers and block direct API access before taking the dump and archives. In-flight jobs may be interrupted and later recovered or retried; no Redis queue snapshot is included. The database outbox and job records are included, but they are not a substitute for production-grade queue durability. See the [PostgreSQL 16 pg_dump documentation](https://www.postgresql.org/docs/16/app-pgdump.html).

Backups contain sensitive records, token hashes, and copies of deleted data. Restrict access and use independently managed encryption, keys, retention, and disposal procedures. The example uses a local temporary directory with restrictive permissions; it is not an encrypted backup. Keep the matching application version, migration version, PostgreSQL/pgvector image, and configuration separately under appropriate access control. Do not put `.env`, passwords, or backup files into source control. Keep required secrets in a separate secrets-management system; losing or rotating secrets can invalidate sessions, reset links, or other dependent functionality.

## Take a quiescent snapshot

Run from the `MediAgent` directory with the intended local Compose project selected. **This stops its application services.** Ensure no direct clients or other writers can connect, and choose an appropriate maintenance window. The 180-second stop timeout gives jobs time to finish but does not guarantee they will; check service state and operational jobs before proceeding. Do not use these commands against a deployment containing real data.

```bash
docker compose stop -t 180 web api dispatcher worker document-purger reset-mailer
docker compose ps
```

Confirm all six services are stopped and the database remains available. A failed stop or any remaining writer invalidates the coordinated snapshot. Run the local-only preflight while writers are stopped:

```bash
docker compose run --rm -T --no-deps --entrypoint python api -m app.check_restore --local-only-preflight
```

**Stop if the preflight exits nonzero.** Any object-upload intent or document-object link makes the local-only archives insufficient, even if the current configuration uses local storage or a matching local file exists. A successful preflight is not a complete backup verification. The uploads archive can contain temporary staging files; inspect and protect it as sensitive synthetic data. In a shell on the Docker host:

```bash
umask 077
backup_dir="$(mktemp -d /tmp/mediagent-backup.XXXXXX)"
docker compose exec -T db pg_dump -U mediagent -d mediagent -Fc --no-owner --no-acl > "$backup_dir/database.dump"
docker compose run --rm -T --no-deps --entrypoint tar api -C /data/uploads -cf - . > "$backup_dir/uploads.tar"
docker compose run --rm -T --no-deps --entrypoint tar api -C /data/knowledge-imports -cf - . > "$backup_dir/knowledge.tar"
docker compose run --rm -T --no-deps --entrypoint alembic api current > "$backup_dir/alembic.txt"
(cd "$backup_dir" && shasum -a 256 database.dump uploads.tar knowledge.tar alembic.txt > SHA256SUMS)
printf 'Snapshot directory: %s\n' "$backup_dir"
```

Check the exit status of **each** command. A failed command can leave an empty or partial output file; discard an incomplete snapshot and repeat the entire process while writers remain stopped. The `shasum` command is available on macOS; use an equivalent SHA-256 tool if necessary. A checksum detects accidental changes only if the manifest itself is protected; it does not authenticate an untrusted backup. Inspect archive listings and preserve the matching version and environment information securely. Do not restore archives from untrusted sources: database restores can execute code, and tar archives can contain unsafe paths. See the [PostgreSQL pg_restore warning](https://www.postgresql.org/docs/16/app-pgrestore.html).

After the snapshot is complete and checked, the operator may resume the source stack with `docker compose up -d` and inspect its health and job states. Resuming processing may produce notifications, model requests, or other configured side effects.

## Restore into a fresh isolated project

Never restore over a running or existing database or file volume. Set `backup_dir` to the trusted snapshot directory above. Verify the checksums and inspect both tar listings before proceeding:

```bash
(cd "$backup_dir" && shasum -a 256 -c SHA256SUMS)
tar -tf "$backup_dir/uploads.tar"
tar -tf "$backup_dir/knowledge.tar"
cat "$backup_dir/alembic.txt"
```

Choose a new Compose project name, then inspect its containers and volumes. If **either** command shows an existing resource, stop and choose another name. Compose project names isolate resources as described in the [Docker documentation](https://docs.docker.com/compose/how-tos/project-name/).

```bash
restore_name="mediagentrestore$(openssl rand -hex 6)"
docker compose -p "$restore_name" ps -a
docker volume ls --filter "label=com.docker.compose.project=$restore_name"
```

With a verified empty name and the same application and database image versions, create only the isolated database and Redis. No application, dispatcher, worker, mailer, or purger is started. These services have no published ports in the supplied Compose file.

```bash
docker compose -p "$restore_name" up -d --wait db redis
docker compose -p "$restore_name" build api
docker compose -p "$restore_name" exec -T db pg_restore -U mediagent -d mediagent --no-owner --no-acl --exit-on-error < "$backup_dir/database.dump"
docker compose -p "$restore_name" run --rm -T --no-deps --entrypoint tar api -C /data/uploads -xf - < "$backup_dir/uploads.tar"
docker compose -p "$restore_name" run --rm -T --no-deps --entrypoint tar api -C /data/knowledge-imports -xf - < "$backup_dir/knowledge.tar"
docker compose -p "$restore_name" run --rm -T --no-deps --entrypoint alembic api current
docker compose -p "$restore_name" run --rm -T --no-deps --entrypoint python api -m app.check_restore
```

Stop on any failure. A failed `pg_restore` can leave a partial database: use a new isolated project for another attempt rather than trying to repair or overwrite an existing environment. Compare the reported Alembic revision with `alembic.txt` and the intended application version. The checker reports aggregate counts only; it checks the storage directories, verifies presence and size of nondeleted local document files, and compares SHA-256 hashes for newly uploaded documents and nondeleted knowledge imports. It reports object references without fetching or verifying them and exits nonzero if any object intent or link exists. Older documents have no stored hash and are counted as `legacy_documents_without_hash`; their contents cannot be verified by this checker, which exits nonzero for them. It does not detect all orphan files, validate clinical content, or prove the backup is complete and safe. Review any nonzero mismatch before proceeding.

An older backup can resurrect accounts, sessions, integration credentials, consent grants, documents, or information deleted or revoked after the snapshot. Pending mail and jobs may also run again. Keep the restore isolated and do not start the application or background services until an authorized operator has designed and performed reconciliation and credential/session revocation, verified the intended data, and disabled unwanted external providers and delivery. Restoring files does not revoke already shared copies or remove data from other backups.

After a synthetic drill, verify the project name again before removing **only that isolated project and its volumes**:

```bash
docker compose -p "$restore_name" down -v --remove-orphans
```

Do not run that command against the source project. Independently protect or dispose of backup files according to the applicable retention policy.
