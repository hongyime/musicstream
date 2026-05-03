# Staging Mode Configuration

## Overview

This document explains the staging/development mode for the musicstream daemon, which disables all music downloads while allowing other features to run.

## Purpose

Staging mode is designed for:
- **Development**: Test code changes without downloading music
- **Debugging**: Investigate issues without consuming bandwidth
- **Testing**: Verify configuration and database integrity
- **Deployment Prep**: Test production deployment without affecting actual music library

## What Still Runs in Staging Mode

✅ **Enabled features:**
- Spotify incremental sync (updates track database)
- ListenBrainz discovery (finds new music recommendations)
- Integrity checks (validates existing files)
- Database backups (automated backups)
- Health endpoints (monitoring)
- Manual sync triggers (without downloads)

❌ **Disabled features:**
- All music downloads (SpotiFLAC, yt-dlp, spotdl, etc.)
- File organization to media drive
- Tagging of downloaded files

## Configuration

### Environment Variables

Add to your `.env` file:

```bash
# Enable staging mode - disables all downloads
DISABLE_DOWNLOADS=1
```

Accepted values: `1`, `true`, `yes`, `on` (case-insensitive)

### Docker Compose Override

For production deployment, ensure `DISABLE_DOWNLOADS` is **NOT** set:

```yaml
services:
  daemon:
    environment:
      # DO NOT set DISABLE_DOWNLOADS in production
      # - DISABLE_DOWNLOADS=0
      # - DISABLE_DOWNLOADS=false
```

## Visual Indicators

### Startup Banner

When staging mode is enabled, the daemon startup banner shows:

```
╔════════════════════════════════════════════════════╗
║         MUSICSTREAM DAEMON v3.0                    ║
╠════════════════════════════════════════════════════╣
║ Last full run:   2026-05-03 20:00 SGT              ║
║ Downloaded:   0  │  Failed:    0  │  Requeued:  0  ║
║ DB tracks:   200  │  Missing:   0  │  Corrupt:  0  ║
║ LB recs:     200  │  Ingested: 200                  ║
║ errors.log: 0.0MB / 5MB                             ║
║                                                        ║
║ DOWNLOADS DISABLED (STAGING MODE)                   ║
╚════════════════════════════════════════════════════╝
```

### Log Messages

You'll see these messages in the logs:

```
INFO  Downloads disabled via DISABLE_DOWNLOADS environment variable - skipping download pipeline
INFO  Step 6/9: Running download pipeline…
INFO  Downloads disabled via DISABLE_DOWNLOADS environment variable - skipping download pipeline
INFO  Downloads disabled via DISABLE_DOWNLOADS - skipping download in manual sync
```

## Use Cases

### 1. Development Testing

```bash
# In .env file
DISABLE_DOWNLOADS=1

# Run daemon
docker-compose up -d daemon

# Make code changes
vim src/ingestion/downloader.py

# Rebuild and test
docker-compose build daemon
docker-compose up -d daemon

# Check logs - should see "DOWNLOADS DISABLED" messages
docker logs -f musicstream-daemon
```

### 2. Production Deployment

```bash
# In .env file (production)
# DO NOT set DISABLE_DOWNLOADS, or set to:
DISABLE_DOWNLOADS=0

# Deploy to production
docker-compose build daemon
docker-compose up -d daemon

# Verify downloads are enabled - should NOT see staging warning
docker logs musicstream-daemon | grep "DOWNLOADS DISABLED"
# Should return nothing
```

### 3. Testing Sync Without Downloads

```bash
# Trigger manual sync - Spotify sync runs, downloads are skipped
curl -X POST http://localhost:9079/sync

# Check status
curl http://localhost:9079/status

# Verify no downloads occurred
curl http://localhost:9079/metrics | jq '.overall'
# Should show: tracks_downloaded: 0
```

## Database State

In staging mode:
- Track database is still updated via Spotify sync
- New tracks are discovered and added to `pending` status
- But they remain in `pending` status (never downloaded)
- This allows testing of the full pipeline flow without actual downloads

## Migration to Production

When moving from staging to production:

1. **Remove staging configuration:**
   ```bash
   # In .env
   # DELETE or comment out:
   # DISABLE_DOWNLOADS=1
   ```

2. **Update media drive path:**
   ```bash
   # Change from staging directory to production media
   EXTERNAL_MEDIA_DRIVE=/path/to/production/music
   ```

3. **Rebuild and deploy:**
   ```bash
   docker-compose build daemon
   docker-compose up -d daemon
   ```

4. **Verify downloads are enabled:**
   ```bash
   # Should NOT see staging warning in logs
   docker logs musicstream-daemon | grep "DOWNLOADS DISABLED"
   
   # Should see active download attempts
   docker logs -f musicstream-daemon | grep "download"
   ```

## Troubleshooting

### Downloads Still Running

If you see downloads despite setting `DISABLE_DOWNLOADS=1`:

1. Check environment variable:
   ```bash
   docker exec musicstream-daemon env | grep DISABLE_DOWNLOADS
   ```

2. Rebuild daemon (code changes required):
   ```bash
   docker-compose build daemon
   docker-compose up -d daemon
   ```

3. Verify logging:
   ```bash
   docker logs musicstream-daemon | grep "Downloads disabled"
   ```

### Cannot Enable Downloads

If downloads remain disabled:

1. Check `.env` file:
   ```bash
   cat .env | grep DISABLE_DOWNLOADS
   # Should be empty, unset, or =0
   ```

2. Restart daemon:
   ```bash
   docker-compose restart daemon
   ```

3. Check startup banner:
   ```bash
   docker logs musicstream-daemon | grep "STAGING"
   # Should NOT appear if downloads are enabled
   ```

## Files Modified

The following files were modified to support staging mode:

1. **`src/daemon.py`** - Added DISABLE_DOWNLOADS checks to:
   - `full_download_pipeline()` - Scheduled downloads
   - `_run_full_pipeline()` - Manual sync downloads
   - `startup_sequence()` - Startup downloads
   - `_print_startup_banner()` - Visual indicator

2. **`docker-compose.yml`** - Added DISABLE_DOWNLOADS environment variable

3. **`.env.example`** - Added DISABLE_DOWNLOADS documentation

4. **`.env.staging.active`** - Complete staging configuration template

## Security Notes

- Staging mode does not disable HTTP endpoints
- All API tokens and credentials are still active
- Spotify sync and ListenBrainz discovery still run
- Database operations continue normally
- Only the actual music download process is disabled

## Summary

Staging mode provides a safe way to develop and test the musicstream daemon without:
- Consuming bandwidth with downloads
- Modifying your production music library
- Hitting API rate limits unnecessarily
- Storing test files in production directories

Simply set `DISABLE_DOWNLOADS=1` in your environment variables, rebuild the daemon, and you're in staging mode. Remove or unset the variable to enable downloads for production use.
