#!/bin/python
# Backups volumes of running containers

import subprocess
import os
import re
import pathlib
import pandas
from datetime import datetime

#Ok 
class BackupException(Exception):
    """Generic exception for backup errors."""
    pass

# OK
def execute_shell_command(command):
    """Execute a shell command and return its output."""
    print(command)
    process = subprocess.Popen([command], stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    out, err = process.communicate()
    if process.returncode != 0:
        raise BackupException(f"Error in command: {command}\nOutput: {out}\nError: {err}\nExit code: {process.returncode}")
    return [line.decode("utf-8") for line in out.splitlines()]

# OK
def get_machine_id():
    """Get the machine identifier."""
    return execute_shell_command("sha256sum /etc/machine-id")[0][0:64]

# OK
def create_backup_directories(base_dir, machine_id, repository_name, backup_time):
    """Create necessary directories for backup."""
    version_dir = os.path.join(base_dir, machine_id, repository_name, backup_time)
    pathlib.Path(version_dir).mkdir(parents=True, exist_ok=True)
    return version_dir

# Muss modifiziert werden. 
def get_database_name(container):
    database_name = re.split("(_|-)(database|db)", container)[0]
    print(f"Extracted database name: {database_name}")
    return database_name

def backup_database(container, databases, version_dir, db_type):
    """Backup database (MariaDB or PostgreSQL) if applicable."""
    print(f"Starting database backup for {container} using {db_type}...")
    database_name = get_database_name(container)
    database_entry = databases.loc[databases['database'] == database_name].iloc[0]
    backup_destination_dir = os.path.join(version_dir, "sql")
    pathlib.Path(backup_destination_dir).mkdir(parents=True, exist_ok=True)
    backup_destination_file = os.path.join(backup_destination_dir, f"{database_name}_backup.sql")
    
    if db_type == 'mariadb':
        backup_command = f"docker exec {container} /usr/bin/mariadb-dump -u {database_entry['username']} -p{database_entry['password']} {database_entry['database']} > {backup_destination_file}"
    elif db_type == 'postgres':
        backup_command = f"PGPASSWORD={database_entry['password']} docker exec -i {container} pg_dump -U {database_entry['username']} -d {database_entry['database']} -h localhost --no-password > {backup_destination_file}"
    
    execute_shell_command(backup_command)
    print(f"Database backup for {container} completed.")

# OK 
def backup_volume(volume_name, version_dir):
    """Backup files of a volume."""
    print(f"Starting backup routine for volume: {volume_name}")
    files_rsync_destination_path = os.path.join(version_dir, volume_name, "files")
    pathlib.Path(files_rsync_destination_path).mkdir(parents=True, exist_ok=True)
    source_dir = f"/var/lib/docker/volumes/{volume_name}/_data/"
    rsync_command = f"rsync -abP --delete --delete-excluded {source_dir} {files_rsync_destination_path}"
    execute_shell_command(rsync_command)
    print(f"Backup routine for volume: {volume_name} completed.")

# OK
def has_image(container,image):
    """Check if the container is using the image"""
    image_info = execute_shell_command(f"docker inspect {container} | jq -r '.[].Config.Image'")
    return image in image_info[0]

# OK
def stop_containers(containers):
    """Stop a list of containers."""
    for container in containers:
        print(f"Stopping container {container}...")
        execute_shell_command(f"docker stop {container}")
# OK
def start_containers(containers):
    """Start a list of stopped containers."""
    for container in containers:
        print(f"Starting container {container}...")
        execute_shell_command(f"docker start {container}")

# OK
def get_container_with_image(containers,image):
    for container in containers:
        if has_image(container,image):
            return container
    return False

def is_image_whitelisted(container, images):
    """Check if the container's image is one of the whitelisted images."""
    image_info = execute_shell_command(f"docker inspect {container} | jq -r '.[].Config.Image'")
    container_image = image_info[0]

    for image in images:
        if image in container_image:
            return True
    return False

def is_any_image_not_whitelisted(containers, images):
    """Check if any of the containers are using images that are not whitelisted."""
    return any(not is_image_whitelisted(container, images) for container in containers)

def backup_routine_for_volume(volume_name, containers, databases, version_dir, whitelisted_images):
    """Perform backup routine for a given volume."""
    for container in containers:
        if has_image(container, 'mariadb'):
            backup_database(container, databases, version_dir, 'mariadb')
        elif has_image(container, 'postgres'):
            backup_database(container, databases, version_dir, 'postgres')
        else:
            if is_any_image_not_whitelisted(containers, whitelisted_images):
                stop_containers(containers)
                backup_volume(volume_name, version_dir)
                start_containers(containers)
            else:
                backup_volume(volume_name, version_dir)

def main():
    print('Start backup routine...')
    dirname = os.path.dirname(__file__)
    repository_name = os.path.basename(dirname)
    machine_id = get_machine_id()
    backups_dir = '/Backups/'
    backup_time = datetime.now().strftime("%Y%m%d%H%M%S")
    version_dir = create_backup_directories(backups_dir, machine_id, repository_name, backup_time)

    print('Start volume backups...')
    databases = pandas.read_csv(os.path.join(dirname, "databases.csv"), sep=";")
    volume_names = execute_shell_command("docker volume ls --format '{{.Name}}'")
    
    # This whitelist is configurated for https://github.com/kevinveenbirkenbach/backup-docker-to-local 
    stop_and_restart_not_needed = [
        'baserow',
        'element',
        'gitea',
        'listmonk',
        'mastodon',
        'matomo',
        'memcached',
        'nextcloud',
        'openproject',
        'pixelfed',
        'redis',
        'wordpress' 
    ]
    
    for volume_name in volume_names:
        print(f'Start backup routine for volume: {volume_name}')
        containers = execute_shell_command(f"docker ps --filter volume=\"{volume_name}\" --format '{{.Names}}'")
        if not containers:
            print('Skipped due to no running containers using this volume.')
            continue
        
        backup_routine_for_volume(volume_name, containers, databases, version_dir, stop_and_restart_not_needed)

    print('Finished volume backups.')

if __name__ == "__main__":
    main()
