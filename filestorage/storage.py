#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 2023/4/14 4:01 PM
# @Author  : wangdongming
# @Site    : 
# @File    : storage.py
# @Software: Hifive
import abc
import hashlib
import os
import json
import shutil
import time
import typing

import s3fs
import requests
import random
import importlib.util
from loguru import logger
from tools.redis import dist_locker
from tools.processor import MultiThreadWorker
from multiprocessing import cpu_count
from urllib.parse import urlparse, urlsplit
from tools.locks import LOCK_EX, LOCK_NB, lock, unlock
from tools.host import get_host_name, get_host_ip
from functools import partial

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.1 (KHTML, like Gecko) Chrome/22.0.1207.1 Safari/537.1"
    "Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/536.6 (KHTML, like Gecko) Chrome/20.0.1092.0 Safari/536.6",
    "Mozilla/5.0 (Windows NT 6.2) AppleWebKit/536.6 (KHTML, like Gecko) Chrome/20.0.1090.0 Safari/536.6",
    "Mozilla/5.0 (Windows NT 6.2; WOW64) AppleWebKit/537.1 (KHTML, like Gecko) Chrome/19.77.34.5 Safari/537.1",
    "Mozilla/5.0 (Windows NT 6.0) AppleWebKit/536.5 (KHTML, like Gecko) Chrome/19.0.1084.36 Safari/536.5",
    "Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/536.3 (KHTML, like Gecko) Chrome/19.0.1063.0 Safari/536.3",
    "Mozilla/5.0 (Windows NT 5.1) AppleWebKit/536.3 (KHTML, like Gecko) Chrome/19.0.1063.0 Safari/536.3",
    "Mozilla/5.0 (Windows NT 6.2) AppleWebKit/536.3 (KHTML, like Gecko) Chrome/19.0.1062.0 Safari/536.3",
    "Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/536.3 (KHTML, like Gecko) Chrome/19.0.1062.0 Safari/536.3",
    "Mozilla/5.0 (Windows NT 6.2) AppleWebKit/536.3 (KHTML, like Gecko) Chrome/19.0.1061.1 Safari/536.3",
    "Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/536.3 (KHTML, like Gecko) Chrome/19.0.1061.1 Safari/536.3",
    "Mozilla/5.0 (Windows NT 6.1) AppleWebKit/536.3 (KHTML, like Gecko) Chrome/19.0.1061.1 Safari/536.3",
    "Mozilla/5.0 (Windows NT 6.2) AppleWebKit/536.3 (KHTML, like Gecko) Chrome/19.0.1061.0 Safari/536.3",
    # 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0.2 Safari/605.1.15',
    "Mozilla/5.0 (Windows NT 6.2; WOW64) AppleWebKit/535.24 (KHTML, like Gecko) Chrome/19.0.1055.1 Safari/535.24",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 11_1_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/89.0.4389.90 Safari/537.36",
    "Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/57.0.2987.133 Safari/537.36"
]


def http_request(url, method='GET', headers=None, cookies=None, data=None, timeout=30, proxy=None, stream=False):
    _headers = {
        'Accept-Language': 'en-US, en; q=0.8, zh-Hans-CN; q=0.5, zh-Hans; q=0.3',
        'Accept-Encoding': 'gzip, deflate, br',
        'Accept': 'text/html, application/xhtml+xml, image/jxr, */*',
        'Connection': 'Keep-Alive',
    }
    if headers and isinstance(headers, dict):
        _headers.update(headers)
    method = method.upper()
    kwargs = {
        'headers': _headers,
        'cookies': cookies,
        'timeout': timeout,
        'verify': False,
    }
    print(f"[{method}]request url:{url}")
    if method == 'GET':
        kwargs['stream'] = stream
    if proxy:
        kwargs['proxies'] = proxy
    if data and method != "GET" and 'json' in _headers.get('Content-Type', ''):
        data = json.dumps(data)
    scheme, netloc, path, query, fragment = urlsplit(url)
    if query:
        data = data or {}
        data.update((item.split('=', maxsplit=1) for item in query.split("&") if item))
        url = url.split("?")[0]

    res = None
    for i in range(3):
        try:
            if 'User-Agent' not in _headers:
                _headers['User-Agent'] = random.choice(USER_AGENTS)
            kwargs['headers'] = _headers
            if method == 'GET':
                res = requests.get(url, data, **kwargs)
            elif method == 'PUT':
                res = requests.put(url, data, **kwargs)
            elif method == 'DELETE':
                res = requests.delete(url, **kwargs)
            elif method == 'OPTIONS':
                data = data if isinstance(data, dict) else {}
                res = requests.options(url, **data)
            else:
                res = requests.post(url, data, **kwargs)
            if res.ok:
                break
        except:
            if i >= 2:
                raise
    return res


class FileLocker:

    def __init__(self, file, mode='r', buffering=None, encoding=None, errors=None, newline=None, closefd=True,
                 lock_flag=LOCK_EX):
        self.f = open(file, mode, buffering, encoding, errors, newline, closefd)
        self.flag = lock_flag

    def __enter__(self):
        lock(self.f, self.flag)

    def __exit__(self, exc_type, exc_val, exc_tb):
        unlock(self.f)
        self.f.close()


class FileStorage:

    def __init__(self):
        self.tmp_dir = os.path.join('tmp')
        os.makedirs(self.tmp_dir, exist_ok=True)
        self.device_id = get_host_name() or get_host_ip()

    @property
    def logger(self):
        return logger

    @abc.abstractmethod
    def download(self, remoting_path, local_path, progress_callback=None) -> str:
        raise NotImplementedError

    def lock_download(self, remoting_path, local_path, progress_callback=None, expire=1800, flocker=True) -> str:
        if os.path.isfile(local_path):
            return local_path
        if os.path.isfile(remoting_path):
            return remoting_path
        if flocker:
            key = self.get_lock_key(remoting_path)
            res = dist_locker(key, self.download, expire, args=[local_path], kwargs={
                'progress_callback': progress_callback
            })
            return res
        else:
            f = None
            try:
                f = self.acquire_flock(remoting_path, local_path, timeout=expire)
                if os.path.isfile(local_path):
                    return local_path
                res = self.download(remoting_path, local_path, progress_callback)
                return res
            except:
                raise
            finally:
                self.release_flock(f, remoting_path)

    @abc.abstractmethod
    def name(self):
        raise NotImplementedError

    @abc.abstractmethod
    def upload(self, local_path, remoting_path) -> str:
        raise NotImplementedError

    def upload_content(self, remoting_path, content) -> str:
        raise NotImplementedError

    def preview_url(self, remoting_path: str) -> str:
        raise NotImplementedError

    def get_lock_key(self, keyname):
        basename = os.path.basename(keyname)
        md5 = hashlib.md5()
        md5.update(keyname.encode())
        hash_str = md5.hexdigest()[:8]
        # 设备（机器）ID:文件名[远程路径HASH]
        return f"{self.device_id}:{basename}[{hash_str}]"

    def get_lock_filename(self, keyname):
        arr = os.path.splitext(os.path.basename(keyname))
        basename = arr[0]
        md5 = hashlib.md5()
        md5.update(keyname.encode())
        hash_str = md5.hexdigest()[:8]
        return os.path.join("models", "Stable-diffusion", f"{basename}[{hash_str}].lock")

    def acquire_flock(self, keyname, local_path, block=True, timeout=-1):
        lock_path = self.get_lock_filename(keyname)

        f = open(lock_path, "wb+")
        timeout = -1 if timeout is None else timeout

        try:
            ok = lock(f, LOCK_EX | LOCK_NB)
            if not ok:
                if not block:
                    raise OSError("cannot get file lock")
                start = time.time()
                while 1:
                    time.sleep(random.randint(2, 5))
                    waite_time = int(time.time() - start)
                    if waite_time % 4 == 0:
                        logger.debug(
                            f"acquire file locker:{lock_path}, timeout:{timeout} sec, wait time:{waite_time} sec")
                    if timeout > 0 and waite_time > timeout:
                        raise OSError(f"cannot download {keyname}: get file lock timeout")

                    if os.path.isfile(local_path):
                        atime = os.path.getatime(local_path)
                        delay = int(time.time() - atime + 6)
                        if delay > 0:
                            time.sleep(delay)
                            logger.debug(f"acquire file locker:{lock_path}, local file existed, sleep {delay} sec")
                        os.popen(f'touch {local_path}')
                        logger.debug(f"acquire file locker:{lock_path}, local file existed!")
                        break

                    f = open(lock_path, "wb+") if f is None else f
                    ok = lock(f, LOCK_EX | LOCK_NB)
                    if ok:
                        logger.debug(f"get file locker:{lock_path}, waite time:{waite_time} sec!")
                        break
        except:
            if f:
                f.close()

            raise
        return f

    def release_flock(self, f, keyname=None):
        if not f:
            return
        unlock(f)
        f.close()

        if keyname and random.randint(0, 10) < 3:
            lock_path = self.get_lock_filename(keyname)
            dirname = os.path.dirname(lock_path)

            for item in os.listdir(dirname):
                full_path = os.path.join(dirname, item)
                if os.path.isfile(full_path):
                    _, ex = os.path.splitext(full_path)
                    if ex != ".lock":
                        continue
                    try:
                        ctime = os.path.getctime(full_path)
                        if time.time() - ctime > 3600*8:
                            os.remove(full_path)
                    except:
                        continue

    def multi_upload(self, local_remoting_pars: typing.Sequence[typing.Tuple[str, str]]):
        if local_remoting_pars:
            worker_count = cpu_count()
            worker_count = worker_count if worker_count <= 4 else 4
            w = MultiThreadWorker(local_remoting_pars, self.upload, worker_count)
            w.run()

    def multi_download(self, remoting_loc_pairs: typing.Sequence[typing.Tuple[str, str]],
                       with_locker=False, locker_exp=1800, flocker=True):
        if remoting_loc_pairs:
            worker_count = cpu_count()
            worker_count = worker_count if worker_count <= 4 else 4
            executor = self.download if not with_locker else partial(
                self.lock_download, flocker=flocker, expire=locker_exp)
            w = MultiThreadWorker(remoting_loc_pairs, executor, worker_count)
            w.run()

    def download_dir(self, remoting_dir: str, local_dir: str) -> bool:
        if os.path.isdir(local_dir):
            shutil.rmtree(local_dir)

        os.makedirs(local_dir, exist_ok=True)
        return False

    def get_keyname(self, remoting_path: str, bucket_name: str):
        if remoting_path.startswith(bucket_name):
            return remoting_path[len(bucket_name):].lstrip('/')
        return remoting_path

    def close(self):
        pass

    def top_dir(self, p: str) -> str:
        array = p.strip(os.path.sep).split(os.path.sep)
        return array[0]

    def mmie(self, p: str) -> str:
        _, ex = os.path.splitext(p)
        ex = ex.lower()
        mmie_d = {
            '.png': 'image/png',
            '.tar': 'application/x-tar',
            '.txt': 'text/plain',
            '.zip': 'application/zip',
            '.json': 'application/json',
            '.ico': 'image/vnd.microsoft.icon',
            '.jpeg': 'image/jpeg',
            '.jpg': 'image/jpeg',
        }
        if ex in mmie_d:
            return mmie_d[ex]
        return mmie_d['.txt']

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class PrivatizationFileStorage(FileStorage):

    def name(self):
        return 'default'

    def download(self, remoting_path: str, local_path: str, progress_callback=None) -> str:
        if os.path.isfile(local_path):
            return local_path

        # 如果是本地路径
        if os.path.isfile(remoting_path):
            shutil.copy(remoting_path, local_path)
            return local_path

        # http
        if 'http' not in remoting_path.lower():
            raise OSError(f'unsupported file:{remoting_path}')

        filename = os.path.basename(remoting_path)
        filepath = os.path.join(self.tmp_dir, filename)
        if not os.path.isfile(filepath):
            self.logger.info(f"download url: {remoting_path}...")
            resp = http_request(remoting_path)
            if resp:

                if 'Content-Disposition' in resp.headers:
                    cd = resp.headers.get('Content-Disposition')
                    map = dict(
                        (item.strip().split('=')[:2] for item in (item for item in cd.split(';') if '=' in item)))
                    if 'filename' in map:
                        filename = map['filename'].strip('"')

                chunk_size = 512

                self.logger.info(f"save to {filename} ...")
                with open(filepath, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=chunk_size):
                        if chunk:
                            f.write(chunk)

                if os.path.isdir(local_path):
                    local_path = os.path.join(local_path, filename)
        shutil.move(filepath, local_path)
        return local_path

    def upload(self, local_path, remoting_path) -> str:
        # local file system
        if remoting_path != local_path:
            shutil.copy(local_path, remoting_path)
            return remoting_path

    def upload_content(self, remoting_path, content) -> str:
        with open(remoting_path, 'wb+') as f:
            f.write(content)
        return remoting_path

    def preview_url(self, remoting_path: str) -> str:
        return remoting_path
