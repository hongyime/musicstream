with open(r'C:\pihole\docker-compose.yml', 'r') as f:
    lines = f.readlines()

out_lines = []
for line in lines:
    if 'restart: always    deploy:      resources:        limits:          memory: 256M' in line:
        indent = line.split('restart:')[0]
        out_lines.append(indent + 'restart: always\n')
        out_lines.append(indent + 'deploy:\n')
        out_lines.append(indent + '  resources:\n')
        out_lines.append(indent + '    limits:\n')
        out_lines.append(indent + '      memory: 256M\n')
    else:
        out_lines.append(line)

with open(r'C:\pihole\docker-compose.yml', 'w') as f:
    f.writelines(out_lines)
