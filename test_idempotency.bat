@echo off
echo ========================================
echo Re-run Safety Test Suite
echo ========================================
echo.

REM Test 1: setup.bat idempotency
echo Test 1: Running setup.bat twice...
call setup.bat
echo.
echo First run complete, running second time...
call setup.bat
echo Result: Should complete without errors and not recreate venv
echo.

REM Test 2: downloader idempotency
echo Test 2: Testing file existence check...
cd ..
if not " %~1 "=="" (
    set "TRACK_ID=%~1"
) else (
    set "TRACK_ID=dQw4w9WgXcQ"  REM Default test video
)

if not " %~2 " neq "" (
    set "OUTPUT_DIR=%~2"
) else (
    set "OUTPUT_DIR=test_downloads"
)

echo Testing URL: https://www.youtube.com/watch?v= %TRACK_ID%
echo Output directory: %OUTPUT_DIR%
echo.

REM First run
echo First download attempt...
python -c "
from downloader import AudioExtractor
import os, sys

# Test track
tracks = [{
    'video_id': 'dQw4w9WgXcQ',
    'track_name': 'Test Track',
    'artist_name': 'Test Artist',
    'album_name': 'Test Album'
}]

downloader = AudioExtractor('test_downloads')
success_count = 0

for track in tracks:
    result = downloader.extract_audio(
        track['video_id'],
        track['track_name'],
        track['artist_name'],
        album_name=track['album_name'],
        force=False
    )
    if result:
        success_count += 1
        print(f'First run: Downloaded {result}')
    else:
        print('First run: Download failed')

print(f'First run complete: {success_count}/{len(tracks)} succeeded')
"

if %errorlevel% neq 0 (
    echo ERROR in first download attempt
    exit /b 1
)

echo.
echo Second run (should skip existing file)...
python -c "
from downloader import AudioExtractor
import os, sys

# Test track
tracks = [{
    'video_id': 'dQw4w9WgXcQ',
    'track_name': 'Test Track',
    'artist_name': 'Test Artist',
    'album_name': 'Test Album'
}]

downloader = AudioExtractor('test_downloads')
skipped_count = 0

def capture_logs():
    import logging
    logs = []
    class LogCapture(logging.Handler):
        def emit(self, record):
            logs.append(self.format(record))
    handler = LogCapture()
    handler.setLevel(logging.INFO)
    logging.getLogger('downloader').addHandler(handler)
    return logs

logs = capture_logs()

for track in tracks:
    result = downloader.extract_audio(
        track['video_id'],
        track['track_name'],
        track['artist_name'],
        album_name=track['album_name'],
        force=False
    )
    if result:
        # Check logs to see if it was skipped
        for log in logs:
            if 'already exists' in log.lower():
                skipped_count += 1
                print('Second run: File correctly skipped (already exists)')
                break
        else:
            print('Second run: WARNING - File was re-downloaded despite existing!')

print(f'Second run complete: {skipped_count}/{len(tracks)} correctly skipped')
if skipped_count == len(tracks):
    print('SUCCESS: Idempotency working correctly!')
else:
    print('FAILURE: Files were re-downloaded')
    sys.exit(1)
"

echo.
echo Test 3: Force parameter test...
python -c "
from downloader import AudioExtractor
downloader = AudioExtractor('test_downloads')
result = downloader.extract_audio(
    'dQw4w9WgXcQ',
    'Test Track',
    'Test Artist',
    album_name='Test Album',
    force=True
)
if result:
    print('Force=True: File downloaded/re-downloaded correctly')
else:
    print('Force=True: Download failed')
"

echo.
echo ========================================
echo All tests completed!
echo ========================================

echo Cleaning up test data...
if exist "test_downloads" rmdir /s /q test_downloads
echo Done.

pause
