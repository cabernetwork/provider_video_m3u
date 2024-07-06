"""
MIT License

Copyright (C) 2023 ROCKY4546
https://github.com/rocky4546

This file is part of Cabernet

Permission is hereby granted, free of charge, to any person obtaining a copy of this software
and associated documentation files (the "Software"), to deal in the Software without restriction,
including without limitation the rights to use, copy, modify, merge, publish, distribute,
sublicense, and/or sell copies of the Software, and to permit persons to whom the Software
is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.
"""

import json
import pathlib
import re
import urllib.request
import urllib.parse

import lib.m3u8 as m3u8
import lib.common.utils as utils
import lib.common.exceptions as exceptions
from lib.plugins.plugin_channels import PluginChannels
from lib.common.tmp_mgmt import TMPMgmt
from lib.db.db_scheduler import DBScheduler

TMP_FOLDERNAME = 'm3u'


class Channels(PluginChannels):

    def __init__(self, _instance_obj):
        super().__init__(_instance_obj)
        self.tmp_mgmt = TMPMgmt(self.config_obj.data)
        self.filter_dict = self.compile_m3u_filter(
            self.config_obj.data[self.config_section]['channel-m3u_filter'])
        self.url_chars = re.compile(r'[^-._~0-9a-zA-z]')

    def compile_m3u_filter(self, _str):
        """
        _dict contains a
        """
        if _str is None:
            return None
        nv_dict = {}
        split_nv = re.compile(r'([^ =]+)=([^,]+),*')
        nv_pairs = re.findall(split_nv, _str)
        for nv in nv_pairs:
            nv_dict[nv[0]] = re.compile(nv[1])
        return nv_dict

    def get_channels(self):
        global TMP_FOLDERNAME
        if self.config_obj.data[self.config_section]['channel-m3u_file'] is None:
            raise exceptions.CabernetException(
                '{}:{} M3U File config not set, unable to get channel list'
                .format(self.plugin_obj.name, self.instance_key))
        url = self.config_obj.data[self.config_section]['channel-m3u_file']
        file_type = self.detect_filetype(url)
        try:
            self.ch_db_list = self.db.get_channels(self.plugin_obj.name, self.instance_key)

            dn_filename = self.tmp_mgmt.download_file(url, 2, TMP_FOLDERNAME, None, file_type)
            if dn_filename is None:
                raise exceptions.CabernetException(
                    '{} Channel Request Failed, unable to download file for instance {}'
                    .format(self.plugin_obj.name, self.instance_key))
            m3u_file = self.extract_file(dn_filename, file_type)
            m3u8_obj = m3u8.load(str(m3u_file))
            ch_list = []
            if m3u8_obj is None or len(m3u8_obj.segments) == 0:
                raise exceptions.CabernetException(
                    '{} Channel Request Failed, no M3U data in the file for instance {}'
                    .format(self.plugin_obj.name, self.instance_key))
            self.logger.info("{}: Found {} stations on instance {}"
                             .format(self.plugin_obj.name, len(m3u8_obj.segments),
                                     self.instance_key))
            ref_url = None
            header = None
            if m3u8_obj.data['session_data']:
                for d in m3u8_obj.data['session_data']:
                    if d['data_id'] == 'HEADER':
                        header = d['value']
                        if header:
                            h_dict = json.loads(header)
                            self.logger.debug('Using Header Session Data {}'.format(h_dict))
                            if h_dict.get('Referer'):
                                ref_url = h_dict.get('Referer')
                            header = {'User-agent': utils.DEFAULT_USER_AGENT}
                            header.update(h_dict)

            for seg in m3u8_obj.segments:
                if self.is_m3u_filtered(seg):
                    continue
                ch_number = None
                if 'tvg-num' in seg.additional_props:
                    ch_number = seg.additional_props['tvg-num']
                elif 'tvg-chno' in seg.additional_props:
                    ch_number = seg.additional_props['tvg-chno']
                else:
                    ch_number = self.set_channel_num(ch_number)

                if 'tvg-id' in seg.additional_props and \
                        len(seg.additional_props['tvg-id']) != 0:
                    ch_id = seg.additional_props['tvg-id']
                elif 'channel-id' in seg.additional_props and \
                        len(seg.additional_props['channel-id']) != 0:
                    ch_id = seg.additional_props['channel-id']
                elif 'channelID' in seg.additional_props and \
                        len(seg.additional_props['channelID']) != 0:
                    ch_id = seg.additional_props['channelID']
                elif ch_number is not None:
                    ch_id = str(ch_number)
                else:
                    ch_id = None
                ch_id = re.sub(self.url_chars, '_', ch_id)

                ch_db_data = self.ch_db_list.get(ch_id)
                if 'tvg-logo' in seg.additional_props and seg.additional_props['tvg-logo'] != '':
                    thumbnail = seg.additional_props['tvg-logo']
                    if self.config_obj.data[self.config_section]['player-decode_url']:
                        thumbnail = urllib.parse.unquote(thumbnail)
                else:
                    thumbnail = None

                if ch_db_data:
                    enabled = ch_db_data[0]['enabled']
                    hd = ch_db_data[0]['json']['HD']
                    if ch_db_data[0]['json']['thumbnail'] == thumbnail:
                        thumbnail_size = ch_db_data[0]['json']['thumbnail_size']
                    else:
                        thumbnail_size = self.get_thumbnail_size(thumbnail, 2, ch_id)
                else:
                    enabled = True
                    hd = 0
                    thumbnail_size = self.get_thumbnail_size(thumbnail, 2, ch_id)

                stream_url = seg.absolute_uri

                if 'group-title' in seg.additional_props:
                    groups_other = seg.additional_props['group-title']
                else:
                    groups_other = None

                ch_callsign = seg.title.strip()
                channel = {
                    'id': ch_id,
                    'enabled': enabled,
                    'callsign': ch_callsign,
                    'number': ch_number,
                    'name': ch_callsign,
                    'HD': hd,
                    'group_hdtv': None,
                    'group_sdtv': None,
                    'groups_other': groups_other,
                    'thumbnail': thumbnail,
                    'thumbnail_size': thumbnail_size,
                    'VOD': False,
                    'stream_url': stream_url,
                    'Header': header,
                    'ref_url': ref_url,
                }
                ch_list.append(channel)

            sched_db = DBScheduler(self.config_obj.data)
            active = sched_db.get_num_active()
            if active < 2:
                self.tmp_mgmt.cleanup_tmp(TMP_FOLDERNAME)
            return ch_list
        except exceptions.CabernetException:
            self.tmp_mgmt.cleanup_tmp(TMP_FOLDERNAME)
            raise

    def get_channel_uri(self, _channel_id):
        ch_dict = self.db.get_channel(_channel_id, self.plugin_obj.name, self.instance_key)
        if not ch_dict:
            return None

        if self.config_obj.data[self.config_section]['player-decode_url']:
            stream_url = urllib.parse.unquote(ch_dict['json']['stream_url'])
        else:
            stream_url = ch_dict['json']['stream_url']

        if self.config_obj.data[self.config_section]['player-stream_type'] == 'm3u8redirect':
            return stream_url

        return self.get_best_stream(stream_url, 2, _channel_id)

    def detect_filetype(self, _filename):
        file_type = self.config_obj.data[self.config_section]['channel-m3u_file_type']
        if file_type == 'autodetect':
            extension = pathlib.Path(_filename).suffix
            if extension == '.gz':
                file_type = '.gz'
            elif extension == '.zip':
                file_type = '.zip'
            elif extension == '.m3u':
                file_type = '.m3u'
            elif extension == '.m3u8':
                file_type = '.m3u'
            else:
                raise exceptions.CabernetException(
                    '{}:{} M3U File unknown File Type.  Set the M3U File Type in config.'
                    .format(self.plugin_obj.name, self.instance_key))
        elif file_type == 'gzip':
            file_type = '.gz'
        elif file_type == 'zip':
            file_type = '.zip'
        elif file_type == 'm3u':
            file_type = '.m3u'
        elif file_type == 'm3u8':
            file_type = '.m3u'
        else:
            raise exceptions.CabernetException(
                '{}:{} M3U File unknown File Type in config.'
                .format(self.plugin_obj.name, self.instance_key))
        return file_type

    def extract_file(self, _filename, _file_type):
        if _file_type == '.zip':
            return self.tmp_mgmt.extract_zip(_filename)
        elif _file_type == '.gz':
            return self.tmp_mgmt.extract_gzip(_filename)
        elif _file_type == '.m3u':
            return _filename
        else:
            raise exceptions.CabernetException(
                '{}:{} M3U File unknown File Type {}'
                .format(self.plugin_obj.name, self.instance_key, _file_type))

    def is_m3u_filtered(self, _segment):
        """
        format: name=regexvalue, Note: regex string cannot have a comma in it...
        """
        all_matched = True
        if self.filter_dict is not None:
            for filtered in self.filter_dict:
                if filtered in _segment.additional_props:
                    if not bool(re.search(self.filter_dict[filtered], _segment.additional_props[filtered])):
                        all_matched = False
                        break
                else:
                    all_matched = False
                    break
        return not all_matched
