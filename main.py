from pydrive.auth import GoogleAuth
from pydrive.drive import GoogleDrive
import os
import re
import yaml
import magic
import logging
import time

config_file = open('config.yaml', 'r')
config_yaml = yaml.load(config_file)

DRIVE_PHOTOS_ROOT_ID = config_yaml['remote_photos_root']
LOCAL_PHOTOS_ROOT_DIR = config_yaml['local_photos_root']
PHOTO_DIR_REGEX = config_yaml['photo_dir_regex']

photo_dir_pattern = re.compile(PHOTO_DIR_REGEX)

mime = magic.open(magic.MAGIC_MIME)
mime.load()


def is_photo_dir(photo_dir):
    return photo_dir_pattern.match(photo_dir) is not None


def remove_start_end_slash(dir_path):
    new_path = dir_path
    if new_path.startswith('/'):
        new_path = new_path[1:]
    if new_path.endswith('/'):
        new_path = new_path[:-1]
    return new_path


class UploadItem(object):

    def __init__(self, abs_path, upload_dir, title, mimetype):
        self.abs_path = abs_path
        self.upload_dir = upload_dir
        self.title = title
        self.mimetype = mimetype

    def __str__(self):
        return "abs:{abs};dir:({dir})".format(abs=self.abs_path, dir=str(self.upload_dir))

class UploadDir(object):

    def __init__(self, rel_dir, parent_dir, title):
        self.rel_dir = rel_dir
        self.parent_dir = parent_dir
        self.title = title

    def __str__(self):
        return "rel:{rel};parent:({parent});title:{title}".format(
            rel=self.rel_dir, parent=str(self.parent_dir), title=self.title)


class LocalFSManager(object):

    def __init__(self, root_dir):
        self.root_dir = root_dir

    def get_photo_dirs(self):
        photo_dirs = set()
        for o in os.listdir(self.root_dir):
            if os.path.isdir(
                    os.path.join(self.root_dir, o)) and is_photo_dir(o):
                photo_dirs.add(o)
        logging.info("Found {dirs} photo directories".format(
            dirs=len(photo_dirs)))
        return photo_dirs

    def get_items(self, item_dir):
        upload_items = list()
        upload_dirs = dict()
        abs_item_dir = os.path.join(self.root_dir, item_dir)
        upload_dirs[''] = UploadDir('', None, item_dir)
        logging.debug("Get items from: %s" % abs_item_dir)
        for dirpath, dnames, fnames in os.walk(abs_item_dir):
            logging.debug("Entering folder: %s" % (dirpath))
            for f in fnames:
                abs_item_path = os.path.join(dirpath, f)
                mimetype = mime.file(abs_item_path)
                # TODO: Use mime regex
                if 'image' in mimetype or 'video' in mimetype:
                    rel_item_dir = remove_start_end_slash(dirpath.replace(abs_item_dir, ''))
                    if not rel_item_dir in upload_dirs:
                        parent_dir_path, item_dir_title = os.path.split(rel_item_dir)
                        upload_dirs[rel_item_dir] = UploadDir(rel_item_dir, upload_dirs.get(parent_dir_path), item_dir_title)
                    upload_dir = upload_dirs[rel_item_dir]
                    upload_item = UploadItem(
                        abs_item_path, upload_dir, f, mimetype)
                    upload_items.append(upload_item)
        return upload_items, upload_dirs

    def check_dir(self, photo_dir, photo_dir_files):
        abs_photo_dir = os.path.join(self.root_dir, photo_dir)
        logging.debug("Entering: %s" % abs_photo_dir)

        subdirs = []
        for _, sub_dirs, _ in os.walk(abs_photo_dir):
            subdirs += sub_dirs
            break

        logging.debug("Subdirs: %s" % subdirs)

        has_photo = False
        for subdir in subdirs:
            sub_dir_path = os.path.join(photo_dir, subdir)
            logging.debug("Recursive: %s" % sub_dir_path)
            has_photo = has_photo or self.check_dir(sub_dir_path)

        photos = self.get_photos(abs_photo_dir)
        logging.debug("Photos: %s" % photos)

        if not has_photo and len(photos) == 0:
            return False

        if len(photos) > 0:
            photo_dir_files[abs_photo_dir] = photos

        return True


class RemoteFSManager(object):
    # use https://code.google.com/apis/console#:access
    # https://developers.google.com/drive/web/quickstart/python
    # http://pythonhosted.org/PyDrive/
    # Auto-iterate through all files that matches this query
    # https://developers.google.com/drive/web/search-parameters

    MIME_FOLDER = 'application/vnd.google-apps.folder'

    def __init__(self, root_dir):
        self.root_dir = root_dir
        self.drive = self._get_drive_conn()

    def _get_drive_conn(self):
        # Drive login
        gauth = GoogleAuth()
        gauth.LocalWebserverAuth()
        drive = GoogleDrive(gauth)
        return drive

    def get_photo_dirs(self):
        photo_dirs = dict()
        file_list = self.drive.ListFile({
            'q': "'%s' in parents and trashed=false and mimeType='%s'" % (
                self.root_dir, self.MIME_FOLDER)
            }).GetList()
        for remote_item in file_list:
            photo_id = remote_item['id']
            photo_dir = remote_item['title']
            if is_photo_dir(photo_dir):
                photo_dirs[photo_dir] = photo_id
        return photo_dirs

    def get_diff(self, photo_dirs1, photo_dirs2):
        # Find folders that not exists photo_dirs2
        diff_dirs = list()
        for photo_dir1 in photo_dirs1:
            if photo_dir1 not in photo_dirs2:
                logging.debug("Pending for upload: {dir}".format(dir=photo_dir1))
                diff_dirs.append(photo_dir1)
        return diff_dirs

    def create_dir(self, photo_dir, root_dir=None):
        if root_dir is None:
            root_dir = self.get_root_dir()
        new_dir = self.drive.CreateFile({
            "title": photo_dir,
            "parents":  [{
                "id": root_dir}],
            "mimeType": "application/vnd.google-apps.folder"})
        new_dir.Upload()
        return new_dir['id']

    def upload_photo(self, photo, mimetype, photo_dir):
        title = os.path.split(photo)[1]
        new_photo = self.drive.CreateFile({
            "title": title,
            "parents":  [{
                "id": photo_dir}],
            "mimeType": mimetype})
        new_photo.SetContentFile(photo)
        new_photo.Upload()
        return new_photo['id']

    def get_root_dir(self):
        return DRIVE_PHOTOS_ROOT_ID


class UploadManager(object):

    def __init__(self, local_fs, remote_fs):
        self.local_fs = local_fs
        self.remote_fs = remote_fs

    def select_dirs(self, dirs):
        count = 0
        for dir in dirs:
            print '{id}) {dir}'.format(id=count, dir=dir)
            count += 1
        raw_sel = input("Which folders to sync? ")
        selection = list()
        if type(raw_sel) == int:
            raw_sel = [raw_sel]

        print "Selected:", raw_sel
        for i in raw_sel:
            dir = dirs[i]
            selection.append(dir)
            print dir
        return selection

    def upload_dir(self, local_photo_dir):
        # Get all local photos from dir
        local_items, local_dirs = self.local_fs.get_items(local_photo_dir)

        # Create remote subdirs
        remote_dirs = dict()
        """
        for item in local_items:
            if not item.rel_dir in remote_dirs:
                id
                remote_dirs[]


            # Create not existing folder
            remote_dir = remote_fs.create_dir(
                local_photo_dir, remote_fs.get_root_dir())
            # Upload all photos from pending folder
            for photo, mimetype in local_photos:
                logging.debug("Uploading: {file}".format(file=photo))
                remote_fs.upload_photo(photo, mimetype, remote_dir)

        # TODO: Recursive upload
        # Get dirs from dir
        # has_photos?
        # Get files from dir
        # Create dir remote
        # Upload files from dir
        """
        pass

    def process(self):
        total_duration = time.time()
        local_photo_dirs = self.local_fs.get_photo_dirs()
        remote_photo_dirs = self.remote_fs.get_photo_dirs()

        # Find differences
        pending_upload_dirs = remote_fs.get_diff(
            local_photo_dirs, remote_photo_dirs)

        # Select folders for upload
        selected_upload_dirs = self.select_dirs(pending_upload_dirs)

        # Create remote folders
        for local_photo_dir in selected_upload_dirs:
            duration = time.time()
            self.upload_dir(local_photo_dir)
            duration = time.time() - duration
            logging.info("Uploaded {dir} in {duration}s".format(
                dir=local_photo_dir, duration=duration))
        total_duration = time.time() - total_duration
        logging.info("Processed in {duration}s".format(
            duration=total_duration))


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    local_fs = LocalFSManager(LOCAL_PHOTOS_ROOT_DIR)
    remote_fs = RemoteFSManager(DRIVE_PHOTOS_ROOT_ID)

    uploader = UploadManager(local_fs, remote_fs)
    uploader.process()
