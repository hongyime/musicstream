#!/usr/bin/env python3
from spotdl import Spotdl
import inspect
import asyncio

client = Spotdl(
    client_id='test',
    client_secret='test',
    downloader_settings={'output': '/test', 'format': 'mp3', 'bitrate': '320k', 'overwrite': 'force', 'log_level': 'ERROR'}
)

print('search type: ', type(client.search))
print('download type:', type(client.download))
print('search is coroutine function?:', inspect.iscoroutinefunction(client.search))
print('download is coroutine function?:', inspect.iscoroutinefunction(client.download))

# Check the actual return type
print('\nCalling search()...')
songs = client.search(['test'])
print('search() returned:', type(songs), songs if isinstance(songs, list) else '...')

# Check if download() returns a coroutine
print('\nChecking download() signature...')
from spotdl.types.song import Song
if songs:
    result = client.download(songs[0])
    print('download() returned:', type(result))
    print('Is coroutine?', inspect.iscoroutine(result))
