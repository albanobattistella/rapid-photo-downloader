#!/usr/bin/python3
__author__ = 'Damon Lynch'

# Copyright (C) 2011-2015 Damon Lynch <damonlynch@gmail.com>

# This file is part of Rapid Photo Downloader.
#
# Rapid Photo Downloader is free software: you can redistribute it and/or
# modify it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Rapid Photo Downloader is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Rapid Photo Downloader.  If not,
# see <http://www.gnu.org/licenses/>.

"""
Generates names for files and folders, and renames (moves) files.

Runs as a daemon process.
"""

import os
import shutil
import datetime
from enum import Enum
from collections import namedtuple

import errno
import logging
import pickle

from PyQt5.QtCore import QSize
from PyQt5.QtGui import QImage

import exiftool
import generatename as gn
import problemnotification as pn
from preferences import DownloadsTodayTracker, Preferences
from constants import (ConflictResolution, FileType, DownloadStatus,
                       ThumbnailCacheStatus, ThumbnailSize,
                       RenameAndMoveStatus)
from interprocess import (RenameAndMoveFileData,
                          RenameAndMoveFileResults, DaemonProcess)
from rpdfile import RPDFile
from thumbnail import Thumbnail, qimage_to_png_buffer
from cache import FdoCacheNormal, FdoCacheLarge, ThumbnailCache
from sql import DownloadedSQL

from gettext import gettext as _

logging.basicConfig(format='%(levelname)s:%(asctime)s:%(message)s',
                    datefmt='%H:%M:%S',
                    level=logging.DEBUG)


class SyncRawJpegStatus(Enum):
    matching_pair = 1
    no_match = 2
    error_already_downloaded = 3
    error_datetime_mismatch = 4


SyncRawJpegMatch = namedtuple('SyncRawJpegMatch', 'status, sequence_number')
SyncRawJpegResult = namedtuple('SyncRawJpegResult', 'sequence_to_use, '
                                                    'failed, photo_name, photo_ext')

class SyncRawJpeg:
    """
    Match JPEG and RAW images
    """

    def __init__(self):
        self.photos = {}

    def add_download(self, name, extension, date_time, sub_seconds,
                     sequence_number_used):
        if name not in self.photos:
            self.photos[name] = (
                [extension], date_time, sub_seconds, sequence_number_used)
        else:
            if extension not in self.photos[name][0]:
                self.photos[name][0].append(extension)

    def matching_pair(self, name, extension, date_time, sub_seconds):
        """Checks to see if the image matches an image that has already been
        downloaded.
        Image name (minus extension), exif date time, and exif subseconds
        are checked.

        Returns -1 and a sequence number if the name, extension, and exif
        values match (i.e. it has already been downloaded)
        Returns 0 and a sequence number if name and exif values match,
        but the extension is different (i.e. a matching RAW + JPG image)
        Returns -99 and a sequence number of None if photos detected with
        the same filenames, but taken at different times
        Returns 1 and a sequence number of None if no match"""

        if name in self.photos:
            if self.photos[name][1] == date_time and self.photos[name][
                2] == sub_seconds:
                if extension in self.photos[name][0]:
                    return SyncRawJpegMatch(
                        SyncRawJpegStatus.error_already_downloaded,
                        self.photos[name][3])
                else:
                    return SyncRawJpegMatch(SyncRawJpegStatus.matching_pair,
                                            self.photos[name][3])
            else:
                return SyncRawJpegMatch(
                    SyncRawJpegStatus.error_datetime_mismatch, None)
        return SyncRawJpegMatch(SyncRawJpegStatus.no_match, None)

    def ext_exif_date_time(self, name):
        """Returns first extension, exif date time and subseconds data for
        the already downloaded photo"""
        return (
            self.photos[name][0][0], self.photos[name][1],
            self.photos[name][2])


def time_subseconds_human_readable(date, subseconds):
    return _("%(hour)s:%(minute)s:%(second)s:%(subsecond)s") % \
           {'hour': date.strftime("%H"),
            'minute': date.strftime("%M"),
            'second': date.strftime("%S"),
            'subsecond': subseconds}


def load_metadata(rpd_file, et_process: exiftool.ExifTool, temp_file=True) \
        -> bool:
    """
    Loads the metadata for the file

    :param et_process: the deamon exiftool process
    :param temp_file: If true, the the metadata from the temporary file
     rather than the original source file is used. This is important,
     because the metadata  can be modified by the filemodify process
    :return True if operation succeeded, false otherwise
    """
    if rpd_file.metadata is None:
        if not rpd_file.load_metadata(exiftool_process=et_process,
                                      temp_file=temp_file):
            # Error in reading metadata
            rpd_file.add_problem(None, pn.CANNOT_DOWNLOAD_BAD_METADATA,
                                 {'filetype': rpd_file.title_capitalized})
            return False
    return True


def _generate_name(generator, rpd_file, et_process):
    do_generation = load_metadata(rpd_file, et_process)

    if do_generation:
        value = generator.generate_name(rpd_file)
        if value is None:
            value = ''
    else:
        value = ''

    return value


def generate_subfolder(rpd_file: RPDFile, et_process):
    if rpd_file.file_type == FileType.photo:
        generator = gn.PhotoSubfolder(rpd_file.subfolder_pref_list)
    else:
        generator = gn.VideoSubfolder(rpd_file.subfolder_pref_list)

    rpd_file.download_subfolder = _generate_name(generator, rpd_file,
                                                 et_process)


def generate_name(rpd_file: RPDFile, et_process):
    if rpd_file.file_type == FileType.photo:
        generator = gn.PhotoName(rpd_file.name_pref_list)
    else:
        generator = gn.VideoName(rpd_file.name_pref_list)

    rpd_file.download_name = _generate_name(generator, rpd_file, et_process)


class RenameMoveFileWorker(DaemonProcess):
    def __init__(self):
        super().__init__('Rename and Move')

        self.prefs = Preferences()

        self.sync_raw_jpeg = SyncRawJpeg()
        self.downloaded = DownloadedSQL()

        logging.debug("Start of day is set to %s", self.prefs.day_start)


    def progress_callback_no_update(self, amount_downloaded, total):
        pass
        # ~ if debug_progress:
        # ~ logging.debug("%.1f", amount_downloaded / float(total))

    def notify_file_already_exists(self, rpd_file: RPDFile, identifier=None):
        """
        Notify user that the download file already exists
        """
        # get information on when the existing file was last modified
        try:
            modification_time = os.path.getmtime(
                rpd_file.download_full_file_name)
            dt = datetime.datetime.fromtimestamp(modification_time)
            date = dt.strftime("%x")
            time = dt.strftime("%X")
        except:
            logging.warning(
                "Could not determine the file modification time of %s",
                rpd_file.download_full_file_name)
            date = time = ''

        if not identifier:
            rpd_file.add_problem(None, pn.FILE_ALREADY_EXISTS_NO_DOWNLOAD,
                                 {'filetype': rpd_file.title_capitalized})
            rpd_file.add_extra_detail(pn.EXISTING_FILE,
                                      {'filetype': rpd_file.title,
                                       'date': date, 'time': time})
            rpd_file.status = DownloadStatus.download_failed
            rpd_file.error_extra_detail = pn.extra_detail_definitions[
                                              pn.EXISTING_FILE] % \
                                          {'date': date, 'time': time,
                                           'filetype': rpd_file.title}
        else:
            rpd_file.add_problem(None, pn.UNIQUE_IDENTIFIER_ADDED,
                                 {'filetype': rpd_file.title_capitalized})
            rpd_file.add_extra_detail(pn.UNIQUE_IDENTIFIER,
                                      {'identifier': identifier,
                                       'filetype': rpd_file.title,
                                       'date': date, 'time': time})
            rpd_file.status = DownloadStatus.downloaded_with_warning
            rpd_file.error_extra_detail = pn.extra_detail_definitions[
                                              pn.UNIQUE_IDENTIFIER] % \
                                          {'identifier': identifier,
                                           'filetype': rpd_file.title,
                                           'date': date, 'time': time}
        rpd_file.error_title = rpd_file.problem.get_title()
        rpd_file.error_msg = _(
            "Source: %(source)s\nDestination: %(destination)s") \
                             % {'source': rpd_file.full_file_name,
                                'destination':
                                    rpd_file.download_full_file_name}

    def notify_download_failure_file_error(self, rpd_file: RPDFile, inst):
        """
        Handle cases where file failed to download
        """
        rpd_file.add_problem(None, pn.DOWNLOAD_COPYING_ERROR,
                             {'filetype': rpd_file.title})
        rpd_file.add_extra_detail(pn.DOWNLOAD_COPYING_ERROR_DETAIL, inst)
        rpd_file.status = DownloadStatus.download_failed
        logging.error("Failed to create file %s: %s",
                      rpd_file.download_full_file_name, inst)

        rpd_file.error_title = rpd_file.problem.get_title()
        rpd_file.error_msg = _("%(problem)s\nFile: %(file)s") % \
                             {'problem': rpd_file.problem.get_problems(),
                              'file': rpd_file.full_file_name}

    def download_file_exists(self, rpd_file: RPDFile) -> bool:
        """
        Check how to handle a download file already existing
        """
        if (self.prefs.conflict_resolution ==
                ConflictResolution.add_identifier):
            logging.debug(
                "Will add unique identifier to avoid duplicate filename for "
                "%s", rpd_file.full_file_name)
            return True
        else:
            self.notify_file_already_exists(rpd_file)
            return False

    def same_name_different_exif(self, sync_photo_name, rpd_file):
        """Notify the user that a file was already downloaded with the same
        name, but the exif information was different"""
        i1_ext, i1_date_time, i1_subseconds = \
            self.sync_raw_jpeg.ext_exif_date_time(
                sync_photo_name)
        detail = {'image1': "%s%s" % (sync_photo_name, i1_ext),
                  'image1_date': i1_date_time.strftime("%x"),
                  'image1_time': time_subseconds_human_readable(i1_date_time,
                                                                i1_subseconds),
                  'image2': rpd_file.name,
                  'image2_date': rpd_file.metadata.date_time().strftime("%x"),
                  'image2_time': time_subseconds_human_readable(
                      rpd_file.metadata.date_time(),
                      rpd_file.metadata.sub_seconds())}
        rpd_file.add_problem(None, pn.SAME_FILE_DIFFERENT_EXIF, detail)

        rpd_file.error_title = _(
            'Photos detected with the same filenames, but taken at different '
            'times')
        rpd_file.error_msg = \
            pn.problem_definitions[pn.SAME_FILE_DIFFERENT_EXIF][1] % detail
        rpd_file.status = DownloadStatus.downloaded_with_warning

    def _move_associate_file(self, extension: str, full_base_name: str,
                             temp_associate_file: str):
        """
        Move (rename) the associate file using the pre-generated name

        :return: tuple of result (True if succeeded, False otherwise)
         and full path and filename
        """

        download_full_name = full_base_name + extension

        # move (rename) associate file
        try:
            # don't check to see if it already exists
            os.rename(temp_associate_file, download_full_name)
            success = True
        except:
            success = False

        return (success, download_full_name)

    def move_thm_file(self, rpd_file: RPDFile):
        """Move (rename) the THM thumbnail file using the pregenerated name"""
        ext = None
        if hasattr(rpd_file, 'thm_extension'):
            if rpd_file.thm_extension:
                ext = rpd_file.thm_extension
        if ext is None:
            ext = '.THM'

        result, rpd_file.download_thm_full_name = self._move_associate_file(
            ext, rpd_file.download_full_base_name, rpd_file.temp_thm_full_name)

        if not result:
            logging.error("Failed to move video THM file %s",
                          rpd_file.download_thm_full_name)

    def move_audio_file(self, rpd_file: RPDFile):
        """
        Move (rename) the associate audio file using the pre-generated
        name
        """
        ext = None
        if hasattr(rpd_file, 'audio_extension'):
            if rpd_file.audio_extension:
                ext = rpd_file.audio_extension
        if ext is None:
            ext = '.WAV'

        result, rpd_file.download_audio_full_name = self._move_associate_file(
            ext, rpd_file.download_full_base_name,
            rpd_file.temp_audio_full_name)

        if not result:
            logging.error("Failed to move file's associated audio file %s",
                          rpd_file.download_audio_full_name)

    def check_for_fatal_name_generation_errors(self, rpd_file: RPDFile) -> \
            bool:
        """
        :return False if either the download subfolder or filename are
         blank, else returns True
         """

        if not rpd_file.download_subfolder or not rpd_file.download_name:
            if not rpd_file.download_subfolder and not rpd_file.download_name:
                area = _("subfolder and filename")
            elif not rpd_file.download_name:
                area = _("filename")
            else:
                area = _("subfolder")
            rpd_file.add_problem(None, pn.ERROR_IN_NAME_GENERATION,
                                 {'filetype': rpd_file.title_capitalized,
                                  'area': area})
            rpd_file.add_extra_detail(pn.NO_DATA_TO_NAME, {'filetype': area})
            rpd_file.status = DownloadStatus.download_failed

            rpd_file.error_title = rpd_file.problem.get_title()
            rpd_file.error_msg = _("%(problem)s\nFile: %(file)s") % \
                                 {'problem': rpd_file.problem.get_problems(),
                                  'file': rpd_file.full_file_name}
            return False
        else:
            return True

    def add_unique_identifier(self, rpd_file: RPDFile) -> \
            bool:
        """
        Adds a unique identifier like _1 to a filename, in ever
        incrementing values, until a unique filename is generated.

        :param rpd_file: the file being worked on
        :return: True if the operation was successful, else returns
         False
        """
        name = os.path.splitext(rpd_file.download_name)
        full_name = rpd_file.download_full_file_name
        while True:
            self.duplicate_files[full_name] = self.duplicate_files.get(
                full_name, 0) + 1
            identifier = '_%s' % self.duplicate_files[full_name]
            rpd_file.download_name = name[0] + \
                                     identifier + \
                                     name[1]
            rpd_file.download_full_file_name = \
                os.path.join(
                    rpd_file.download_path,
                    rpd_file.download_name)

            try:
                if os.path.exists(
                        rpd_file.download_full_file_name):
                    raise IOError(errno.EEXIST,
                                  "File exists: %s" %
                                  rpd_file.download_full_file_name)
                os.rename(rpd_file.temp_full_file_name,
                          rpd_file.download_full_file_name)
                self.notify_file_already_exists(rpd_file, identifier)
                return True

            except OSError as inst:
                if inst.errno != errno.EEXIST:
                    self.notify_download_failure_file_error(
                        rpd_file, inst)
                    return False

    def sync_raw_jpg(self, rpd_file: RPDFile) -> SyncRawJpegResult:

        failed = False
        sequence_to_use = None
        photo_name, photo_ext = os.path.splitext(
            rpd_file.name)
        if not load_metadata(rpd_file, self.et_process):
            failed = True
            rpd_file.status = DownloadStatus.download_failed
            self.check_for_fatal_name_generation_errors(
                rpd_file)
        else:
            matching_pair = self.sync_raw_jpeg.matching_pair(
                name=photo_name, extension=photo_ext,
                date_time=rpd_file.metadata.date_time(),
                sub_seconds=rpd_file.metadata.sub_seconds())
            """ :type : SyncRawJpegMatch"""
            sequence_to_use = matching_pair.sequence_number
            if matching_pair.status == \
                    SyncRawJpegStatus.error_already_downloaded:
                # this exact file has already been
                # downloaded (same extension, same filename,
                # and same exif date time subsecond info)
                if (self.prefs.conflict_resolution !=
                        ConflictResolution.add_identifier):
                    rpd_file.add_problem(None,
                                    pn.FILE_ALREADY_DOWNLOADED, {
                                    'filetype':rpd_file.title_capitalized})
                    rpd_file.error_title = _(
                        'Photo has already been downloaded')
                    rpd_file.error_msg = _(
                        "Source: %(source)s") % {
                                         'source': rpd_file.full_file_name}
                    rpd_file.status = \
                        DownloadStatus.download_failed
                    failed = True
            else:
                self.sequences.set_matched_sequence_value(
                    matching_pair.sequence_number)
                if matching_pair.status == \
                        SyncRawJpegStatus.error_datetime_mismatch:
                    self.same_name_different_exif(
                        photo_name, rpd_file)
        return SyncRawJpegResult(sequence_to_use, failed, photo_name,
                                 photo_ext)

    def prepare_rpd_file(self, rpd_file: RPDFile):
        """
        Populate the RPDFile with download values used in subfolder
        and filename generation
        """
        if rpd_file.file_type == FileType.photo:
            rpd_file.download_folder = self.prefs.photo_download_folder
            rpd_file.subfolder_pref_list = self.prefs.photo_subfolder
            rpd_file.name_pref_list = self.prefs.photo_rename
        else:
            rpd_file.download_folder = self.prefs.video_download_folder
            rpd_file.subfolder_pref_list = self.prefs.video_subfolder
            rpd_file.name_pref_list = self.prefs.video_rename

    def process_rename_failure(self, rpd_file: RPDFile):
        if rpd_file.problem is None:
            logging.error("%s (%s) has no problem information",
                          rpd_file.full_file_name,
                          rpd_file.download_full_file_name)
        else:
            logging.error("%s: %s - %s", rpd_file.full_file_name,
                      rpd_file.problem.get_title(),
                      rpd_file.problem.get_problems())
        try:
            os.remove(rpd_file.temp_full_file_name)
        except OSError:
            logging.error("Failed to delete temporary file %s",
                          rpd_file.temp_full_file_name)

    def generate_names(self, rpd_file: RPDFile) -> bool:

        rpd_file.strip_characters = self.prefs.strip_characters

        generate_subfolder(rpd_file, self.exiftool_process)

        if rpd_file.download_subfolder:
            logging.debug("Generated subfolder name %s for file %s",
                          rpd_file.download_subfolder, rpd_file.name)

            rpd_file.sequences = self.sequences

            # generate the file name
            generate_name(rpd_file, self.exiftool_process)

            if rpd_file.has_problem():
                logging.debug(
                    "Encountered a problem generating file name for file %s",
                    rpd_file.name)
                rpd_file.status = DownloadStatus.downloaded_with_warning
                rpd_file.error_title = rpd_file.problem.get_title()
                rpd_file.error_msg = _(
                    "%(problem)s\nFile: %(file)s") % {'problem':
                                           rpd_file.problem.get_problems(),
                                          'file': rpd_file.full_file_name}
            else:
                logging.debug("Generated file name %s for file %s",
                              rpd_file.download_name, rpd_file.name)
        else:
            logging.debug("Failed to generate subfolder name for file: %s",
                          rpd_file.name)

        return self.check_for_fatal_name_generation_errors(rpd_file)

    def move_file(self, rpd_file: RPDFile) -> bool:
        move_succeeded = False

        rpd_file.download_path = os.path.join(
            rpd_file.download_folder,
            rpd_file.download_subfolder)
        rpd_file.download_full_file_name = os.path.join(
            rpd_file.download_path, rpd_file.download_name)
        rpd_file.download_full_base_name = \
            os.path.splitext(rpd_file.download_full_file_name)[0]

        if not os.path.isdir(rpd_file.download_path):
            try:
                os.makedirs(rpd_file.download_path)
            except IOError as inst:
                if inst.errno != errno.EEXIST:
                    logging.error(
                        "Failed to create download "
                        "subfolder: %s",
                        rpd_file.download_path)
                    logging.error(inst)
                    rpd_file.error_title = _(
                        "Failed to create download subfolder")
                    rpd_file.error_msg = _(
                        "Path: %s") % rpd_file.download_path

        # Move temp file to subfolder

        add_unique_identifier = False
        try:
            if os.path.exists(
                    rpd_file.download_full_file_name):
                raise IOError(errno.EEXIST,
                              "File exists: %s" %
                              rpd_file.download_full_file_name)
            logging.debug(
                "Attempting to rename file %s to %s .....",
                rpd_file.temp_full_file_name,
                rpd_file.download_full_file_name)
            os.rename(rpd_file.temp_full_file_name,
                      rpd_file.download_full_file_name)
            logging.debug("....successfully renamed file")
            move_succeeded = True
            if rpd_file.status != \
                    DownloadStatus.downloaded_with_warning:
                rpd_file.status = DownloadStatus.downloaded
        except OSError as inst:
            if inst.errno == errno.EEXIST:
                add_unique_identifier = \
                    self.download_file_exists(
                        rpd_file)
            else:
                rpd_file = self.notify_download_failure_file_error(
                    rpd_file, inst.strerror)
        except:
            rpd_file = self.notify_download_failure_file_error(
                rpd_file,
                "An unknown error occurred while renaming "
                "the file")

        if add_unique_identifier:
            self.add_unique_identifier(rpd_file)

        return move_succeeded

    def process_file(self, rpd_file: RPDFile, download_count: int):
        move_succeeded = False

        self.prepare_rpd_file(rpd_file)

        synchronize_raw_jpg = (self.prefs.must_synchronize_raw_jpg() and
                               rpd_file.file_type == FileType.photo)
        if synchronize_raw_jpg:
            sync_result = self.sync_raw_jpg(rpd_file)

            if sync_result.failed:
                return False

        generation_succeeded = self.generate_names(rpd_file)

        if generation_succeeded:
            move_succeeded = self.move_file(rpd_file)

            logging.debug("Finished processing file: %s",
                          download_count)

        if move_succeeded:
            if synchronize_raw_jpg:
                if sync_result.sequence_to_use is None:
                    sequence = \
                        self.sequences.create_matched_sequences
                else:
                    sequence = sync_result.sequence_to_use
                self.sync_raw_jpeg.add_download(
                    name=sync_result.photo_name,
                    extension=sync_result.photo_ext,
                    date_time=rpd_file.metadata.date_time(),
                    sub_seconds=rpd_file.metadata.sub_seconds(),
                    sequence_number_used=sequence)
            if not synchronize_raw_jpg or (synchronize_raw_jpg and \
                    sync_result.sequence_to_use is None):
                uses_sequence_session_no = self.prefs.any_pref_uses_session_sequence_no()
                uses_sequence_letter = self.prefs.any_pref_uses_sequence_letter_value()
                if uses_sequence_session_no or uses_sequence_letter:
                    self.sequences.increment(uses_sequence_session_no,
                                             uses_sequence_letter)
                if self.prefs.any_pref_uses_stored_sequence_no():
                    self.prefs.stored_sequence_no += 1
                self.downloads_today_tracker.increment_downloads_today()

            if rpd_file.temp_thm_full_name:
                self.move_thm_file(rpd_file)

            if rpd_file.temp_audio_full_name:
                self.move_audio_file(rpd_file)

            if rpd_file.temp_xmp_full_name:
                # copy and rename XMP sidecar file
                # generate_name() has generated xmp extension with correct capitalization
                download_xmp_full_name = rpd_file.download_full_base_name + rpd_file.xmp_extension

                try:
                    os.rename(rpd_file.temp_xmp_full_name,
                              download_xmp_full_name)
                    rpd_file.download_xmp_full_name = download_xmp_full_name
                except:
                    logging.error(
                        "Failed to move XMP sidecar file %s",
                        download_xmp_full_name)

        return move_succeeded

    def process_renamed_file(self, rpd_file: RPDFile) -> QImage:
        """
        Generate thumbnails for display (needed only if the thumbnail was
        from a camera or was not already generated) and for the
        freedesktop.org cache
        :return: thumbnail suitable for display to the user, if needed
        """
        thumbnail = None

        # Check to see if existing thumbnail in FDO cache can be modified
        # and renamed to reflect new URI
        mtime = os.path.getmtime(rpd_file.download_full_file_name)
        if rpd_file.fdo_thumbnail_128_name and self.prefs.save_fdo_thumbnails:
            logging.debug("Copying and modifying existing FDO 128 thumbnail")
            rpd_file.fdo_thumbnail_128_name = \
                self.fdo_cache_normal.modify_existing_thumbnail_and_save_copy(
                    existing_cache_thumbnail=rpd_file.fdo_thumbnail_128_name,
                    full_file_name=rpd_file.download_full_file_name,
                    size=rpd_file.size,
                    modification_time=mtime)

        if rpd_file.fdo_thumbnail_256_name and self.prefs.save_fdo_thumbnails:
            logging.debug("Copying and modifying existing FDO 256 thumbnail")
            rpd_file.fdo_thumbnail_256_name = \
                self.fdo_cache_large.modify_existing_thumbnail_and_save_copy(
                    existing_cache_thumbnail=rpd_file.fdo_thumbnail_256_name,
                    full_file_name=rpd_file.download_full_file_name,
                    size=rpd_file.size,
                    modification_time=mtime)

        if ((self.prefs.save_fdo_thumbnails and (
                not rpd_file.fdo_thumbnail_256_name or
                not rpd_file.fdo_thumbnail_128_name)) or
                rpd_file.thumbnail_status !=
                ThumbnailCacheStatus.suitable_for_fdo_cache_write):
            logging.debug("Thumbnail status: %s", rpd_file.thumbnail_status)
            logging.debug("Have FDO 128: %s; have FDO 256: %s",
                          rpd_file.fdo_thumbnail_128_name != '',
                          rpd_file.fdo_thumbnail_256_name != '')
            discard_thumbnail = rpd_file.thumbnail_status == \
                ThumbnailCacheStatus.suitable_for_fdo_cache_write

            # Generate a newly rendered thumbnail for main window and
            # both sizes of freedesktop.org thumbnails. Note that
            # thumbnails downloaded from the camera using the gphoto2
            # get_thumb fuction have no orientation tag, so regenerating
            # the thumbnail again for those images is no bad thing!
            t = Thumbnail(rpd_file, rpd_file.camera_model,
                          thumbnail_quality_lower=False,
                          thumbnail_cache=self.thumbnail_cache,
                          fdo_cache_normal=self.fdo_cache_normal,
                          fdo_cache_large=self.fdo_cache_large,
                          must_generate_fdo_thumbs=
                                self.prefs.save_fdo_thumbnails,
                          have_ffmpeg_thumbnailer=self.have_ffmpeg_thumbnailer,
                          modification_time=mtime)
            thumbnail = t.get_thumbnail(size=QSize(ThumbnailSize.width,
                                     ThumbnailSize.height))
            if discard_thumbnail:
                thumbnail = None


        self.downloaded.add_downloaded_file(name=rpd_file.name,
                size=rpd_file.size,
                modification_time=rpd_file.modification_time,
                download_full_file_name=rpd_file.download_full_file_name)

        return thumbnail

    def run(self):
        """
        Generate subfolder and filename, and attempt to move the file
        from its temporary directory.

        Move video THM and/or audio file if there is one.

        If successful, increment sequence values.

        Report any success or failure.
        """
        i = 0

        # Dict of filename keys and int values used to track ints to add as
        # suffixes to duplicate files
        self.duplicate_files = {}

        self.have_ffmpeg_thumbnailer = shutil.which('ffmpegthumbnailer')

        with exiftool.ExifTool() as self.exiftool_process:
            while True:
                if i:
                    logging.debug("Finished %s. Getting next task.",
                                  i)

                # rename file and move to generated subfolder
                directive, content = self.receiver.recv_multipart()

                self.check_for_command(directive, content)

                data = pickle.loads(content)
                """ :type : RenameAndMoveFileData"""
                if data.message == RenameAndMoveStatus.download_started:
                    # Syncrhonize QSettings instance in preferences class
                    self.prefs.sync()

                    # Track downloads today, using a class whose purpose is to
                    # take the value in the user prefs, increment, and then
                    # finally used to update the prefs
                    self.downloads_today_tracker = DownloadsTodayTracker(
                        day_start=self.prefs.day_start,
                        downloads_today=self.prefs.downloads_today)

                    self.sequences = gn.Sequences(self.downloads_today_tracker,
                                              self.prefs.stored_sequence_no)
                    dl_today = self.downloads_today_tracker\
                        .get_or_reset_downloads_today()
                    logging.debug("Completed downloads today: %s", dl_today)
                    if self.prefs.save_fdo_thumbnails:
                        self.fdo_cache_normal = FdoCacheNormal()
                        self.fdo_cache_large = FdoCacheLarge()
                    else:
                        self.fdo_cache_large = self.fdo_cache_normal = None
                    if self.prefs.use_thumbnail_cache:
                        self.thumbnail_cache = ThumbnailCache()
                    else:
                        self.thumbnail_cache = None
                elif data.message == RenameAndMoveStatus.download_completed:
                    # Ask main application process to update prefs with stored
                    # sequence number and downloads today values. Cannot do it
                    # here because to save QSettings, QApplication should be
                    # used.
                    self.content = pickle.dumps(RenameAndMoveFileResults(
                        stored_sequence_no=self.sequences.stored_sequence_no,
                        downloads_today=\
                            self.downloads_today_tracker.downloads_today),
                        pickle.HIGHEST_PROTOCOL)
                    dl_today = self.downloads_today_tracker\
                        .get_or_reset_downloads_today()
                    logging.debug("Downloads today: %s", dl_today)
                    self.send_message_to_sink()
                else:
                    rpd_file = data.rpd_file
                    download_count = data.download_count
                    thumbnail = None

                    if data.download_succeeded:
                        move_succeeded = self.process_file(rpd_file,
                                                           download_count)
                        if not move_succeeded:
                            self.process_rename_failure(rpd_file)
                        else:
                            # Add system-wide thumbnail and record downloaded
                            # file in SQLite database
                            thumbnail = self.process_renamed_file(rpd_file)
                    else:
                        move_succeeded = False

                    if thumbnail is not None:
                        png_data = qimage_to_png_buffer(thumbnail).data()
                    else:
                        png_data = None
                    rpd_file.metadata = None
                    self.content = pickle.dumps(RenameAndMoveFileResults(
                        move_succeeded=move_succeeded,
                        rpd_file=rpd_file,
                        download_count=download_count,
                        png_data=png_data),
                        pickle.HIGHEST_PROTOCOL)
                    self.send_message_to_sink()

                    i += 1


if __name__ == '__main__':
    rename = RenameMoveFileWorker()
    rename.run()