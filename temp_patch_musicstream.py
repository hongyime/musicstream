with open('docker-compose.yml', 'r') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if 'image: postgres:18.4-alpine' in line:
        lines[i] = line.replace('image: postgres:18.4-alpine', 'image: postgres:16.13-alpine')
    elif 'MAX_CONCURRENT_WORKERS=${MAX_CONCURRENT_WORKERS:-12}' in line:
        lines[i] = line.replace('12', '2')
    elif 'memory: 2G' in line:
        lines[i] = line.replace('memory: 2G', 'memory: 512M')

in_postgres = False
in_plex = False
in_scrobbler = False
out_lines = []

for line in lines:
    out_lines.append(line)
    if line.strip() == 'postgres:':
        in_postgres = True
    elif line.strip() == 'plex:':
        in_plex = True
    elif line.strip() == 'scrobbler:':
        in_scrobbler = True
    elif line.strip() == 'daemon:':
        in_postgres = False
        in_plex = False
        in_scrobbler = False
        
    if 'restart: unless-stopped' in line:
        indent = line.split('restart:')[0]
        if in_postgres:
            out_lines.append(indent + 'deploy:\n')
            out_lines.append(indent + '  resources:\n')
            out_lines.append(indent + '    limits:\n')
            out_lines.append(indent + '      memory: 128M\n')
            in_postgres = False
        elif in_plex:
            out_lines.append(indent + 'deploy:\n')
            out_lines.append(indent + '  resources:\n')
            out_lines.append(indent + '    limits:\n')
            out_lines.append(indent + '      memory: 256M\n')
            in_plex = False
        elif in_scrobbler:
            out_lines.append(indent + 'deploy:\n')
            out_lines.append(indent + '  resources:\n')
            out_lines.append(indent + '    limits:\n')
            out_lines.append(indent + '      memory: 256M\n')
            in_scrobbler = False

with open('docker-compose.yml', 'w') as f:
    f.writelines(out_lines)
