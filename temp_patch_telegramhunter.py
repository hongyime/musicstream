with open(r'C:\telegramhunter\docker-compose.yml', 'r') as f:
    lines = f.readlines()

out_lines = []
current_service = None

for line in lines:
    if line.startswith('  redis:'):
        current_service = 'redis'
    elif line.startswith('  api:'):
        current_service = 'api'
    elif line.startswith('  worker-core:'):
        current_service = 'worker-core'
    elif line.startswith('  worker-scanners:'):
        current_service = 'worker-scanners'
    elif line.startswith('  worker-scrape:'):
        current_service = 'worker-scrape'
    elif line.startswith('  worker-validators:'):
        current_service = 'worker-validators'
    elif line.startswith('  beat:'):
        current_service = 'beat'
    elif line.startswith('  frontend:'):
        current_service = 'frontend'
    elif line.startswith('  bot:'):
        current_service = 'bot'
    elif line.startswith('volumes:'):
        current_service = None

    # Reductions
    if current_service == 'api' and '-w 2' in line:
        line = line.replace('-w 2', '-w 1')
    elif current_service == 'worker-core' and '--concurrency=4' in line:
        line = line.replace('--concurrency=4', '--concurrency=1')
    elif current_service == 'worker-scanners' and '--concurrency=2' in line:
        line = line.replace('--concurrency=2', '--concurrency=1')
    elif current_service == 'worker-validators' and '--concurrency=4' in line:
        line = line.replace('--concurrency=4', '--concurrency=1')
    elif current_service == 'frontend' and 'restart: always' in line:
        line = line.replace('restart: always', 'restart: "no"')

    out_lines.append(line)

    # Insert memory limits after logging: block (actually, insert before depends_on or environment)
    # Wait, the easiest is to insert after 'init: true' which is common to most.
    if 'init: true' in line and current_service:
        indent = line.split('init:')[0]
        limit = None
        if current_service == 'redis': limit = '64M'
        elif current_service == 'api': limit = '384M'
        elif current_service == 'worker-core': limit = '256M'
        elif current_service == 'worker-scanners': limit = '256M'
        elif current_service == 'worker-scrape': limit = '256M'
        elif current_service == 'worker-validators': limit = '256M'
        elif current_service == 'beat': limit = '192M'
        elif current_service == 'frontend': limit = '128M' # Just in case it runs
        elif current_service == 'bot': limit = '192M'
        
        if limit:
            out_lines.append(indent + 'deploy:\n')
            out_lines.append(indent + '  resources:\n')
            out_lines.append(indent + '    limits:\n')
            out_lines.append(indent + f'      memory: {limit}\n')

with open(r'C:\telegramhunter\docker-compose.yml', 'w') as f:
    f.writelines(out_lines)
