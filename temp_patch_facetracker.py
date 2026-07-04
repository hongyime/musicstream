with open(r'C:\facetracker\docker-compose.yml', 'r') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if 'memory: 4G' in line:
        lines[i] = line.replace('memory: 4G', 'memory: 1024M')
    elif 'cpus: "1.0"' in line:
        lines[i] = line.replace('cpus: "1.0"', 'cpus: "0.5"')

in_postgres = False
in_dashboard = False
out_lines = []

for line in lines:
    out_lines.append(line)
    if line.startswith('  postgres:'):
        in_postgres = True
        in_dashboard = False
    elif line.startswith('  dashboard:'):
        in_dashboard = True
        in_postgres = False
    elif line.startswith('  api:'):
        in_postgres = False
        in_dashboard = False
    elif line.startswith('networks:'):
        in_dashboard = False
        in_postgres = False
        
    if 'restart: unless-stopped' in line and (in_postgres or in_dashboard):
        indent = line.split('restart:')[0]
        out_lines.append(indent + 'deploy:\n')
        out_lines.append(indent + '  resources:\n')
        out_lines.append(indent + '    limits:\n')
        out_lines.append(indent + '      memory: 128M\n')

with open(r'C:\facetracker\docker-compose.yml', 'w') as f:
    f.writelines(out_lines)
