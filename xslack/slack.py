# -*- coding: utf-8 -*-

from slack import WebClient
from attrdict import AttrDict
from collections import UserList, UserDict
from types import SimpleNamespace
import os
import json
import time
import re


class ApiFailed(Exception):
    pass


class Client:
    def __init__(self, **kwargs):
        self.client = WebClient(**kwargs)

        self.channel = ChannelApi(self)
        self.user = UserApi(self)
        self.chat = ChatApi(self)
        self.emoji = EmojiApi(self)

    def api_call(self, api_method: str, *, files: dict = None, params={}, **kwargs):
        union_params = {**params, **kwargs}
        for key, value in union_params.items():
            if isinstance(value, bool):
                value = 1 if value else 0
                union_params[key] = value
        return AttrDict(
            self.client.api_call(
                api_method, files=files, params=union_params
            ).data
        )

    def __getattr__(self, attr):
        def proxy(**kwargs):
            api_func = getattr(self.client, attr)
            return AttrDict(api_func(**kwargs).data)

        return proxy


class CachedClient(Client):
    """
    CachedClient is quite early stage of the experimental implementation.
    Both of the design and the implementation are not well considered at all.
    """

    def __init__(self, cache_dir=".", **kwargs):
        super().__init__(**kwargs)
        self.cache_dir = cache_dir
        self._cache = {}

    def _setup_cache_dir(self):
        if os.path.isdir(self.cache_dir):
            raise Exception("%s is not a directory" % self.cache_dir)

    def with_cache(self, cache, pred):
        if cache in self._cache:
            return self._cache[cache]

        def _inner():
            if os.path.isfile(cache):
                with open(cache) as f:
                    return AttrDict(json.load(f))

            ret = pred()
            if ret.ok:
                with open(cache, "w") as f:
                    json.dump(ret, f, ensure_ascii=False)

            return ret

        self._cache[cache] = _inner()
        return self._cache[cache]

    def api_call(self, method, *args, **kwargs):
        cache_file = None

        comp = method.split(".")
        if len(comp) >= 2 and comp[-1] in ["list"]:
            cache_file = "%s/%s-%s.json" % (self.cache_dir, comp[0], comp[-1])

        api_call = super().api_call

        def pred():
            return api_call(method, *args, **kwargs)

        if cache_file:
            resp = self.with_cache(cache_file, pred)
        else:
            resp = pred()

        if not resp.ok:
            raise ApiFailed(resp.error)

        return resp


class ChannelApi:
    def __init__(self, client):
        self._client = client

    def list(self):
        resp = self._client.api_call("channels.list")
        return Channels(self._client, resp.channels)

    def info(self, channel):
        resp = self._client.api_call("channels.info", channel=channel)
        return Channel(self._client, resp.channel)

    def history(self, channel, **kwargs):
        return self._client.api_call("channels.history", channel=channel, **kwargs)

    def byName(self, name):
        return self.list().byName(name)


class ChatApi:
    def __init__(self, client):
        self._client = client

    def postEphemeral(self, channel, text, user, **kwargs):
        if isinstance(channel, Channel):
            channel = channel.id
        if isinstance(user, User):
            user = user.id
        return self._client.api_call(
            "chat.postEphemeral", channel=channel, text=text, user=user, **kwargs
        )

    def postMessage(self, channel, text, **kwargs):
        if isinstance(channel, Channel):
            channel = channel.id
        return self._client.api_call(
            "chat.postMessage", channel=channel, text=text, **kwargs
        )


class Channels(UserList):
    def __init__(self, client, data):
        super().__init__(data)
        self._client = client
        self._createIndex()

    def __getitem__(self, *args):
        return Channel(self._client, super().__getitem__(*args))

    def _createIndex(self):
        by_name = {}
        for x in self.data:
            by_name[x.name] = x
        self.idxByName = by_name

    def byName(self, name):
        return Channel(self._client, self.idxByName[name])


class Channel(AttrDict):
    def __init__(self, client, data):
        self._client = client
        super().__init__(data, recursive=False)

    def __getattribute__(self, name):
        # AttrDictに任せると __class__.__init__(data) を呼ばれてしまうため対策
        if name in self and isinstance(self[name], dict):
            return AttrDict(self[name])
        else:
            return super().__getattribute__(name)

    def info(self):
        return self._client.channel.info(channel=self.id)

    def postEphemeral(self, text, user, **kwargs):
        return self._client.chat.postEphemeral(self.id, text, user, **kwargs)

    def postMessage(self, text, **kwargs):
        return self._client.chat.postMessage(self.id, text, **kwargs)

    def history(self, **kwargs):
        return self._client.channel.history(channel=self.id, **kwargs)

    def history_before(self, ts, **kwargs):
        return self.history(newest=ts, **kwargs)

    def history_after(self, ts, batch=False, inclusive=0):
        messages = []

        next_ts = ts
        has_more = True
        while has_more:
            resp = self.history(oldest=next_ts, count=1000, inclusive=inclusive)
            has_more = resp.has_more
            if len(resp.messages):
                messages.extend(reversed(resp.messages))
                next_ts = messages[-1].ts
                inclusive = False
                time.sleep(0.2)

        return ChannelHistory(self._client, messages, has_more)

    @property
    def members(self):
        users = self._client.user.list()
        return tuple(users.byId(x) for x in self["members"])


class ChannelHistory(list, UserList):
    def __init__(self, client, messages, has_more):
        super().__init__(messages)
        self._client = client
        self.has_more = has_more

    def __getitem__(self, *args):
        return Message(super().__getitem__(*args))


class Message(AttrDict):
    pass


class UserApi:
    def __init__(self, client):
        self._client = client

    def list(self, **kwargs):
        resp = self._client.api_call("users.list", **kwargs)
        return Users(self._client, resp.members)

    def conversations(self, user):
        if isinstance(user, User):
            user = user.id
        resp = self._client.api_call("users.conversations", user=user)
        # TODO
        if resp.response_metadata and resp.response_metadata.next_cursor:
            raise Exception(resp.response_metadata)
        return Channels(self._client, resp.channels)

    def byName(self, name):
        return self.list().byName(name)

    def byId(self, id):
        return self.list().byId(id)


class Users(UserList):
    def __init__(self, client, data):
        super().__init__(data)
        self._client = client
        self._createIndex()

    def __getitem__(self, *args):
        return User(self._client, super().__getitem__(*args))

    def _createIndex(self):
        by_id, by_name = {}, {}
        for x in self.data:
            by_id[x.id] = x
            by_name[x.name] = x
        self.idxById = by_id
        self.idxByName = by_name

    def byName(self, name):
        return User(self._client, self.idxByName[name])

    def byId(self, id):
        return User(self._client, self.idxById[id])


class User(AttrDict):
    def __init__(self, client, data):
        self._client = client
        super().__init__(data, recursive=False)

    def __getattribute__(self, name):
        # AttrDictに任せると __class__.__init__(data) を呼ばれてしまうため対策
        if name in self and isinstance(self[name], dict):
            return AttrDict(self[name])
        else:
            return super().__getattribute__(name)

    def conversations(self, **kwargs):
        return self._client.user.conversations(user=self)


class EmojiApi:
    def __init__(self, client):
        self._client = client

    def list(self):
        resp = self._client.api_call("emoji.list")
        return Emojies(self._client, resp.emoji)


class Emojies(UserDict):
    def __init__(self, client, data):
        super().__init__(data)
        self._client = client

    def __getitem__(self, key):
        return Emoji(self, key, super().__getitem__(key))


class Emoji:
    def __init__(self, parent, name, image):
        self._parent = parent
        self.name = name
        self.image = image
        if not image.startswith("alias:"):
            self._init_image(name, image)
        else:
            self._init_alias(name, image)

    def _init_alias(self, name, image):
        alias_to_name = re.match("alias:(.+)", image)[1]
        alias_to = self._parent.get(alias_to_name)
        self.type = "alias"
        self.image_url = alias_to.image_url if alias_to else None

    def _init_image(self, name, image):
        self.type = "image"
        self.image_url = image

    def __repr__(self):
        return "<Emoji(name=%s, type=%s)>" % (self.name, self.type)

