#!/usr/bin/python2.7

from datetime import datetime
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import traceback

def install_packages(*packages):
    subprocess.check_call(["apt-get", "update"])
    subprocess.check_call(["apt-get", "-y", "install"] + list(packages))

def delete_packages(*packages):
    subprocess.check_call(["apt-get", "-y", "remove"] + list(packages))

def delete_apt_keys(*keyids):
    for keyid in keyids:
        subprocess.check_call(["apt-key", "del", keyid])

def add_apt_key(keyfile):
    subprocess.check_call(["apt-key", "add", keyfile])

def add_new_apt_key():
    # We want to make upgrading simple, so we should only have to copy this script to the target machines for upgrade.
    # We don't use the keyservers to download the key because hkp is blocked by the default firewall rules.
    subprocess.check_call("wget -qO - https://freedom.press/sites/default/files/fpf.asc | sudo apt-key add -", shell=True)

def backup_app():
    tar_fn = 'backup-app-{}.tar.bz2'.format(datetime.now().strftime("%Y-%m-%d--%H-%M-%S"))
    with tarfile.open(tar_fn, 'w:bz2') as t:
        t.add('/var/lib/securedrop/')
        t.add('/var/lib/tor/services/')
        t.add('/var/www/securedrop/config.py')
        t.add('/var/www/securedrop/static/i/logo.png')
    print "**  Backed up system to {} before migrating.".format(tar_fn)

def backup_mon():
    # The only thing we have to back up for the monitor server is the SSH ATHS cert.
    # All other required values are available in prod-specific.yml from the installation.
    tar_fn = 'backup-mon-{}.tar.bz2'.format(datetime.now().strftime("%Y-%m-%d--%H-%M-%S"))
    with tarfile.open(tar_fn, 'w:bz2') as t:
        t.add('/var/lib/tor/services/')
    print "**  Backed up system to {} before migrating.".format(tar_fn)

logo_path = '/var/www/securedrop/static/i/logo.png'
logo_backup_path = '/tmp/logo.png'
    
def backup_logo():
    shutil.copy(logo_path, logo_backup_path)

def restore_logo():
    shutil.copy(logo_backup_path, logo_path)
    # Apparently I don't need to set the permissions to www-data:www-data, they
    # are automatically set that way (tested).
    
def migrate_app_db():
    # To get CREATE TABLE from SQLAlchemy:
    # >>> import db
    # >>> from sqlalchemy.schema import CreateTable
    # >>> print CreateTable(db.Journalist.__table__).compile(db.engine)
    # Or, add `echo=True` to the engine constructor.
    db_path = "/var/lib/securedrop/db.sqlite"
    assert os.path.isfile(db_path)
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # CREATE TABLE replies
    c.execute("""
CREATE TABLE replies (
	id INTEGER NOT NULL,
	journalist_id INTEGER,
	source_id INTEGER,
	filename VARCHAR(255) NOT NULL,
	size INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(journalist_id) REFERENCES journalists (id),
	FOREIGN KEY(source_id) REFERENCES sources (id)
)""")

    # Fill in replies from the replies in STORE_DIR at the time of the migration
    # 
    # Caveats:
    #
    # 1. Before we added the `replies` table, we did not keep track of which
    # journalist wrote the reply. There is no way for us to reverse-engineer
    # that information, so the migration will default to saying they were all
    # created by the first journalist (arbitrarily). Since we do not surface
    # this in the UI yet anyway, it should not be a big deal.
    #
    # 2. We do not try to get the order of the (autoincrementing primary key)
    # reply_id to match the order in which the replies were created (which could
    # be inferred from the file timestamps, since we only normalize submission
    # timestamps and not reply timestamps) since this order is not used anywhere
    # in the code.

    # Copy from db.py to compute filesystem-safe journalist filenames
    def journalist_filename(s):
        valid_chars = 'abcdefghijklmnopqrstuvwxyz1234567890-_'
        return ''.join([c for c in s.lower().replace(' ', '_') if c in valid_chars])

    store_dir = "/var/lib/securedrop/store"
    reply_id = 1
    for source_dir in os.listdir(store_dir):
        try:
            source_id, journalist_designation = c.execute(
                "SELECT id, journalist_designation FROM sources WHERE filesystem_id=?",
                (source_dir,)).fetchone()
        except sqlite3.Error as e:
            print "!!\tError occurred migrating replies for source {}".format(source_dir)
            print traceback.format_exc()
            continue

        for filename in os.listdir(os.path.join(store_dir, source_dir)):
            if "-reply.gpg" not in filename:
                continue

            # Rename the reply file from 0.3pre convention to 0.3 convention
            interaction_count = filename.split('-')[0]
            new_filename = "{}-{}-reply.gpg".format(interaction_count,
                journalist_filename(journalist_designation))
            os.rename(os.path.join(store_dir, source_dir, filename),
                      os.path.join(store_dir, source_dir, new_filename))

            # need id, journalist_id, source_id, filename, size
            journalist_id = 1 # *shrug*
            full_path = os.path.join(store_dir, source_dir, new_filename)
            size = os.stat(full_path).st_size
            c.execute("INSERT INTO replies VALUES (?,?,?,?,?)",
                      (reply_id, journalist_id, source_id, new_filename, size))
            reply_id += 1 # autoincrement for next reply

    # CREATE TABLE journalist_login_attempts
    c.execute("""
CREATE TABLE journalist_login_attempt (
	id INTEGER NOT NULL,
	timestamp DATETIME,
	journalist_id INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(journalist_id) REFERENCES journalists (id)
)""")

    # ALTER TABLE journalists, add last_token column
    c.execute("""ALTER TABLE journalists ADD COLUMN last_token VARCHAR(6)""")

    # Save changes and close connection
    conn.commit()
    conn.close()


def upgrade_app():
    backup_app()
    
    # The logo.png should not be included in the package (since it is typically
    # customized by the installation admin), but it currently is. To maintain it
    # across removing securedrop-app-code and installing the new version, we
    # need to back it up before removing the old package and then restore it
    # after installing the new package.
    backup_logo()

    delete_packages("securedrop-app-code", "securedrop-grsec", "securedrop-ossec-agent")
    migrate_app_db()
    # delete the old signing key
    delete_apt_keys("BD67D096")
    # install the new signing key
    # the key is stored in a file in the same directory as this script
    add_new_apt_key()
    # install the new packages
    install_packages("securedrop-app-code", "securedrop-ossec-agent", "ossec-agent", "securedrop-grsec")
    restore_logo()

    
def upgrade_mon():
    backup_mon()
    delete_packages("securedrop-ossec-server")
    delete_apt_keys("BD67D096")
    add_new_apt_key()
    install_packages("securedrop-ossec-server", "ossec-server", "securedrop-grsec")


def main():
    if len(sys.argv) <= 1:
        print "Usage: 0.3pre_upgrade.py app|mon"
        sys.exit(1)

    server_role = sys.argv[1]
    assert server_role in ("app", "mon")

    if server_role == "app":
        upgrade_app()
    else:
        upgrade_mon()


if __name__ == "__main__":
    main()