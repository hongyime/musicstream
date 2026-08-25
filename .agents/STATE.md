# STATE

## 2026-08-24 — Wave 3 spec drafted
- SPEC.md §W3 added: Discovery Parity (LB weekly playlists -> MBID resolve -> auto-download), QUALITY_CUTOFF (default mp3_320, FLAC opt-in), blocklist, m3u export, webhook alerts + token early-warning, library browse/search tab.
- Constraints: $0 spend; Plex demoted to optional push; download-chain focus.
- Next: build W3a tasks T12-T18.

<!-- MOLT_AUTO_START -->
## Auto State

- Updated: 2026-08-25 07:32:36 +08:00
- Machine: PRAWN-L390
- Harness: claude
- Event: stop
- Branch: main
- HEAD: 91a0d60
- Dirty files: 20
- Resume hint: Read .agents/STATE.md, then the latest file in .agents/handoffs/ if present.
<!-- MOLT_AUTO_END -->

---
## 2026-08-25 - W3a IMPLEMENTED + VERIFIED (T12-T18)
- SPEC.md W3-T rows marked x. 313 tests pass incl. 27 new (blocklist/m3u/notify-token).
- Live QA: 2224 playlists exported to Y:/playlists w/ container-to-host path translation; block/unblock+reset-failed V7 verified via TestClient; token refresher FIXED live (secret-aware refresh; cached token was expired -1h, now fresh).
- Gotchas fixed along the way: env.py cp1252 decode, main.py cmd-after-main ordering, Path('/media') win32 backslash normalization, PKCE-vs-secret refresh semantics.
- NEXT: W3b = T19 cutoff+transcode -> T20 upgrade pass; T21-T23 discover-weekly; T24-T26 library tab + FE badges.

## 2026-08-25 - daemon redeployed on W3a code
- docker compose up -d --force-recreate daemon (dev override bind-mounts src -> no rebuild needed).
- Token refresher hardened: in-place cache write (os.replace breaks single-file bind mounts); secret-aware refresh verified LIVE both host+container. auth/status degraded=False.

## 2026-08-25 - W3b IMPLEMENTED (T19-T26) + pushed
- Transcode-on-import live (cutoff mp3_320, KEEP_FLAC_MASTER honored). Upgrade-pass = single bulk UPDATE w/ 500/run cap after mass-requeue incident (restored 114k rows).
- Discover-weekly engine done+tested; LIVE run empty because LB troi-bot feed needs opt-in -> user must follow listenbrainz.org/user/troi-bot.
- FE: Library tab + block buttons + token banner built into image? NO - docker build timed out on npm; dist docker-cp'd into running container. REBUILD DEBT: next docker compose build daemon will bake it properly.
- All containers Up. 326 tests green.
