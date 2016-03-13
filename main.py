from pydrive.auth import GoogleAuth
from pydrive.drive import GoogleDrive
import os
import re
import yaml
import magic
import logging

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


class LocalFSManager(object):

    def __init__(self, root_dir):
        self.root_dir = root_dir

    def get_photo_dirs(self):
        photo_dirs = set()
        for o in os.listdir(self.root_dir):
            if os.path.isdir(
                    os.path.join(self.root_dir, o)) and is_photo_dir(o):
                photo_dirs.add(o)
        logging.info(
            "Found {dirs} photo directories".format(dirs=len(photo_dirs)))
        return photo_dirs

    def get_photos(self, photo_dir):
        photos = list()
        abs_photo_dir = os.path.join(self.root_dir, photo_dir)
        for dirpath, dnames, fnames in os.walk(abs_photo_dir):
            for f in fnames:
                abs_photo = os.path.join(dirpath, f)
                mimetype = mime.file(abs_photo)
                # TODO: Use mime regex
                if 'image' in mimetype or 'video' in mimetype:
                    photos.append((abs_photo, mimetype))
            break  # Only one level
        return photos


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

    def process(self):
        local_photo_dirs = local_fs.get_photo_dirs()
        remote_photo_dirs = remote_fs.get_photo_dirs()

        # Find differences
        pending_upload_dirs = remote_fs.get_diff(
            local_photo_dirs, remote_photo_dirs)

        # Select folders for upload
        selected_upload_dirs = self.select_dirs(pending_upload_dirs)

        # Create remote folders
        for local_photo_dir in selected_upload_dirs:
            # Create not existing folder
            remote_dir = remote_fs.create_dir(
                local_photo_dir, remote_fs.get_root_dir())
            # Get all local photos from dir
            local_photos = local_fs.get_photos(local_photo_dir)
            # Upload all photos from pending folder
            for photo, mimetype in local_photos:
                logging.debug("Uploading: {file}".format(file=photo))
                remote_fs.upload_photo(photo, mimetype, remote_dir)


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    local_fs = LocalFSManager(LOCAL_PHOTOS_ROOT_DIR)
    remote_fs = RemoteFSManager(DRIVE_PHOTOS_ROOT_ID)

    uploader = UploadManager(local_fs, remote_fs)
    uploader.process()
