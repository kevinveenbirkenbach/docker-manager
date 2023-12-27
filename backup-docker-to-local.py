#!/bin/python
# Backups volumes of running containers

import subprocess
import os
import re
import pathlib
import pandas
from datetime import datetime
import argparse

class BackupException(Exception):
    """Generic exception for backup errors."""
    pass

def execute_shell_command(command):
    """Execute a shell command and return its output."""
    print(command)
    process = subprocess.Popen([command], stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    out, err = process.communicate()
    if process.returncode != 0:
        raise BackupException(f"Error in command: {command}\nOutput: {out}\nError: {err}\nExit code: {process.returncode}")
    return [line.decode("utf-8") for line in out.splitlines()]

def get_machine_id():
    """Get the machine identifier."""
    return execute_shell_command("sha256sum /etc/machine-id")[0][0:64]

def create_version_directory(versions_dir, backup_time):
    """Create necessary directories for backup."""
    version_dir = os.path.join(versions_dir, backup_time)
    pathlib.Path(version_dir).mkdir(parents=True, exist_ok=True)
    return version_dir

def get_instance(container):
    instance_name = re.split("(_|-)(database|db|postgres)", container)[0]
    print(f"Extracted instance name: {instance_name}")
    return instance_name

def backup_database(container, databases, volume_dir, db_type):
    """Backup database (MariaDB or PostgreSQL) if applicable."""
    print(f"Starting database backup for {container} using {db_type}...")
    instance_name = get_instance(container)

    # Filter the DataFrame for the given instance_name
    database_entries = databases.loc[databases['instance'] == instance_name]

    # Check if there are more than one entries
    if len(database_entries) > 1:
        raise BackupException(f"More than one entry found for instance '{instance_name}'")

    # Check if there is no entry
    if database_entries.empty:
        raise BackupException(f"No entry found for instance '{instance_name}'")

    # Get the first (and only) entry
    database_entry = database_entries.iloc[0]

    backup_destination_dir = os.path.join(volume_dir, "sql")
    pathlib.Path(backup_destination_dir).mkdir(parents=True, exist_ok=True)
    backup_destination_file = os.path.join(backup_destination_dir, f"backup.sql")
    
    if db_type == 'mariadb':
        backup_command = f"docker exec {container} /usr/bin/mariadb-dump -u {database_entry['username']} -p{database_entry['password']} {database_entry['database']} > {backup_destination_file}"
    elif db_type == 'postgres':
        if database_entry['password']:
            # Include PGPASSWORD in the command when a password is provided
            backup_command = (
                f"PGPASSWORD={database_entry['password']} docker exec -i {container} "
                f"pg_dump -U {database_entry['username']} -d {database_entry['database']} "
                f"-h localhost > {backup_destination_file}"
            )
        else:
            # Exclude PGPASSWORD and use --no-password when the password is empty
            backup_command = (
                f"docker exec -i {container} pg_dump -U {database_entry['username']} "
                f"-d {database_entry['database']} -h localhost --no-password "
                f"> {backup_destination_file}"
            )

    execute_shell_command(backup_command)
    print(f"Database backup for {container} completed.")

def get_last_backup_dir(versions_dir, volume_name, current_backup_dir):
    """Get the most recent backup directory for the specified volume."""
    versions = sorted(os.listdir(versions_dir), reverse=True)
    for version in versions:
        backup_dir = os.path.join(versions_dir, version, volume_name, "files")
        # Ignore current backup dir
        if backup_dir != current_backup_dir:
            if os.path.isdir(backup_dir):
                return backup_dir
    print(f"No previous backups available for volume: {volume_name}")
    return None

def backup_volume(volume_name, volume_dir, versions_dir):
    """Backup files of a volume with incremental backups."""
    print(f"Starting backup routine for volume: {volume_name}")
    files_rsync_destination_path = os.path.join(volume_dir, "files")
    pathlib.Path(files_rsync_destination_path).mkdir(parents=True, exist_ok=True)

    last_backup_dir = get_last_backup_dir(versions_dir, volume_name, files_rsync_destination_path)
    link_dest_option = f"--link-dest='{last_backup_dir}'" if last_backup_dir else ""

    source_dir = f"/var/lib/docker/volumes/{volume_name}/_data/"
    rsync_command = f"rsync -abP --delete --delete-excluded {link_dest_option} {source_dir} {files_rsync_destination_path}"
    execute_shell_command(rsync_command)
    print(f"Backup routine for volume: {volume_name} completed.")

def get_image_info(container):
    return execute_shell_command(f"docker inspect {container} | jq -r '.[].Config.Image'")

def has_image(container,image):
    """Check if the container is using the image"""
    image_info = get_image_info(container)
    return image in image_info[0]

def stop_containers(containers):
    """Stop a list of containers."""
    for container in containers:
        print(f"Stopping container {container}...")
        execute_shell_command(f"docker stop {container}")

def start_containers(containers):
    """Start a list of stopped containers."""
    for container in containers:
        print(f"Starting container {container}...")
        execute_shell_command(f"docker start {container}")

def get_container_with_image(containers,image):
    for container in containers:
        if has_image(container,image):
            return container
    return False

def is_image_whitelisted(container, images):
    """Check if the container's image is one of the whitelisted images."""
    image_info = get_image_info(container)
    container_image = image_info[0]

    for image in images:
        if image in container_image:
            return True
    return False

def is_any_image_not_whitelisted(containers, images):
    """Check if any of the containers are using images that are not whitelisted."""
    return any(not is_image_whitelisted(container, images) for container in containers)

def create_volume_directory(version_dir,volume_name):
    """Create necessary directories for backup."""
    volume_dir = os.path.join(version_dir, volume_name)
    pathlib.Path(volume_dir).mkdir(parents=True, exist_ok=True)
    return volume_dir

def is_image_ignored(container, ignored_images):
    """Check if the container's image is one of the ignored images."""
    for image in ignored_images:
        if has_image(container, image):
            return True
    return False

def backup_with_containers_paused(volume_name, volume_dir, versions_dir, containers):
    stop_containers(containers)
    backup_volume(volume_name, volume_dir, versions_dir)
    start_containers(containers)

def default_backup_routine_for_volume(volume_name, containers, databases, version_dir, whitelisted_images, versions_dir):
    """Perform backup routine for a given volume."""
    volume_dir=""
    for container in containers:
        
        # Skip ignored images
        if is_image_ignored(container, ['redis', 'memcached']):
            print(f"Ignoring volume '{volume_name}' linked to container '{container}' with ignored image.")
            continue 

        # Directory which contains files and sqls
        volume_dir = create_volume_directory(version_dir, volume_name)
        
        # Execute MariaDB procedure
        if has_image(container, 'mariadb'):
            backup_database(container, databases, volume_dir, 'mariadb')
            return

        # Execute Postgres procedure
        if has_image(container, 'postgres'):
            backup_database(container, databases, volume_dir, 'postgres')
            return

    # Execute backup if image is not ignored
    if volume_dir:    
        backup_volume(volume_name, volume_dir, versions_dir)
        if is_any_image_not_whitelisted(containers, whitelisted_images):
            backup_with_containers_paused(volume_name, volume_dir, versions_dir, containers)

def force_file_backup_routine_for_volume(volume_name, containers, version_dir, versions_dir):
    """Perform file backup routine for a given volume."""
    volume_dir=create_volume_directory(version_dir, volume_name)
    backup_volume(volume_name, volume_dir, versions_dir)
    backup_with_containers_paused(volume_name, volume_dir, versions_dir, containers)

def main():
    parser = argparse.ArgumentParser(description='Backup Docker volumes.')
    parser.add_argument('--force-file-backup', action='store_true',
                        help='Force file backup for all volumes, ignoring whitelists and database checks.')
    args = parser.parse_args()
    print('Start backup routine...')
    dirname = os.path.dirname(__file__)
    repository_name = os.path.basename(dirname)
    machine_id = get_machine_id()
    backups_dir = '/Backups/'
    versions_dir = os.path.join(backups_dir, machine_id, repository_name)
    backup_time = datetime.now().strftime("%Y%m%d%H%M%S")
    version_dir = create_version_directory(versions_dir, backup_time)

    print('Start volume backups...')
    databases = pandas.read_csv(os.path.join(dirname, "databases.csv"), sep=";")
    volume_names = execute_shell_command("docker volume ls --format '{{.Name}}'")
    
    # This whitelist is configurated for https://github.com/kevinveenbirkenbach/backup-docker-to-local 
    stop_and_restart_not_needed = [
        # 'baserow', Doesn't use an extra database
        'element',
        'gitea',
        'listmonk',
        'mastodon',
        'matomo',
        'nextcloud',
        'openproject',
        'pixelfed',
        'redis',
        'wordpress' 
    ]
    
    for volume_name in volume_names:
        print(f'Start backup routine for volume: {volume_name}')
        containers = execute_shell_command(f"docker ps --filter volume=\"{volume_name}\" --format '{{{{.Names}}}}'")
        if not containers:
            print('Skipped due to no running containers using this volume.')
            continue
        if args.force_file_backup:
            force_file_backup_routine_for_volume(volume_name, containers, version_dir, versions_dir)
        else:    
            default_backup_routine_for_volume(volume_name, containers, databases, version_dir, stop_and_restart_not_needed, versions_dir)

    print('Finished volume backups.')

if __name__ == "__main__":
    main()
