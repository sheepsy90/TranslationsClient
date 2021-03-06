# -*- coding: utf-8 -*-
from logging import getLogger
from time import time
from uuid import uuid4

from zmq import Context, DEALER, Poller, POLLIN, LINGER
from itertools import chain

from .cache import TranslationCache

_LOG = getLogger(__name__)


class TranslationsServerError(Exception):

    pass


class TranslationsClient():
    """A translation service client.

    Not thread safe! Create and use one instance per thread.
    """

    def __init__(self, server_host, server_port, timeout=3, encoding="utf-8",
                 skip_translations=False, translation_cache=None):
        """ Create a client instance which can be used to get translations.

        A client will close all resources on exit. If resource should be freed
        before that use the close function.


        :type server_host: str
        :type server_port: int
        :param timeout: Timeout for a request, in seconds.
        :type timeout: int
        :type encoding: str
        """
        self._timeout = timeout or 0.001  # seconds
        self._encoding = encoding
        self._context = Context(1)
        self._socket = self._context.socket(DEALER)
        self._socket.set(LINGER, 0)  # do not wait for anything when closing
        self._socket.connect("tcp://{}:{}".format(server_host, server_port))
        self._poller = Poller()
        self._poller.register(self._socket, POLLIN)
        self._skip_translations = skip_translations

        self._translation_cache = translation_cache

        if self._translation_cache and not isinstance(
                self._translation_cache, TranslationCache):
            raise TypeError(
                "The cache needs to be a subclass of TranslationCache")

    def _handle_response(self, req_id, response):
        """ Check the response and extract the translations.

        :type response: [bytes]
        :rtype: [str] or None
        """
        translations = None
        if len(response) < 3:
            raise TranslationsServerError("Server could not handle request.")
        else:
            response = [r.decode(self._encoding) for r in response]
            response_id, _ = response[:2]
            if response_id != req_id:
                _LOG.info(
                    "Got a response for an old or unknown request.",
                    extra={"response": response})
            else:
                translations = response[2:]
                if translations == [""]:
                    raise TranslationsServerError(
                        "Server encountered an error.")
        return translations

    def get(self, lang, country, *keys):
        """ Try to find translations for the given keys (and plural forms).

        If the service is not available or a translation can not be found the
        keys are returned instead.

        Note: if only one key was requested, only a string will be returned
        instead of a list of strings!

        :param keys: Keys and or tuples of key and plural to translate.
        :type keys: str or (str, int)
        :return List of translations for each key or key and plural tuple. If
            something can not be translated (or the service is down) the keys
            will be returned instead of a translation. If there was only one
            key or tuple requested than only that translation is returned
            instead of a list of translations.
        :rtype: str or [str]
        """
        if not keys:
            raise ValueError("No keys to translate!")
        if self._context is None:
            raise AttributeError("Client is closed.")
        keys = [(k, "") if isinstance(k, str) else k for k in keys]
        req_id = uuid4().hex
        # Put an unique id in front of the request, separated by an empty
        # frame. This way the unique id look like an additional routing
        # identity to ZMQ and it will be treated as such by this client, too.
        request = (
            [req_id, "", lang, country or ""] +
            list(
                chain.from_iterable(
                    (k, str(p) if p is not None else "") for k, p in keys)))
        self._socket.send_multipart(
            [part.encode(self._encoding) for part in request])
        # Poll for the response, but only for so long...
        timeout = self._timeout
        translations = None
        while timeout > 0 and translations is None:
            start = time()
            sockets = dict(self._poller.poll(timeout))
            timeout -= (time() - start)
            if self._socket in sockets:
                response = self._socket.recv_multipart()
                translations = self._handle_response(req_id, response)
        if translations is None:  # timeout, return keys
            translations = [key for key, _ in keys]
        return translations if len(keys) > 1 else translations[0]

    def close(self):
        if self._context is not None:
            self._socket.close()
            self._socket = None
            self._context.term()
            self._context = None

    def translate(self, language, country, *keys):
        """ Translate strings with or without their plural flavor.

        :param language: str
        :param country: str
        :type keys: [str or (str, int)]
        :rtype: str or [str]
        """

        if self._skip_translations:
            translations = [k if isinstance(k, str) else k[0] for k in keys]
            return translations if len(translations) > 1 else translations[0]

        if not self._translation_cache:
            return self.get(language, country, *keys)

        translations = self._translation_cache.get_multiple(
            language, country, keys)

        if all(translations):
            return translations if len(keys) > 1 else translations[0]

        # Give all the to be translated things an ordering index
        ordered_translations = list(
            zip(range(len(translations)), keys, translations))

        # Split of the keys that do not have a translation
        indexed_missing_keys = [
            (idx, key) for idx, key, translation in ordered_translations
            if translation is None
        ]

        idx = [idx for idx, key in indexed_missing_keys]
        new_keys = [key for idx, key in indexed_missing_keys]

        missing_translations = self.get(language, country, *new_keys)
        if not isinstance(missing_translations, (list, tuple)):
            missing_translations = [missing_translations]

        self._translation_cache.set_multiple(
            language, country, new_keys, missing_translations)

        missing_translations = list(zip(idx, missing_translations))
        present_translations = [
            (idx, translation) for idx, key, translation in
            ordered_translations]

        final_translations = dict(present_translations)
        final_translations.update(dict(missing_translations))
        final_translations = sorted(final_translations.items(),
                                    key=lambda x: x[0])

        translations = [e[1] for e in final_translations]
        return translations if len(keys) > 1 else translations[0]

    def translate_closure(self, language, country):
        """ Returns a function that encloses the language and the country for
            this particular translation client
        """
        def _t(*keys):
            return self.translate(language, country, *keys)
        return _t
