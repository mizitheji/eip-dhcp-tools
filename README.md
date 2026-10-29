# EfficientIP DHCP tool

Creates, updates, verifies and deletes DHCP + IPAM sites on SOLIDserver from a CSV.

## Install (once)

```bash
cd ~/eip-dhcp                      # unzip the tool here; keep your existing .env and .venv
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade -r requirements.txt     # SOLIDserverRest >= 2.14 is needed for update
chmod 600 .env
```

`.env` keeps the same names as before (`EIP_HOST`, `EIP_USER`, `EIP_PASSWORD`, `EIP_VERIFY_SSL`).
See `.env.example` for the optional extras.

## Use

```bash
python eip-dhcp.py                          # interactive menu
python eip-dhcp.py scopes.csv --validate    # check CSV + server, read-only
python eip-dhcp.py scopes.csv --dry-run     # preview creating NEW sites
python eip-dhcp.py scopes.csv --wet-run --batch 10
python eip-dhcp.py scopes.csv --update --dry-run   # what differs on existing sites?
python eip-dhcp.py scopes.csv --update --wet-run
python eip-dhcp.py scopes.csv --verify      # read everything back and compare
python eip-dhcp.py scopes.csv --verify --site "Site name 1"
python eip-dhcp.py scopes.csv --delete --dry-run --site "Site name 1"
python eip-dhcp.py scopes.csv --delete --wet-run --site "Site name 1"
```

| Action | What it does |
|---|---|
| `--create` (default) | New sites only. A site whose DHCP scope already exists is **skipped untouched**. |
| `--update` | Makes existing sites match the CSV: creates what is missing, fixes what differs. Shows a diff in `--dry-run`. |
| `--verify` | Read-only comparison (IPAM network, scope, range, pool, options, member servers). Exit code 1 on differences. |
| `--validate` | Read-only CSV checks; add `--offline` to skip the server. |
| `--delete` | Deletes sites the way the GUI does: the IPAM network (and its block), after which the DHCP scope, range and pool follow. See "Delete" below. |

Create and update need `--dry-run` or `--wet-run`. Every wet run validates first and refuses to send rows with errors.

Other flags: `--site NAME` (repeatable), `--batch N` (ask after every N sites), `--yes`, `--keep-names`
(update never renames), `--no-ipam`, `--skip-pool`, `--skip-options`, `--options-only`.

## CSV

| Column | Needed | Notes |
|---|---|---|
| `space` | yes* | IPAM space, e.g. `Space name` |
| `site` | yes | Name of the block, network and scope |
| `network` | yes | CIDR, aligned to its prefix (`10.0.2.0/23`) |
| `dhcp_server` | yes* | The smart DHCP server, e.g. `DHCP server name` |
| `start_address`, `end_address` | yes* | Full IP, or a number counted from the network address (`10`, `510` in a /23) |
| `gateway` | no | Also becomes the router option. Full IP or offset |
| `dns_servers` | no | Comma, semicolon or space separated |
| `failover_channel` | no | A smart member server, e.g. `FO name` |

\* `space` and `dhcp_server` may be left empty if you set `EIP_DEFAULT_SPACE` / `EIP_DEFAULT_DHCP_SERVER` in `.env`.
Everything else comes from the CSV only. Extra columns are ignored, so old CSVs keep working.

`start_address`, `end_address` and `gateway` accept a full IP or a number counted from the network address
(`21`, `244`, `4` in a /24; `10` and `510` in a /23). Leave `gateway` or `dns_servers` empty and that option is not set.

## What one site gets

IPAM block + network (gateway, failover channel), DHCP scope on the smart server, range, IPAM pool `DHCP`
(read-only, linked to the range), router and DNS options. Member servers receive scope, range and options from the
smart server; the tool never writes to them.

**The server may create things itself.** On your server, creating an IPAM network that has a failover channel
(and gateway) makes the server create the DHCP scope on its own, and possibly the router option. Every create step
therefore looks again right before it acts and says "already there, made by the server" instead of failing.
If a run still stops half way, run **update** for that site (`--update --wet-run --site NAME`, or menu 4): it fills in
what is missing. `--create` never touches a site whose scope exists.

## Update: what it compares

IPAM network (name, gateway, failover; other class parameters are kept), scope (name, failover), range (start, end,
failover), pool (start, end), router and DNS option values. It never deletes anything. If a scope has several ranges
and none matches, it warns and leaves them alone.

## Delete

`--delete` works like deleting in the GUI: it removes the **IPAM network**, then its **block**, and the DHCP scope,
range and pool are expected to follow. It never deletes DHCP objects itself, and afterwards it checks that they are
really gone (waiting a few seconds for the sync). Anything left behind is reported, never hidden.

Safety rules, for every site:

* `--dry-run` first. `--wet-run` needs you to type `DELETE <number of sites>`; `--yes` does **not** skip this.
* It is driven by the CSV (`space`, `site`, `network` are enough) and `--site` narrows it. Use `--site`.
* A site is **refused** (nothing deleted, exit code 1) when: the IPAM network name differs from the CSV `site`
  (`--any-name` overrides), IPAM has registered IP addresses **other than the gateway** (`--allow-used` overrides;
  the gateway and the network/broadcast addresses that IPAM registers itself are ignored; in the menu, a site refused for this reason can be deleted anyway by typing `YES` after the list of addresses is shown), the range has active leases,
  or static leases exist (no override for these two).
* The block is deleted only if it is exactly this network, has the same name, and contains nothing else. A larger
  parent block is never touched.
* A copy of every object (network, block, pools, scopes, ranges, options) is saved to `runs/snapshots/` **before**
  anything is deleted, so you can recreate it by hand.
* If the DHCP scope exists but there is no IPAM network, the site is refused: remove that scope in the GUI.

## Files it writes

* `logs/` - one detailed log per run (API calls, replies, tracebacks). The console stays tidy.
* `runs/*.json` - the **ledger** of every wet run: ids created, updated or deleted.
* `runs/*_results.csv` - one line per site with its status.
* `runs/snapshots/*.json` - copies of everything a delete removed.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests
```
The tests run against a fake server modelled on your real one. **They do not prove the tool works on the live server.**
