# Using the Linux box as a Veeam repository

## The confusion worth clearing up first

Veeam Backup & Replication Community Edition must run **on Windows**. Running
the backup *server* itself on Linux needs a paid VUL licence. That much is
true, and it is easy to extend it one step too far into "so backups have to
land on the Windows box."

They don't. Adding a **Linux server as a backup repository** is a standard
feature and is not licence-gated. The Windows machine stays the backup server;
the Linux machine becomes storage it manages over SSH.

The difference is the whole data path:

```
SMB share as repository (the workaround)
  Linux agent  ──network──▶  Windows gateway  ──network──▶  SMB share ──▶ nvme0n1
                                                            (same Linux box)

Linux repository (what the product supports)
  Linux agent  ──▶  nvme0n1
```

Same destination disk. One of them crosses the network twice and routes every
byte through a gateway to get back to where it started.

## Why it is worth changing

Beyond removing the round trip, a real Linux repository unlocks two things a
CIFS/SMB repository structurally cannot do:

**Fast Clone.** On XFS with `reflink=1`, Veeam builds synthetic fulls by block
cloning instead of copying. A synthetic full then costs almost no additional
space. That is the difference between needing a spare full backup's worth of
free space forever and needing almost none — see
[AUDIT #9](AUDIT.md#9-the-veeam-job-had-not-run-in-three-days), where a
repository ran out of room doing exactly this. ext4 has no reflink, so Fast
Clone is unavailable there.

**Immutability.** A hardened Linux repository can make restore points immutable
for a retention period. An SMB repository cannot, which shows up in the job log
as:

```
Skip immutability set. Reason: Skipping immutability processing because the
immutability settings are not available for backup <id>
```

If ransomware is in your threat model, this is the setting that matters, and
SMB cannot give it to you at any retention level.

> Note the tension: immutability requires periodic fulls, so it rules out
> forever-forward incremental. With Fast Clone you don't need forever-forward
> anyway — synthetic fulls stop being expensive.

## Order of operations

**Fix the free space before migrating.** Pointing a job at a new repository
starts a new backup chain, and a new chain begins with a full. If the volume
cannot fit one, the migration fails the same way the synthetic full did. Get
free space above the size of one full first, by reducing retention or clearing
the older full.

## Prerequisites

Most of these are already satisfied on a machine VBR is backing up with the
Linux agent, because the transport components are the same ones:

| Requirement | Check |
|---|---|
| SSH reachable | `systemctl is-active ssh` and `ss -tln \| grep :22` |
| Veeam transport installed | `dpkg -l \| grep veeamtransport` |
| Transport port listening | `ss -tln \| grep 6162` |
| Account with root or sudo | the wizard asks for it |
| Firewall permits 22 + 6162 from the backup server | `ufw status` |
| Target path exists and has space | `df -h /mnt/storage` |

On this build all of these were already true — `veeamtransport` was installed
and listening on `6162` before any repository work started, because agent
management had already deployed it.

## Step 1 — add the Linux server as a managed server

If the machine is only known to VBR as a protected computer, it still needs
adding as a managed server.

```
Backup Infrastructure -> Managed Servers -> Add Server -> Linux
```

The wizard runs:

1. **Before You Begin**
2. **Specify Server Name or Address** — hostname or IP
3. **Specify Credentials and SSH Settings** — SSH account, root or sudo-capable.
   Accept the host fingerprint when prompted.
4. **Review Components** — VBR lists the transport components it will deploy
5. **Apply Settings**
6. **Finish**

If the components are already present, this step is fast and non-disruptive.

## Step 2 — add the repository

```
Backup Infrastructure -> Backup Repositories -> Add Repository
                      -> Direct attached storage -> Linux
```

The wizard runs:

1. **Name and description**
2. **Server settings** — pick the Linux server from step 1
3. **Repository settings** — browse to the path (e.g. `/mnt/storage/backups`),
   set concurrent task limits and any read/write rate limit
4. **Mount server settings** — normally the Windows backup server
5. **Review properties and components**
6. **Apply**
7. **Finish**

In repository settings, **Advanced** holds the Fast Clone option (wording is
along the lines of using fast cloning on XFS volumes). It is unavailable on
ext4. If it is greyed out, that is the filesystem telling you, not a licence.

## Step 3 — bring the existing backups across

Do **not** point two repositories at the same directory. Instead:

1. Note the current repository's path and the restore points in it
2. Remove the old SMB repository from VBR — removing the repository object
   does not delete the backup files on disk
3. Add the Linux repository pointing at that same path
4. Right-click the repository → **Rescan**. VBR imports the existing chain and
   the restore points reappear
5. Only then edit the job → **Storage** → select the new repository

Verify the restore points are listed before you let the next scheduled run
happen.

## Step 4 — verify

```bash
# On the Linux box, during and after a run:
ls -lt /mnt/storage/backups/*/ | head        # new .vbk/.vib appearing locally
ss -tn | grep :6162                          # transport connection from the backup server
df -h /mnt/storage                           # space moving as expected
```

In the console, confirm the job reports the new repository and that a restore
point can be browsed — not merely that the job succeeded.

## The XFS question

Fast Clone is the reason to care about the filesystem, and it cannot be
retrofitted: `reflink` is set at `mkfs` time, so switching means reformatting
and therefore relocating whatever is on the volume first.

```bash
# Does this volume support it?
xfs_info /mnt/storage | grep reflink     # reflink=1 required
findmnt -no FSTYPE /mnt/storage          # ext4 -> not available
```

If you are building a machine like this from scratch, make the backup volume
XFS with `reflink=1` from the beginning:

```bash
mkfs.xfs -m reflink=1 /dev/nvmeXn1p1
```

It costs nothing at build time and is expensive to change later — which is
exactly the kind of decision worth getting right once.

## Sources

- [Adding Linux Repositories Using Console — Veeam B&R User Guide](https://helpcenter.veeam.com/docs/vbr/userguide/linux_repository_add.html)
- [Linux Server — Veeam B&R User Guide](https://helpcenter.veeam.com/docs/vbr/userguide/linux_server.html)
- [Forever Forward Incremental Backup — Veeam B&R User Guide](https://helpcenter.veeam.com/docs/vbr/userguide/incremental_forever_backup.html)
