#!/usr/bin/env python3
# Collection of utility functions
import datetime
import os
import sys
import logging
import fcntl
import subprocess
import shutil
import time
import random
import re
import requests
import apprise
import psutil

from netifaces import interfaces, ifaddresses, AF_INET
from arm.config.config import cfg
from arm.ripper import apprise_bulk
from arm.ui import app, db
import arm.models.models as m

NOTIFY_TITLE = "ARM notification"


def notify(job, title, body):
    """
    Send notifications with apprise
    :param job: Current Job
    :param title: title for notification
    :param body: body of the notification
    :return: None
    """

    # Prepend Site Name if configured, append Job ID if configured
    if cfg["ARM_NAME"] != "":
        title = f"[{cfg['ARM_NAME']}] - {title}"
    if cfg["NOTIFY_JOBID"]:
        title = f"{title} - {job.job_id}"

    # Create an Apprise instance
    apobj = apprise.Apprise()
    if cfg["PB_KEY"] != "":
        apobj.add('pbul://' + str(cfg["PB_KEY"]))
    if cfg["IFTTT_KEY"] != "":
        apobj.add('ifttt://' + str(cfg["IFTTT_KEY"]) + "@" + str(cfg["IFTTT_EVENT"]))
    if cfg["PO_USER_KEY"] != "":
        apobj.add('pover://' + str(cfg["PO_USER_KEY"]) + "@" + str(cfg["PO_APP_KEY"]))
    if cfg["JSON_URL"] != "":
        apobj.add(str(cfg["JSON_URL"]).replace("http://", "json://").replace("https://", "jsons://"))
    try:
        apobj.notify(body, title=title)
    except Exception as error:  # noqa: E722
        logging.error(f"Failed sending notifications. error:{error}. Continuing processing...")

    if cfg["APPRISE"] != "":
        try:
            apprise_bulk.apprise_notify(cfg["APPRISE"], title, body)
            logging.debug(f"apprise-config: {cfg['APPRISE']}")
        except Exception as error:  # noqa: E722
            logging.error(f"Failed sending apprise notifications. {error}")


def notify_entry(job):
    """
    Notify On Entry
    :param job:
    :return:
    """
    # TODO make this better or merge with notify/class
    if job.disctype in ["dvd", "bluray"]:
        # Send the notifications
        notify(job, NOTIFY_TITLE,
               f"Found disc: {job.title}. Disc type is {job.disctype}. Main Feature is {cfg['MAINFEATURE']}"
               f".  Edit entry here: http://{check_ip()}:"
               f"{cfg['WEBSERVER_PORT']}/jobdetail?job_id={job.job_id}")
    elif job.disctype == "music":
        notify(job, NOTIFY_TITLE, f"Found music CD: {job.label}. Ripping all tracks")
    elif job.disctype == "data":
        notify(job, NOTIFY_TITLE, "Found data disc.  Copying data.")
    else:
        notify(job, NOTIFY_TITLE, "Could not identify disc.  Exiting.")
        sys.exit()


def scan_emby():
    """Trigger a media scan on Emby"""

    if cfg["EMBY_REFRESH"]:
        logging.info("Sending Emby library scan request")
        url = f"http://{cfg['EMBY_SERVER']}:{cfg['EMBY_PORT']}/Library/Refresh?api_key={cfg['EMBY_API_KEY']}"
        try:
            req = requests.post(url)
            if req.status_code > 299:
                req.raise_for_status()
            logging.info("Emby Library Scan request successful")
        except requests.exceptions.HTTPError:
            logging.error(f"Emby Library Scan request failed with status code: {req.status_code}")
    else:
        logging.info("EMBY_REFRESH config parameter is false.  Skipping emby scan.")


def sleep_check_process(process_str, transcode_limit):
    """
    New function to check for max_transcode from cfg file and force obey limits\n
    :param process_str: The process string from arm.yaml
    :param transcode_limit: The user defined limit for maximum transcodes
    :return: Bool - when we have space in the transcode queue
    """
    if transcode_limit > 0:
        loop_count = transcode_limit + 1
        logging.debug(f"loop_count {loop_count}")
        logging.info(f"Starting A sleep check of {process_str}")
        while loop_count >= transcode_limit:
            loop_count = sum(1 for proc in psutil.process_iter() if proc.name() == process_str)
            logging.debug(f"Number of Processes running is: "
                          f"{loop_count} going to waiting 12 seconds.")
            if transcode_limit > loop_count:
                return True
            # Try to make each check at different times
            random_time = random.randrange(20, 120, 10)
            logging.debug(f"sleeping for {random_time} seconds")
            time.sleep(random_time)
    else:
        logging.info("Transcode limit is disabled")
    return False


def convert_job_type(video_type):
    """
    Converts the job_type to the correct folder
    :param video_type: job.video_type
    :return: string of the correct folder
    """
    if video_type == "movie":
        type_sub_folder = "movies"
    elif video_type == "series":
        type_sub_folder = "tv"
    else:
        type_sub_folder = "unidentified"
    return type_sub_folder


def fix_job_title(job):
    """
    Validate the job title remove/add job year as needed
    :param job:
    :return: correct job.title
    """
    if job.year and job.year != "0000" and job.year != "":
        job_title = f"{job.title} ({job.year})"
    else:
        job_title = f"{job.title}"
    return job_title


def move_files(base_path, filename, job, ismainfeature=False):
    """
    Move files from RAW_PATH or TRANSCODE_PATH to final media directory\n\n
    :param base_path: Path to source directory\n
    :param filename: name of file to be moved\n
    :param job: instance of Job class\n
    :param ismainfeature: True/False
    :return: None
    """
    video_title = fix_job_title(job)
    type_sub_folder = convert_job_type(job.video_type)

    logging.debug(f"Arguments: {base_path} : {filename} : "
                  f"{job.hasnicetitle} : {video_title} : {ismainfeature}")

    movie_path = os.path.join(cfg["COMPLETED_PATH"], str(type_sub_folder), video_title)
    logging.info(f"Moving {job.video_type} {filename} to {movie_path}")
    # For series there are no extras so always use the base path
    extras_path = os.path.join(movie_path, cfg["EXTRAS_SUB"]) if job.video_type != "series" else movie_path
    make_dir(movie_path)

    if ismainfeature is True:
        movie_file = os.path.join(movie_path, video_title + "." + cfg["DEST_EXT"])
        logging.info(f"Track is the Main Title.  Moving '{filename}' to {movie_file}")
        if not os.path.isfile(movie_file):
            try:
                shutil.move(os.path.join(base_path, filename), movie_file)
            except Exception as error:
                logging.error(f"Unable to move '{filename}' to '{movie_path}' - Error: {error}")
        else:
            logging.info(f"File: {movie_file} already exists.  Not moving.")
    else:
        make_dir(extras_path)
        logging.info(f"Moving '{filename}' to {extras_path}")
        extras_file = os.path.join(extras_path, video_title + "." + cfg["DEST_EXT"])
        if not os.path.isfile(extras_file):
            try:
                shutil.move(os.path.join(base_path, filename), os.path.join(extras_path, filename))
            except Exception as error:
                logging.error(f"Unable to move '{filename}' to {extras_path} - {error}")
        else:
            logging.info(f"File: {extras_file} already exists.  Not moving.")
    return movie_path


def make_dir(path):
    """
    Make a directory\n
    path = Path to directory\n

    returns success True if successful
        false if the directory already exists
    """
    if not os.path.exists(path):
        logging.debug(f"Creating directory: {path}")
        try:
            os.makedirs(path)
            return True
        except OSError:
            err = f"Couldn't create a directory at path: {path} Probably a permissions error.  Exiting"
            logging.error(err)
            # TODO set job to fail and commit to db
            sys.exit(err)
    else:
        return False


def get_cdrom_status(devpath):
    """get the status of the cdrom drive\n
    devpath = path to cdrom\n

    returns int
    CDS_NO_INFO		0\n
    CDS_NO_DISC		1\n
    CDS_TRAY_OPEN		2\n
    CDS_DRIVE_NOT_READY	3\n
    CDS_DISC_OK		4\n

    see linux/cdrom.h for specifics\n
    """

    try:
        fd = os.open(devpath, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        # Sometimes ARM will log errors opening hard drives. this check should stop it
        if not re.search(r'hd[a-j]|sd[a-j]|loop[0-9]', devpath):
            logging.info(f"Failed to open device {devpath} to check status.")
        sys.exit(2)
    result = fcntl.ioctl(fd, 0x5326, 0)

    return result


def find_file(filename, search_path):
    """
    Check to see if file exists by searching a directory recursively\n
    filename = filename to look for\n
    search_path = path to search recursively\n

    returns True or False
    """

    for dirpath, dirnames, filenames in os.walk(search_path):
        if filename in filenames:
            return True
    return False


def find_largest_file(files, mkv_out_path):
    """
    Step through given dir and return the largest file name
    :param files: dir in os.listdir() format
    :param mkv_out_path: RAW_PATH
    :return: largest file name
    """
    largest_file_name = ""
    for file in files:
        # initialize largest_file_name
        if largest_file_name == "":
            largest_file_name = file
        temp_path_f = os.path.join(mkv_out_path, file)
        temp_path_largest = os.path.join(mkv_out_path, largest_file_name)
        if os.stat(temp_path_f).st_size > os.stat(temp_path_largest).st_size:
            largest_file_name = file
    return largest_file_name


def rip_music(job, logfile):
    """
    Rip music CD using abcde config\n
    :param job: job object
    :param logfile: location of logfile\n
    :return: Bool on success or fail
    """
    """
    Rip music CD using abcde using abcde config\n
    job = job object\n
    logfile = location of logfile\n

    returns True/False for success/fail
    """

    abcfile = cfg["ABCDE_CONFIG_FILE"]
    if job.disctype == "music":
        logging.info("Disc identified as music")
        # If user has set a cfg file with ARM use it
        if os.path.isfile(abcfile):
            cmd = f'abcde -d "{job.devpath}" -c {abcfile} >> "{logfile}" 2>&1'
        else:
            cmd = f'abcde -d "{job.devpath}" >> "{logfile}" 2>&1'

        logging.debug(f"Sending command: {cmd}")

        try:
            # TODO check output and confirm all tracks ripped; find "Finished\.$"
            subprocess.check_output(cmd, shell=True).decode("utf-8")
            logging.info("abcde call successful")
            return True
        except subprocess.CalledProcessError as ab_error:
            err = f"Call to abcde failed with code: {ab_error.returncode} ({ab_error.output})"
            logging.error(err)
    return False


def rip_data(job):
    """
    Rip data disc using dd on the command line\n
    :param job: Current job
    :return: True/False for success/fail
    """
    success = False
    if job.label == "" or job.label is None:
        job.label = "data-disc"
    # get filesystem in order
    raw_path = os.path.join(cfg["RAW_PATH"], str(job.label))
    final_path = os.path.join(cfg["COMPLETED_PATH"], convert_job_type(job.video_type))
    final_file_name = str(job.label)

    if (make_dir(raw_path)) is False:
        random_time = str(round(time.time() * 100))
        raw_path = os.path.join(cfg["RAW_PATH"], str(job.label) + "_" + random_time)
        final_file_name = f"{job.label}_{random_time}"
        if (make_dir(raw_path)) is False:
            logging.info(f"Could not create data directory: {raw_path}  Exiting ARM. ")
            sys.exit()

    final_path = os.path.join(final_path, final_file_name)
    incomplete_filename = os.path.join(raw_path, str(job.label) + ".part")
    make_dir(final_path)
    logging.info(f"Ripping data disc to: {incomplete_filename}")
    # Added from pull 366
    cmd = f'dd if="{job.devpath}" of="{incomplete_filename}" {cfg["DATA_RIP_PARAMETERS"]} 2>> {job.logfile}'
    logging.debug(f"Sending command: {cmd}")
    try:
        subprocess.check_output(cmd, shell=True).decode("utf-8")
        full_final_file = os.path.join(final_path, f"{str(job.label)}.iso")
        logging.info(f"Moving data-disc from '{incomplete_filename}' to '{full_final_file}'")
        os.rename(incomplete_filename, full_final_file)
        logging.info("Data rip call successful")
        success = True
    except subprocess.CalledProcessError as dd_error:
        err = f"Data rip failed with code: {dd_error.returncode}({dd_error.output})"
        logging.error(err)
        os.unlink(incomplete_filename)
        args = {'status': 'fail', 'errors': err}
        database_updater(args, job)
        success = False
    try:
        logging.info(f"Trying to remove raw_path: '{raw_path}'")
        shutil.rmtree(raw_path)
    except OSError as error:
        logging.error(f"Error: {error.filename} - {error.strerror}.")
    return success


def set_permissions(job, directory_to_traverse):
    """

    :param job: job object
    :param directory_to_traverse: directory to fix permissions
    :return: Bool if fails
    """
    if not cfg['SET_MEDIA_PERMISSIONS']:
        return False
    try:
        corrected_chmod_value = int(str(cfg["CHMOD_VALUE"]), 8)
        logging.info(f"Setting permissions to: {cfg['CHMOD_VALUE']} on: {directory_to_traverse}")
        os.chmod(directory_to_traverse, corrected_chmod_value)
        if job.config.SET_MEDIA_OWNER and job.config.CHOWN_USER and job.config.CHOWN_GROUP:
            import pwd
            import grp
            uid = pwd.getpwnam(job.config.CHOWN_USER).pw_uid
            gid = grp.getgrnam(job.config.CHOWN_GROUP).gr_gid
            os.chown(directory_to_traverse, uid, gid)

        for dirpath, l_directories, l_files in os.walk(directory_to_traverse):
            for cur_dir in l_directories:
                logging.debug(f"Setting path: {cur_dir} to permissions value: {cfg['CHMOD_VALUE']}")
                os.chmod(os.path.join(dirpath, cur_dir), corrected_chmod_value)
                if job.config.SET_MEDIA_OWNER:
                    os.chown(os.path.join(dirpath, cur_dir), uid, gid)
            for cur_file in l_files:
                logging.debug(f"Setting file: {cur_file} to permissions value: {cfg['CHMOD_VALUE']}")
                os.chmod(os.path.join(dirpath, cur_file), corrected_chmod_value)
                if job.config.SET_MEDIA_OWNER:
                    os.chown(os.path.join(dirpath, cur_file), uid, gid)
        logging.info("Permissions set successfully: True")
    except Exception as error:
        logging.error(f"Permissions setting failed as: {error}")
    return True


def check_db_version(install_path, db_file):
    """
    Check if db exists and is up to date.
    If it doesn't exist create it.  If it's out of date update it.
    """
    from alembic.script import ScriptDirectory
    from alembic.config import Config
    import sqlite3
    import flask_migrate

    mig_dir = os.path.join(install_path, "arm/migrations")

    config = Config()
    config.set_main_option("script_location", mig_dir)
    script = ScriptDirectory.from_config(config)

    # create db file if it doesn't exist
    if not os.path.isfile(db_file):
        logging.info("No database found.  Initializing arm.db...")
        make_dir(os.path.dirname(db_file))
        with app.app_context():
            flask_migrate.upgrade(mig_dir)

        if not os.path.isfile(db_file):
            logging.error("Can't create database file.  "
                          "This could be a permissions issue.  Exiting...")
            sys.exit()

    # check to see if db is at current revision
    head_revision = script.get_current_head()
    logging.debug(f"Head is: {head_revision}")

    conn = sqlite3.connect(db_file)
    c = conn.cursor()

    c.execute("SELECT version_num FROM alembic_version")
    db_version = c.fetchone()[0]
    logging.debug(f"Database version is: {db_version}")
    if head_revision == db_version:
        logging.info("Database is up to date")
    else:
        logging.info(
            f"Database out of date. Head is {head_revision} and "
            f"database is {db_version}. Upgrading database...")
        with app.app_context():
            random_time = round(time.time() * 100)
            logging.info(f"Backuping up database '{db_file}' to '{db_file}_{random_time}'.")
            shutil.copy(db_file, db_file + "_" + str(random_time))
            flask_migrate.upgrade(mig_dir)
        logging.info("Upgrade complete.  Validating version level...")

        c.execute("SELECT version_num FROM alembic_version")
        db_version = c.fetchone()[0]
        logging.debug(f"Database version is: {db_version}")
        if head_revision == db_version:
            logging.info("Database is now up to date")
        else:
            logging.error(
                f"Database is still out of date. Head is {head_revision} and "
                f"database is {db_version}. Exiting arm.")
            sys.exit()


def put_track(job, t_no, seconds, aspect, fps, mainfeature, source, filename=""):
    """
    Put data into a track instance\n

    :param job: instance of job class\n
    :param str t_no: track number\n
    :param int seconds: length of track in seconds\n
    :param str aspect: aspect ratio (ie '16:9')\n
    :param str fps: frames per second:str (-not a float-)\n
    :param bool mainfeature: user only wants mainfeature \n
    :param str source: Source of information\n
    :param str filename: filename of track\n
    """

    logging.debug(
        f"Track #{int(t_no):02} Length: {seconds: >4} fps: {float(fps):2.3f} "
        f"aspect: {aspect: >4} Mainfeature: {mainfeature} Source: {source}")

    job_track = m.Track(
        job_id=job.job_id,
        track_number=t_no,
        length=seconds,
        aspect_ratio=aspect,
        fps=fps,
        main_feature=mainfeature,
        source=source,
        basename=job.title,
        filename=filename
    )
    job_track.ripped = (seconds > int(cfg['MINLENGTH']))
    # TODO add the db adder or updater here
    db.session.add(job_track)
    db.session.commit()


def arm_setup():
    """
    Setup arm - make sure everything is fully setup and ready and there are no errors.

    :arguments: None
    :return: None
    """
    arm_directories = [cfg['RAW_PATH'], cfg['TRANSCODE_PATH'],
                       cfg['COMPLETED_PATH'], cfg['LOGPATH']]
    try:
        for folder in arm_directories:
            if make_dir(folder):
                logging.error(f"Cant creat folder: {folder}")
    except IOError as error:
        logging.error(f"A fatal error has occurred. "
                      f"Cant find/create the folders from arm.yaml - Error:{error}")


def database_updater(args, job, wait_time=90):
    """
    Try to update our db for x seconds and handle it nicely if we cant

    :param args: This needs to be a Dict with the key being the job.
    Method you want to change and the value being
    the new value. If args isn't a dict assume we are wanting a rollback
    :param job: This is the job object
    :param wait_time: The time to wait in seconds
    :return: Success
    """
    if not isinstance(args, dict):
        db.session.rollback()
        return False
    # Loop through our args and try to set any of our job variables
    for (key, value) in args.items():
        setattr(job, key, value)
        logging.debug(f"{key}={value}:{type(value)}")

    for i in range(wait_time):  # give up after the users wait period in seconds
        try:
            db.session.commit()
        except Exception as error:
            if "locked" in str(error):
                time.sleep(1)
                logging.debug(f"database is locked - try {i}/{wait_time}")
            else:
                logging.debug(f"Error: {error}")
                raise RuntimeError(str(error)) from error
    logging.debug("successfully written to the database")
    return True


def database_adder(obj_class):
    """
    Used to stop database locked error
    :param obj_class: Job/Config/Track/ etc
    :return: True if success
    """
    for i in range(90):  # give up after the users wait period in seconds
        try:
            logging.debug(f"Trying to add {type(obj_class).__name__}")
            db.session.add(obj_class)
            db.session.commit()
            break
        except Exception as error:
            if "locked" in str(error):
                time.sleep(1)
                logging.debug(f"database is locked - try {i}/90")
            else:
                logging.error(f"Error: {error}")
                raise RuntimeError(str(error)) from error
    logging.debug(f"successfully written {type(obj_class).__name__} to the database")
    return True


def clean_old_jobs():
    """
    Check for running jobs, update failed jobs that are no longer running
    :return: None
    """
    active_jobs = db.session.query(m.Job).filter(m.Job.status.notin_(['fail', 'success'])).all()
    # Clean up abandoned jobs
    for job in active_jobs:
        if psutil.pid_exists(job.pid):
            job_process = psutil.Process(job.pid)
            if job.pid_hash == hash(job_process):
                logging.info(f"Job #{job.job_id} with PID {job.pid} is currently running.")
        else:
            logging.info(f"Job #{job.job_id} with PID {job.pid} has been abandoned."
                         f"Updating job status to fail.")
            job.status = "fail"
            db.session.commit()


def job_dupe_check(job):
    """
    function for checking the database to look for jobs that have completed
    successfully with the same crc
    :param job: The job obj so we can use the crc/title etc
    :return: True/False, dict/None
    """
    if job.crc_id is None:
        return False, None
    logging.debug(f"trying to find jobs with crc64={job.crc_id}")
    previous_rips = m.Job.query.filter_by(crc_id=job.crc_id, status="success", hasnicetitle=True)
    results = {}
    i = 0
    for j in previous_rips:
        logging.debug(f"job obj= {j.get_d()}")
        job_dict = j.get_d().items()
        results[i] = {}
        for key, value in iter(job_dict):
            results[i][str(key)] = str(value)
        i += 1

    logging.debug(f"previous rips = {results}")
    if results:
        logging.debug(f"we have {len(results)} jobs")
        # This might need some tweaks to because of title/year manual
        title = results[0]['title'] if results[0]['title'] else job.label
        year = results[0]['year'] if results[0]['year'] != "" else ""
        poster_url = results[0]['poster_url'] if results[0]['poster_url'] != "" else None
        hasnicetitle = (str(results[0]['hasnicetitle']).lower() == 'true')
        video_type = results[0]['video_type'] if results[0]['hasnicetitle'] != "" else "unknown"
        active_rip = {
            "title": title, "year": year, "poster_url": poster_url, "hasnicetitle": hasnicetitle,
            "video_type": video_type}
        database_updater(active_rip, job)
        return True, results

    logging.debug("We have no previous rips/jobs matching this crc64")
    return False, None


def check_ip():
    """
        Check if user has set an ip in the config file
        if not gets the most likely ip
        arguments:
        none
        return: the ip of the host or 127.0.0.1
    """
    if cfg['WEBSERVER_IP'] != 'x.x.x.x':
        return cfg['WEBSERVER_IP']
    # autodetect host IP address
    ip_list = []
    for interface in interfaces():
        inet_links = ifaddresses(interface).get(AF_INET, [])
        for link in inet_links:
            ip_address = link['addr']
            if ip_address != '127.0.0.1' and not ip_address.startswith('172'):
                ip_list.append(ip_address)
    if len(ip_list) > 0:
        return ip_list[0]
    return '127.0.0.1'


def clean_for_filename(string):
    """ Cleans up string for use in filename """
    string = re.sub('\\[(.*?)]', '', string)
    string = re.sub('\\s+', '-', string)
    string = string.replace(' : ', ' - ')
    string = string.replace(':', '-')
    string = string.replace('&', 'and')
    string = string.replace("\\", " - ")
    string = string.replace(" ", " - ")
    string = string.strip()
    return re.sub('[^\\w.() -]', '', string)


def duplicate_run_check(dev_path):
    """
    This will kill any runs that have been triggered twice on the same device

    :return: None
    """
    running_jobs = db.session.query(m.Job).filter(
        m.Job.status.notin_(['fail', 'success']), m.Job.devpath == dev_path).all()
    if len(running_jobs) >= 1:
        for j in running_jobs:
            print(j.start_time - datetime.datetime.now())
            mins_last_run = int(round(abs(j.start_time - datetime.datetime.now()).total_seconds()) / 60)
            if mins_last_run <= 1:
                logging.error(f"Job already running on {dev_path}")
                sys.exit(1)


def save_disc_poster(final_directory, job):
    """
     Use FFMPeg to convert Large Poster if enabled in config
    :param final_directory: folder to put the poster in
    :param job: Current Job
    :return: None
    """
    if job.disctype == "dvd" and cfg["RIP_POSTER"]:
        os.system("mount " + job.devpath)
        if os.path.isfile(job.mountpoint + "/JACKET_P/J00___5L.MP2"):
            logging.info("Converting NTSC Poster Image")
            os.system('ffmpeg -i "' + job.mountpoint + '/JACKET_P/J00___5L.MP2" "'
                      + final_directory + '/poster.png"')
        elif os.path.isfile(job.mountpoint + "/JACKET_P/J00___6L.MP2"):
            logging.info("Converting PAL Poster Image")
            os.system('ffmpeg -i "' + job.mountpoint + '/JACKET_P/J00___6L.MP2" "'
                      + final_directory + '/poster.png"')
        os.system("umount " + job.devpath)


def check_for_dupe_folder(have_dupes, hb_out_path, job):
    """
    Check if the folder already exists
     if it exist lets make a new one using random numbers
    :param have_dupes: is this title in the local arm database
    :param hb_out_path: path to HandBrake out
    :param job: Current job
    :return: Final media directory path
    """
    if (make_dir(hb_out_path)) is False:
        logging.info(f"Handbrake Output directory \"{hb_out_path}\" already exists.")
        # Only begin ripping if we are allowed to make duplicates
        # Or the successful rip of the disc is not found in our database
        logging.debug(f"Value of ALLOW_DUPLICATES: {cfg['ALLOW_DUPLICATES']}")
        logging.debug(f"Value of have_dupes: {have_dupes}")
        if cfg["ALLOW_DUPLICATES"] or not have_dupes:
            random_time = round(time.time() * 100)
            hb_out_path = hb_out_path + "_" + str(random_time)

            if (make_dir(hb_out_path)) is False:
                # We failed to make a random directory, most likely a permission issue
                logging.exception(
                    "A fatal error has occurred and ARM is exiting.  "
                    "Couldn't create filesystem. Possible permission error")
                notify(job, NOTIFY_TITLE,
                       f"ARM encountered a fatal error processing {job.title}."
                       f" Couldn't create filesystem. Possible permission error. ")
                job.status = "fail"
                db.session.commit()
                sys.exit()
        else:
            # We aren't allowed to rip dupes, notify and exit
            logging.info("Duplicate rips are disabled.")
            notify(job, NOTIFY_TITLE, f"ARM Detected a duplicate disc. For {job.title}. "
                                      f"Duplicate rips are disabled. "
                                      f"You can re-enable them from your config file. ")
            job.eject()
            job.status = "fail"
            db.session.commit()
            sys.exit()
    return hb_out_path


def check_for_wait(job, config):
    """
    wait if we have have waiting for user input updates\n\n
    :param config: Config for current Job
    :param job: Current Job
    :return: None
    """
    #  If we have have waiting for user input enabled
    if cfg["MANUAL_WAIT"]:
        logging.info(f"Waiting {cfg['MANUAL_WAIT_TIME']} seconds for manual override.")
        job.status = "waiting"
        db.session.commit()
        sleep_time = 0
        while sleep_time < cfg["MANUAL_WAIT_TIME"]:
            time.sleep(5)
            sleep_time += 5
            db.session.refresh(job)
            db.session.refresh(config)
            if job.title_manual:
                logging.info("Manual override found.  Overriding auto identification values.")
                job.updated = True
                job.hasnicetitle = True
                break
        job.status = "active"
        db.session.commit()
