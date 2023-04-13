#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 2023/4/12 6:59 PM
# @Author  : wangdongming
# @Site    :
# @File    : posex.py
# @Software: Hifive
import os.path
import typing
from tools.encryptor import b64_image
from handlers.formatter import AlwaysonScriptArgsFormatter

PoseX = 'Posex'


class PosexFormatter(AlwaysonScriptArgsFormatter):

    def name(self):
        return PoseX

    def format(self, args: typing.Union[typing.Sequence[typing.Any], typing.Mapping]) \
            -> typing.Sequence[typing.Any]:

        def obj_to_array(obj: typing.Mapping) -> typing.Sequence:
            # 如果是[OBJ1, OBJ2]形式的，需要转换为ARRAY
            if isinstance(obj, dict):
                return [args['enabled'],
                        args['base64'],
                        args['cn_num']]
            return obj

        if isinstance(args, dict):
            posex_script_args = obj_to_array(args)
        else:
            posex_script_args = [obj_to_array(x) for x in args]
        if posex_script_args:
            if len(posex_script_args) > 2 and posex_script_args[0]:
                image = posex_script_args[1]
                if os.path.isfile(image):
                    # 必须是png
                    posex_script_args[1] = b64_image(image)

        return posex_script_args